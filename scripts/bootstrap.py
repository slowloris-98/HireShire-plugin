"""Create (or refresh) the plugin's Python venv in ${CLAUDE_PLUGIN_DATA}.

Run from the SessionStart hook. Two rules shape this file:

* The venv goes in the **data** dir, never the install dir. The install dir is
  replaced wholesale on every plugin update, which would silently delete a
  2.5 GB torch install and leave the engine unable to import.
* Idempotency is decided by comparing the shipped requirements against a lock
  copy in the data dir, not by testing whether the venv directory exists. A
  half-finished install leaves a directory behind; it does not leave a matching
  lock file, so the next session repairs it.

Also deliberately dependency-free: it runs on the system interpreter before the
venv exists, so it may only import the standard library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hireshire import orchestration_status  # noqa: E402
from hireshire.plugin_dirs import MIGRATABLE, legacy_data_dirs, resolve_dirs  # noqa: E402

ROOT, DATA = resolve_dirs()

VENV_DIR = DATA / "venv"
REQUIREMENTS = ROOT / "requirements-core.txt"
LOCK = DATA / "requirements.lock"


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    """The interpreter inside the venv, by absolute path.

    Everything downstream must invoke this rather than a bare `python`: Claude
    Code's hook exec form cannot spawn the `.cmd`/`.bat` shims Windows installs,
    and a bare `python` there can resolve to the Microsoft Store alias stub.
    """
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def is_current() -> bool:
    """True when the venv exists and was built from the shipped requirements."""
    if not venv_python().exists() or not LOCK.exists():
        return False
    if not REQUIREMENTS.exists():
        return True
    return LOCK.read_bytes() == REQUIREMENTS.read_bytes()


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


def status() -> int:
    """Print whether a recurring sweep is running.

    This exists so `/hireshire:start-orchestration` can *verify* instead of assert.
    The skill used to announce that sweeps had begun on the strength of having been
    invoked; when the mechanism behind that did not fire, the user was told a live
    sweep was running and nothing contradicted it.

    Installs nothing and imports nothing outside the stdlib, so it answers on a fresh
    machine as readily as a warm one.
    """
    print(orchestration_status.describe(DATA))
    return 0


def check() -> int:
    """Session-start probe. Recovers stranded data, reports readiness, installs nothing.

    Installing from the SessionStart hook looked reasonable and was not: the hook runs
    before the user has typed anything and blocks their first turn, so a fresh install
    spent ~4 minutes downloading 2.5 GB in silence while the "this takes 10-15 minutes"
    warning sat in the setup skill, unable to run until the thing it warns about had
    finished. The user sees a spinner and concludes the plugin is stuck.

    So the heavy work moved to the skill, which can talk. What is left here is a line
    of stdout — which SessionStart hands to the agent as context, the one channel that
    does reach the user.
    """
    rescue_stranded_data()
    if is_current():
        return 0
    print(
        "HireShire: dependencies are not installed yet. Before running anything, tell "
        "the user that the first /hireshire:setup downloads about 2.5 GB and takes "
        "10-15 minutes, then start it.",
        flush=True,
    )
    return 0


def main() -> int:
    rescue_stranded_data()

    if is_current():
        return 0

    DATA.mkdir(parents=True, exist_ok=True)

    py = venv_python()
    if not py.exists():
        print(f"HireShire: creating venv at {VENV_DIR} (this takes a moment)", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)

    if not REQUIREMENTS.exists():
        print(f"HireShire: no requirements file at {REQUIREMENTS}", file=sys.stderr)
        return 1

    print(
        "HireShire: installing dependencies. The first run downloads PyTorch and "
        "two small transformer models (~2.5 GB) and can take several minutes.",
        flush=True,
    )
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "-q", "-r", str(REQUIREMENTS)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Leave the lock absent so the next session retries rather than assuming
        # a broken environment is good.
        print(f"HireShire: dependency install failed\n{result.stderr[-2000:]}", file=sys.stderr)
        return result.returncode

    # Only now, on success, does the lock get written.
    LOCK.write_bytes(REQUIREMENTS.read_bytes())
    print("HireShire: ready. Run /hireshire:setup to get started.", flush=True)
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--paths" in argv:
        sys.exit(paths())
    if "--status" in argv:
        sys.exit(status())
    sys.exit(check() if "--check" in argv else main())
