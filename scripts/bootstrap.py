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
"""

from __future__ import annotations

import json
import os
import shutil
import signal
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


def stop() -> int:
    """Stop a recurring sweep, then clear the status file.

    The monitor is deliberately not detached — it is meant to stop with the session
    that started it — but on Windows it has repeatedly outlived one, leaving a sweeper
    writing to the database with no way to reach it short of Task Manager. Without
    this the user's only recourse is finding a PID by hand.

    The kill must be **tree-wide**. `run_orchestration.py` re-execs twice (system
    interpreter -> venv -> engine), so the `pid` in the status file is a leaf two
    levels below the process that owns the terminal, and killing it alone leaves the
    parents alive to be misread as a live sweep.

    Failure to kill is reported, never raised: the status file is cleared regardless,
    because a stale document that says "running" is the more harmful of the two states
    and `describe` already treats a quiet heartbeat as stopped.
    """
    doc = orchestration_status.read(DATA)
    if not doc or not orchestration_status.is_running(DATA):
        orchestration_status.clear(DATA)
        print("HireShire orchestration: not running; nothing to stop.")
        return 0

    pid = doc.get("pid")
    killed = False
    if isinstance(pid, int):
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

    orchestration_status.clear(DATA)
    if killed:
        print(f"HireShire orchestration: stopped (pid {pid}).")
        return 0
    print(
        f"HireShire orchestration: could not stop pid {pid}; status cleared anyway.\n"
        "  If a sweep is still writing, end it from Task Manager (Windows) or "
        "`kill` it directly."
    )
    return 1


#: SessionEnd reasons that do NOT mean the session is over. `/clear` and a resume both
#: leave the user sitting in a live session, and stopping their sweep there would be a
#: new bug of exactly the kind this hook exists to prevent.
#:
#: Held as a deny-list rather than an allow-list of endings on purpose: an unfamiliar
#: or missing reason then still stops the sweep. The whole point of this hook is that
#: a sweep must not outlive its session, so an unknown reason should fail toward
#: stopping — the cost is a sweep the user restarts, against an orphan they can only
#: reach through Task Manager.
_SESSION_CONTINUES_REASONS = frozenset({"clear", "resume"})


def session_end() -> int:
    """Stop the sweep when the session that started it ends.

    The recurring sweep is documented as session-scoped and on Windows it was not:
    nothing signals an orphan there — no process group, no SIGHUP — so a monitor
    outlived its session repeatedly, once with a shortlist in hand and auto-apply
    enabled. This is the clean half of the fix: the host tells us the session is over
    and we stop.

    The other half is in `run_orchestration.py`, which watches the session's pids
    itself, because this hook cannot fire when Claude Code is force-killed or crashes.
    Neither layer is sufficient alone.

    Silence is the default, as in `approve.py`: an unreadable payload leaves the sweep
    running rather than guessing at it.
    """
    try:
        # No payload at all is treated as an empty one: the hook fired, so the session
        # ended — only the reason is missing, and that is the deny-list's case. A
        # payload that is unreadable or not an object is different, and left alone.
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("reason") in _SESSION_CONTINUES_REASONS:
        return 0
    return stop()


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
        "two small transformer models, and takes about 15-20 minutes.",
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
    if "--stop" in argv:
        sys.exit(stop())
    if "--session-end" in argv:
        sys.exit(session_end())
    sys.exit(check() if "--check" in argv else main())
