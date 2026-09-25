"""Create (or refresh) the plugin's Python venv in ${CLAUDE_PLUGIN_DATA}.

Run from the SessionStart hook. Two rules shape this file:

* The venv goes in the **data** dir, never the install dir. The install dir is
  replaced wholesale on every plugin update, which would silently delete a
  ~1.2 GB torch install and leave the engine unable to import.
* Idempotency is decided by comparing the shipped requirements against a lock
  copy in the data dir, not by testing whether the venv directory exists. A
  half-finished install leaves a directory behind; it does not leave a matching
  lock file, so the next session repairs it.

Also deliberately dependency-free: it runs on the system interpreter before the
venv exists, so it may only import the standard library.

PyTorch is installed through uv's `--torch-backend`, not pip, so a machine with a
GPU gets a GPU build — see `_install_with_uv` for why, and for the three rules
(never swap torch under a live sweep, only an architecture mismatch downgrades to
CPU, a failed download removes nothing) that keep that from costing a working venv.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hireshire import sweep_pid  # noqa: E402
from hireshire.plugin_dirs import MIGRATABLE, legacy_data_dirs, resolve_dirs  # noqa: E402
from hireshire.process_liveness import is_alive  # noqa: E402

ROOT, DATA = resolve_dirs()

VENV_DIR = DATA / "venv"
REQUIREMENTS = ROOT / "requirements-core.txt"
LOCK = DATA / "requirements.lock"
# What torch build the venv ended up with. Informational, except for one marker:
# a line starting with _ARCH_FALLBACK pins this install to the CPU build.
TORCH_VARIANT = DATA / "torch_variant"
_ARCH_FALLBACK = "cpu (architecture fallback"
_LOCK_TORCH_PREFIX = b"\n# torch-backend: "

# Run inside the venv. Prints one tab-separated line: status, torch version, detail.
# ROCm builds answer through torch.cuda too, which is why `hip` counts as a GPU build.
_SMOKE_SCRIPT = r"""
import torch
v = torch.__version__
if torch.cuda.is_available():
    try:
        a = torch.ones(64, 64, device="cuda")
        (a @ a).sum().item()
        print("ok\t" + v + "\t" + torch.cuda.get_device_name(0))
    except Exception as exc:
        msg = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        print("bad_kernels\t" + v + "\t" + msg)
elif torch.version.cuda or torch.version.hip:
    print("no_gpu\t" + v + "\t")
else:
    print("ok\t" + v + "\t")
