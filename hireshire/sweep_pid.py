"""The pid of the running sweep, and nothing else.

This replaced `orchestration_status.py`, which answered "is a sweep running" from a
heartbeat, a five-minute staleness window and a liveness veto. All of that existed to
support `--status` and a session-scoped teardown, and both are gone — see the note in
CLAUDE.md on why the sweep no longer couples itself to a Claude Code session.

Two callers, and only two:

* `scripts/run_orchestration.py` writes its pid at start-up, clears it on the way out,
  and reads it once to refuse becoming a second sweeper. Two sweepers are two writers
  on one SQLite database doing identical work.
* `scripts/bootstrap.py --stop` reads it to know what to kill.

Nothing here decides whether that pid is *alive* — `hireshire.process_liveness` does,
and it is asked only by the duplicate guard. That separation is deliberate. The bug
that prompted all of this was never a liveness probe being wrong; it was the plugin
feeding a probe an identity it had guessed at (`CLAUDE_PID`, a shell's `$$`). A pid
this module wrote about itself is the one identity that needs no guessing.

Deliberately **stdlib only**: `bootstrap.py` serves `--stop` on the system interpreter
before the venv exists, and cannot import `hireshire.paths` (which needs PyYAML).
"""

from __future__ import annotations

import os
from pathlib import Path

from hireshire.plugin_dirs import resolve_dirs

PID_FILENAME = "orchestration.pid"


def pid_path(data: Path | None = None) -> Path:
    return (data or resolve_dirs()[1]) / PID_FILENAME


def read(data: Path | None = None) -> int | None:
    """The recorded pid, or None when there is no usable one.

    A missing, empty, corrupt or non-positive file all answer None rather than raising.
    Both callers have something sensible to do with "no", and neither has anything
    sensible to do with an exception: `--stop` would fail in front of the user, and the
    duplicate guard would refuse to start a sweep because a file was malformed.
    """
    try:
        pid = int(pid_path(data).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write(pid: int, data: Path | None = None) -> None:
    """Record `pid` as the running sweep.

    Written to a temporary file and moved into place so a reader never sees a
    half-written number; `os.replace` is atomic on Windows as well as POSIX.

    Failing to write is not worth ending a sweep over. The cost is a sweep `--stop`
    cannot reach and a duplicate guard that will not fire — the user can still kill the
    shell task, which is the fallback the skill already documents.
    """
    path = pid_path(data)
    tmp = path.with_suffix(".pid.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(str(pid), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def clear(data: Path | None = None) -> None:
    """Forget the recorded sweep. Absent is the same as cleared."""
    try:
        pid_path(data).unlink()
    except OSError:
        pass
