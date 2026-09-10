"""Whether a recurring sweep is running — the one place anything asks.

`/hireshire:start-orchestration` used to *assert* that sweeps had begun, because the
plugin monitor it relied on was supposed to start on its own. Monitors are an
experimental component that is skipped on some hosts, so the claim was sometimes
false and the skill had no way to notice. Told to start it anyway, one session
improvised a detached `nohup … & disown` sweeper, which then contradicted what the
user had been told about it stopping with the session, and left a second writer on
the same SQLite database.

Both failures come from the same gap: nothing could answer "is it running?". This
file answers it, from a small JSON document in the data directory.

Liveness is decided by **heartbeat freshness, with a dead PID as a veto**. The sweeper
rewrites `heartbeat` about once a minute for as long as it lives, so a file whose
heartbeat has gone quiet describes a process that is gone — whether it exited, crashed,
or was killed with the session.

Freshness alone was not enough, and the gap was measured rather than theorised. A
heartbeat is only refreshed every `HEARTBEAT_INTERVAL_S`, so a sweep killed the instant
after one tick still reads as running for the whole `STALE_AFTER_S` window. That window
is five minutes, and in it two things go wrong: `--status` reports a pid that does not
exist (observed reporting `running (pid 25860)` against nothing), and — worse — the
sweeper's own start-up guard refuses to start a **new** sweep because a dead one still
looks alive (observed: `already running — not starting a second` against dead pid
26616). The first misleads a human; the second silently blocks the user's work.

So a recorded PID that is definitively gone now vetoes the answer. It is a veto and not
the primary signal, which is what keeps the original objection satisfied: PID recycling
can only make a dead sweep read as *alive*, and that case is still caught by the
heartbeat going stale. `os.kill(pid, 0)` is not portable to Windows — most of this
plugin's audience — so the probe goes through `hireshire.process_liveness.is_alive`,
which asks Win32 `OpenProcess` there and is stdlib-only like everything else here.

Deliberately **stdlib only**: `scripts/bootstrap.py` serves `--status` on the system
interpreter before the venv exists, and cannot import `hireshire.paths` (which needs
PyYAML).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from hireshire.plugin_dirs import resolve_dirs

STATUS_FILENAME = "orchestration_status.json"

#: How often the running sweeper refreshes `heartbeat`.
HEARTBEAT_INTERVAL_S = 60

#: A heartbeat older than this means the process is gone. Generous next to the
#: refresh interval: a machine that slept, or a sweep pinning the CPU, must not read
#: as dead and let a second sweeper start alongside the first.
STALE_AFTER_S = 300


def status_path(data: Path | None = None) -> Path:
    return (data or resolve_dirs()[1]) / STATUS_FILENAME


def read(data: Path | None = None) -> dict | None:
    """The status document, or None when there is no readable one.

    A corrupt or half-written file is treated as absent rather than raised: the answer
    to "is a sweep running" must never be an exception, because both callers — the
    launcher's `--status` and the sweeper's own start-up guard — have something
    sensible to do with "no".
    """
    try:
        return json.loads(status_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(data: Path | None = None, **fields) -> None:
    """Merge `fields` into the status document and stamp a fresh heartbeat.

    Written to a temporary file and moved into place, so a reader never sees a half
    document. `os.replace` is atomic on Windows as well as POSIX.
    """
    path = status_path(data)
    doc = read(data) or {}
    doc.update(fields)
    doc["heartbeat"] = time.time()

    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Losing a heartbeat is not worth killing a 20-minute sweep over. The next
        # write recovers; a run of failures reads as stopped, which is the safe way
        # to be wrong — it permits a restart rather than blocking one.
        pass


def clear(data: Path | None = None) -> None:
    """Remove the status file, so a clean stop is visible immediately rather than
    after the staleness window."""
    try:
        status_path(data).unlink()
    except OSError:
        pass


def is_running(data: Path | None = None) -> bool:
    """Whether a sweeper is alive: a fresh heartbeat that a dead PID has not vetoed.

    See the module docstring for why the PID check is a veto rather than the primary
    signal. Order matters — the heartbeat is checked first because it is the cheap
    answer and the common one; the probe only runs on a document that still looks live.
    """
    doc = read(data)
    if not doc:
        return False
    try:
        fresh = (time.time() - float(doc["heartbeat"])) < STALE_AFTER_S
    except (KeyError, TypeError, ValueError):
        return False
    if not fresh:
        return False

    pid = doc.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        # Imported here rather than at module scope so that a status file with no
        # usable `pid` — and `--status` on a machine where the probe cannot load —
        # still answers from the heartbeat alone.
        try:
            from hireshire.process_liveness import is_alive
        except ImportError:
            return True
        if not is_alive(pid):
            return False
    return True


def _clock(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "unknown"


def describe(data: Path | None = None) -> str:
    """The `--status` report: what is true, in the words the skill should repeat.

    Phrased so a skill can relay it without adding anything. The skill's job is to
    print what this says, not to conclude anything beyond it.
    """
    doc = read(data)
    if not doc or not is_running(data):
        stopped = "not running"
        if doc and doc.get("last_sweep"):
            stopped += f" (last sweep {_clock(doc['last_sweep'])})"
        return f"HireShire orchestration: {stopped}."

    # A one-shot sweep and a recurring one are both "running", and saying so in the
    # same words would attribute a schedule to a sweep that has none — the skill
    # relays this verbatim, so "every 4.0h" after a `--sweep` is a promise nothing
    # is left running to keep.
    if doc.get("mode") == "once":
        lines = [
            "HireShire: a single sweep is running"
            f" (pid {doc.get('pid', '?')}, started {_clock(doc.get('started_at'))})."
            # ASCII only: `--status` is served by bootstrap.py on the system
            # interpreter, which does not get run_engine.py's utf-8 stdout, so on
            # Windows an em-dash here reaches the user as a replacement character.
            "\n  It is not recurring; nothing is scheduled after it."
        ]
    else:
        lines = [
            "HireShire orchestration: running"
            f" (pid {doc.get('pid', '?')}, every {doc.get('interval_hours', '?')}h,"
            f" started {_clock(doc.get('started_at'))})."
        ]
    if doc.get("last_summary"):
        lines.append(f"  Last sweep {_clock(doc.get('last_sweep'))}: {doc['last_summary']}")
    if doc.get("next_sweep"):
        lines.append(f"  Next sweep due {_clock(doc['next_sweep'])}.")
    return "\n".join(lines)