"""

_BUILD_SCRIPT = (
    "import torch;"
    "print('gpu' if (torch.version.cuda or torch.version.hip) else 'cpu')"
)


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    """The interpreter inside the venv, by absolute path.

    Everything downstream must invoke this rather than a bare `python`: Claude
    Code's hook exec form cannot spawn the `.cmd`/`.bat` shims Windows installs,
    and a bare `python` there can resolve to the Microsoft Store alias stub.
    """
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _torch_backend() -> str | None:
    """The uv `--torch-backend` to ask for, or None for plain pip (macOS).

    macOS has no CUDA builds and PyPI's arm64 wheel already carries MPS, so there is
    nothing for uv to choose. `HIRESHIRE_TORCH` is a support escape hatch (`cpu`,
    `cu126`, `rocm7.2`, ...). Everything else is `auto`, which is the point: uv reads
    the driver and knows the current PyTorch index tags, which change every release,
    so no tag list lives here.
    """
    if sys.platform == "darwin":
        return None
    override = os.environ.get("HIRESHIRE_TORCH", "").strip()
    if override:
        return override
    try:
        if TORCH_VARIANT.read_text(encoding="utf-8").startswith(_ARCH_FALLBACK):
            return "cpu"
    except OSError:
        pass
    return "auto"


def _lock_bytes() -> bytes:
    """The shipped requirements plus the torch backend that was *requested*.

    The request, not the resolved build: recording what uv picked would mean probing
    the GPU on every `is_current()`, which runs on SessionStart and before every
    engine command.
    """
    backend = _torch_backend() or "pypi"
    return REQUIREMENTS.read_bytes() + _LOCK_TORCH_PREFIX + backend.encode() + b"\n"


def is_current() -> bool:
    """True when the venv exists and was built from the shipped requirements."""
    if not venv_python().exists() or not LOCK.exists():
        return False
    if not REQUIREMENTS.exists():
        return True
    return LOCK.read_bytes() == _lock_bytes()


def _requirements_unchanged() -> bool:
    """True when the lock differs from what we want in the torch line alone.

    Covers the pre-uv lock format too, which was the requirements bytes verbatim.
    """
    if not LOCK.exists() or not REQUIREMENTS.exists():
        return False
    old, req = LOCK.read_bytes(), REQUIREMENTS.read_bytes()
    return old == req or (old.startswith(req) and old[len(req):].startswith(_LOCK_TORCH_PREFIX))


def _sweep_running() -> bool:
    """A live sweep holds torch's DLLs; replacing them under it half-removes torch on
    Windows. The pid is one the sweeper wrote about itself, so `is_alive` is safe to
    trust here for the same reason the duplicate-sweeper guard trusts it."""
    pid = sweep_pid.read(DATA)
    return pid is not None and is_alive(pid)


def _installed_torch(py: Path) -> str | None:
    """'gpu', 'cpu', or None when torch does not import in the venv."""
    try:
        result = subprocess.run(
            [str(py), "-c", _BUILD_SCRIPT], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines and lines[-1].strip() in ("gpu", "cpu") else None


def _smoke_test(py: Path) -> tuple[str, str, str]:
    """(status, torch version, detail). Status is `ok`, `no_gpu`, `bad_kernels`, or
    `broken` when torch does not import or the probe does not finish."""
    try:
        result = subprocess.run(
            [str(py), "-c", _SMOKE_SCRIPT], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "broken", "", str(exc)
    # Not .strip(): the detail field is often empty, and stripping would eat its tab.
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    parts = lines[-1].split("\t", 2) if result.returncode == 0 and lines else []
    if len(parts) != 3 or parts[0] not in ("ok", "no_gpu", "bad_kernels"):
        return "broken", "", (result.stderr or "").strip()[-500:]
    return parts[0], parts[1], parts[2]


def _gpu_tool_present() -> bool:
    return bool(shutil.which("nvidia-smi") or shutil.which("rocm-smi"))


def _uv_install(py: Path, backend: str, reinstall_torch: bool) -> subprocess.CompletedProcess:
    cmd = [str(py), "-m", "uv", "pip", "install", "-q", "--python", str(py),
           "--torch-backend", backend]
    if reinstall_torch:
        cmd += ["--reinstall-package", "torch"]
    cmd += ["-r", str(REQUIREMENTS)]
    return subprocess.run(cmd, capture_output=True, text=True)


def _write_variant(text: str) -> None:
    try:
        TORCH_VARIANT.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"HireShire: could not write {TORCH_VARIANT}: {exc}", file=sys.stderr)


def _install_with_uv(py: Path, backend: str) -> subprocess.CompletedProcess:
    """Install the requirements with the torch build this machine can use.

    uv's `--torch-backend` routes torch to the PyTorch index matching the driver and
    everything else to PyPI. It downloads before it replaces anything, so a network
    failure leaves the previous build intact — which the old uninstall-then-install
    idea did not.

    uv looks at the driver version, not the GPU's compute capability (astral-sh/uv
    #14742), so it can pick a build with no kernels for an old or brand-new card.
    The smoke test catches exactly that, and it is the ONLY thing that downgrades to
    CPU. A CUDA build on a machine whose CUDA is merely unavailable (driver asleep,
    Optimus, a transient fault) is kept: sentence-transformers then runs it on CPU,
    and a temporary problem never pins the install to CPU for good.
    """
    up = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "--upgrade", "uv"],
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        return up

    # uv treats an installed `2.14.0+cpu` as satisfying `torch` and keeps it, so a
    # CPU build has to be named for replacement. Only when a GPU might exist, though:
    # otherwise every GPU-less install would re-download the CPU build once.
    reinstall = (
        backend != "cpu" and _gpu_tool_present() and _installed_torch(py) == "cpu"
    )
    result = _uv_install(py, backend, reinstall)
    if result.returncode != 0:
        return result

    status, version, detail = _smoke_test(py)
    if status == "broken":
        return subprocess.CompletedProcess(
            result.args, 1, result.stdout, f"torch does not work in the venv: {detail}"
        )
    if status == "bad_kernels":
        print(
            f"HireShire: this GPU cannot run PyTorch {version} ({detail}); installing "
            "the CPU build instead. A newer PyTorch or driver may fix it.",
            flush=True,
        )
        result = _uv_install(py, "cpu", True)
        if result.returncode != 0:
            return result
        _write_variant(f"{_ARCH_FALLBACK}: {detail})")
        return result

    _write_variant(f"{version} {detail}".strip())
    return result


def _install_failed(py: Path, result: subprocess.CompletedProcess) -> int:
    """Leave the lock absent so the next start retries. If only the torch line changed
    and the old build still imports, keep sweeping on it: refusing to start because a
    GPU upgrade could not download would turn a speedup into an outage."""
    tail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
    if _requirements_unchanged() and _installed_torch(py) is not None:
        print(
            "HireShire: could not update PyTorch; keeping the current build and "
            "retrying on the next start.",
            flush=True,
        )
        print(tail, file=sys.stderr)
        return 0
    print(f"HireShire: dependency install failed\n{tail}", file=sys.stderr)
    return result.returncode or 1


def rescue_stranded_data() -> None:
    """Move config, database and logs out of an install directory into DATA.

    Until 0.2.1 the engine resolved DATA to `ROOT/data` whenever the environment
    did not name one, which is the case for everything the skills run. The install
    directory is replaced on every update, so a user's answers to setup and their
    whole job history sat somewhere that was going to be deleted — and the update
    carrying this fix is exactly the event that would have deleted them.

    Runs before the venv check, so it happens even on an install with nothing else
    to do. Never overwrites: a file already in DATA is the newer one, because DATA
    is where the fixed code writes.
    """
    for legacy in legacy_data_dirs(ROOT, DATA):
        # Only the allowlist moves. Sweeping up "everything except the venv" reads as
        # thorough and is the opposite: it makes the blast radius whatever happens to
        # be in a directory we merely believe is ours, and one wrong guess about which
        # directory that is takes the user's unrelated files with it.
        for name in MIGRATABLE:
            entry = legacy / name
            if not entry.exists():
                continue
            target = DATA / name
            if target.exists():
                continue
            DATA.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(entry), str(target))
                print(f"HireShire: recovered {entry.name} from a previous install", flush=True)
            except OSError as exc:
                # Better to leave a copy behind than to fail the session start.
                print(f"HireShire: could not move {entry}: {exc}", file=sys.stderr)


def paths() -> int:
    """Print the two directories, one `KEY=value` per line.

    This is how a skill learns where DATA is. It must not work it out itself:
    `${CLAUDE_PLUGIN_DATA}` expands to a *different* directory in the Claude
    desktop app than in the terminal or the VS Code extension, so a skill that
    substitutes the placeholder writes somewhere the engine never reads.

    Installs nothing and imports nothing outside the stdlib, so it answers before
    the venv exists — which is when the setup skill first needs it.
    """
    print(f"ROOT={ROOT}")
    print(f"DATA={DATA}")
    return 0


def stop() -> int:
    """Stop a running sweep, then clear the pid file.

    This is now the **only** thing that stops a sweep on purpose. The `SessionEnd` hook
    and the `CLAUDE_PID` watchdog that used to do it automatically are gone: both needed
    a session identity the host does not always publish, and when it was missing they
    did not degrade — they killed every sweep on the machine. See the note in
    `run_orchestration.py`.

    The kill must be **tree-wide**. `run_orchestration.py` re-execs twice (system
    interpreter -> venv -> engine), so the recorded pid is a leaf two levels below the
    process that owns the terminal, and killing it alone leaves the parents alive.

    Failure to kill is reported, never raised, and the pid file is cleared regardless:
    a file naming a process that no longer exists would make the duplicate guard refuse
    the user's next sweep.
    """
    pid = sweep_pid.read(DATA)
    if pid is None:
        sweep_pid.clear(DATA)
        print("HireShire: no sweep on record; nothing to stop.")
        # Still worth running: a sweep killed some other way (the shell task, Task
        # Manager) left its dashboards reading "running", and this is how to fix them.
        _finalise_stopped_runs()
        return 0

    killed = False
    if sys.platform == "win32":
        # /T reaches the recorded process and everything under it, which is what
        # takes down an apply subprocess and the browser it is driving.
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            killed = False
    else:
        # `pkill -P` signals the CHILDREN of pid and never pid itself, so it is a
        # first step, never the whole job. This used to gate the SIGTERM below on
        # pkill having *failed*, which meant that whenever pkill succeeded — that
        # is, whenever the sweep had a child — the sweeper was left running and
        # reported as stopped. The sweep has a child in exactly one situation:
        # while `claude -p` drives a browser through the apply phase. So the stop
        # path failed at the one moment that mattered most.
        try:
            subprocess.run(
                ["pkill", "-TERM", "-P", str(pid)], capture_output=True, text=True
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            killed = False

    sweep_pid.clear(DATA)
    if killed:
        print(f"HireShire: sweep stopped (pid {pid}).")
        _wait_for_exit(pid)
        _finalise_stopped_runs()
        return 0
    print(
        f"HireShire: could not stop pid {pid}; the record was cleared anyway.\n"
        "  If a sweep is still writing, end it from Task Manager (Windows) or "
        "`kill` it directly."
    )
    return 1


def _wait_for_exit(pid: int, timeout_s: float = 10.0) -> None:
    """Give a killed sweep a moment to actually go, so its SQLite handle is released
    before the finaliser opens the same database."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and is_alive(pid):
        time.sleep(0.25)


def _finalise_stopped_runs() -> None:
    """Write what the killed sweep's `finally` never did, so both dashboards stop
    reading "running" — see `scripts/finalise_stopped.py`.

    Runs in the venv, because it needs the engine; this file stays stdlib-only. A
    failure is reported, never raised: the sweep is already stopped, which is what
    the user asked for, and the next sweep's start-up retries the same finalisation.
    """
    python = venv_python()
    if not python.exists():
        return
    env = dict(os.environ)
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(ROOT))
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    try:
        result = subprocess.run(
            [str(python), str(ROOT / "scripts" / "finalise_stopped.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        if result.stdout.strip():
            print(result.stdout.strip())
        return
    print("HireShire: could not update the dashboards for the stopped run; "
          "the next sweep will (see logs/orchestration.log).")


def check() -> int:
    """Session-start probe. Recovers stranded data, reports readiness, installs nothing.

    Installing from the SessionStart hook looked reasonable and was not: the hook runs
    before the user has typed anything and blocks their first turn, so a fresh install
    spent minutes downloading in silence while the "this takes a while" warning sat in
    the setup skill, unable to run until the thing it warns about had finished. The
    user sees a spinner and concludes the plugin is stuck.

    So the heavy work moved to the skill, which can talk. What is left here is a line
    of stdout — which SessionStart hands to the agent as context, the one channel that
    does reach the user.
    """
    rescue_stranded_data()
    if is_current():
        return 0
    # Time, not size. A gigabyte count is not something the user can act on, and it
    # reads as a warning about their disk rather than an answer to the question they
    # are actually asking, which is how long they will be waiting.
    if venv_python().exists():
        # Stale, not missing: a release changed the requirements or the torch line.
        # Neutral about GPUs on purpose — a CPU-only machine lands here too.
        print(
            "HireShire: dependencies need updating; the next sweep start takes a few "
            "extra minutes.",
            flush=True,
        )
        return 0
    print(
        "HireShire: dependencies are not installed yet. Before running anything, tell "
        "the user that the first /hireshire:setup takes about 15-20 minutes, then "
        "start it.",
        flush=True,
    )
    return 0


def main() -> int:
    rescue_stranded_data()

    if is_current():
        return 0

    py = venv_python()
    # First, before anything touches the venv: bootstrap runs ahead of the sweeper's
    # own duplicate guard, so a second start would otherwise swap torch under the
    # running sweep. The engine carries on with the venv it has.
    if py.exists() and _sweep_running():
        print(
            "HireShire: a sweep is running; dependencies will update on the next start "
            "after it stops.",
            flush=True,
        )
        return 0

    DATA.mkdir(parents=True, exist_ok=True)

    if not py.exists():
        print(f"HireShire: creating venv at {VENV_DIR} (this takes a moment)", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)

    if not REQUIREMENTS.exists():
        print(f"HireShire: no requirements file at {REQUIREMENTS}", file=sys.stderr)
        return 1

    print(
        "HireShire: installing dependencies. The first run downloads PyTorch and "
        "two small transformer models, and takes about 15-20 minutes.",
        flush=True,
    )
    backend = _torch_backend()
    if backend is None:
        result = subprocess.run(
            [str(py), "-m", "pip", "install", "--disable-pip-version-check",
             "-q", "-r", str(REQUIREMENTS)],
            capture_output=True,
            text=True,
        )
    else:
        result = _install_with_uv(py, backend)
    if result.returncode != 0:
        # Leave the lock absent so the next session retries rather than assuming
        # a broken environment is good.
        return _install_failed(py, result)

    # Only now, on success, does the lock get written. After an architecture
    # fallback `_torch_backend()` reads the marker and records `cpu`.
    LOCK.write_bytes(_lock_bytes())
    print("HireShire: ready. Run /hireshire:setup to get started.", flush=True)
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--paths" in argv:
        sys.exit(paths())
    if "--stop" in argv:
        sys.exit(stop())
    sys.exit(check() if "--check" in argv else main())
