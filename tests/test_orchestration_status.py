"""The file that decides whether a recurring sweep is running.

Two bugs motivated it, and both are asserted here. The skill used to *claim* sweeps
had started, so a user could be told a sweep was live while nothing ran. And a session
that started one by hand had no way to notice an existing sweeper, leaving two writers
on one SQLite database.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# `scripts/` is not a package, and the `stop()` tests below import `bootstrap` from it.
# Without this the file passes only when some other test module has already put that
# directory on sys.path, so running this one alone failed with ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from hireshire import orchestration_status as status  # noqa: E402

#: A pid that is genuinely running, for the tests that mean "a live sweep". Liveness is
#: now a veto on a fresh heartbeat, so an arbitrary number here would silently make
#: those tests assert the opposite of what they are about.
LIVE_PID = os.getpid()


def test_nothing_written_reads_as_not_running(tmp_path):
    assert status.read(tmp_path) is None
    assert status.is_running(tmp_path) is False
    assert "not running" in status.describe(tmp_path)


def test_a_fresh_heartbeat_reads_as_running(tmp_path):
    status.write(tmp_path, pid=LIVE_PID, interval_hours=4, started_at=time.time())

    assert status.is_running(tmp_path) is True
    described = status.describe(tmp_path)
    assert "running" in described and str(LIVE_PID) in described


def test_a_stale_heartbeat_reads_as_stopped(tmp_path):
    """The sweeper stops writing when it dies with the session, is killed, or crashes.
    None of those get a chance to clean up, so silence has to be what marks it gone —
    otherwise a stale file blocks every future start."""
    status.write(tmp_path, pid=4242, interval_hours=4)
    doc = status.read(tmp_path)
    doc["heartbeat"] = time.time() - (status.STALE_AFTER_S + 1)
    status.status_path(tmp_path).write_text(__import__("json").dumps(doc), encoding="utf-8")

    assert status.is_running(tmp_path) is False


def test_a_dead_pid_vetoes_a_fresh_heartbeat(tmp_path):
    """**This reverses `test_liveness_never_probes_the_pid`, and the reversal is
    deliberate — do not restore it.**

    The original rule was "the pid is recorded for humans, never for liveness", on the
    grounds that `os.kill(pid, 0)` is not portable to Windows and a recycled pid reads
    as alive. Both remain true, and neither is an argument for ignoring a pid that is
    definitively *gone*.

    What forced the change was measured, twice, on one machine. A heartbeat is refreshed
    only every `HEARTBEAT_INTERVAL_S`, so a sweep killed just after a tick still reads as
    running for the rest of the `STALE_AFTER_S` window — five minutes. In that window
    `--status` reported `running (pid 25860)` against a process that did not exist, and
    the sweeper's own start-up guard refused a new sweep with `already running — not
    starting a second` against dead pid 26616. The first misleads a human; the second
    silently blocks the user's work.

    Portability is handled by going through `process_liveness.is_alive` (Win32
    `OpenProcess` on Windows) rather than `os.kill`. Recycling is handled by this being
    a **veto**, never the primary signal: a recycled pid can only make a dead sweep look
    alive, and that case is still caught by the heartbeat going stale.
    """
    status.write(tmp_path, pid=999_999_999, interval_hours=4)

    assert status.is_running(tmp_path) is False
    assert "not running" in status.describe(tmp_path)


def test_a_one_shot_sweep_is_not_described_as_recurring(tmp_path):
    """`--status` is relayed verbatim by the skills, and `interval_hours` is recorded
    for both modes. Reporting "every 4.0h" after a `--sweep` would attribute a schedule
    to a sweep that has none — a runtime claim the plugin cannot back."""
    status.write(tmp_path, pid=LIVE_PID, interval_hours=4, started_at=time.time(),
                 mode="once")

    described = status.describe(tmp_path)
    assert "running" in described
    assert "every" not in described and "4.0h" not in described
    assert "not recurring" in described


def test_a_recurring_sweep_still_reports_its_interval(tmp_path):
    status.write(tmp_path, pid=LIVE_PID, interval_hours=4, started_at=time.time(),
                 mode="recurring")

    assert "every 4h" in status.describe(tmp_path)


def test_a_missing_pid_still_answers_from_the_heartbeat_alone(tmp_path):
    """The veto needs a pid to act on. A document without a usable one — legacy files,
    or a half-written record — must fall back to freshness rather than read as dead."""
    status.write(tmp_path, interval_hours=4)

    assert status.is_running(tmp_path) is True


def test_writes_merge_rather_than_replace(tmp_path):
    """Cycle updates carry only what changed; the identity written at startup has to
    survive them, or `--status` loses the interval it is meant to report."""
    status.write(tmp_path, pid=7, interval_hours=12, started_at=1000.0)
    status.write(tmp_path, last_summary="3 new match(es)")

    doc = status.read(tmp_path)
    assert doc["pid"] == 7 and doc["interval_hours"] == 12
    assert doc["last_summary"] == "3 new match(es)"


def test_clear_makes_a_clean_stop_visible_immediately(tmp_path):
    status.write(tmp_path, pid=7, interval_hours=4)
    status.clear(tmp_path)

    assert status.is_running(tmp_path) is False
    status.clear(tmp_path)  # absent file is not an error


def test_a_corrupt_status_file_reads_as_not_running(tmp_path):
    """A half-written file must not raise: both callers — the launcher's `--status` and
    the sweeper's own guard — need an answer, and "no" is the safe one. It permits a
    restart rather than blocking one forever."""
    status.status_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert status.read(tmp_path) is None
    assert status.is_running(tmp_path) is False


def test_describe_reports_the_configured_interval_not_a_default(tmp_path):
    """The bug this guards: `orchestrate.py --interval` defaults to 4 hours and never
    reads the user's config, so a hand-rolled sweeper silently ignored their choice."""
    status.write(tmp_path, pid=7, interval_hours=12, started_at=time.time())

    assert "every 12h" in status.describe(tmp_path)


# --- stopping a sweep the session failed to take with it ---------------------
#
# `--stop` exists because "session-scoped" turned out to be the intent rather than a
# guarantee: on Windows a monitor has outlived its session more than once, leaving a
# sweeper on the database reachable only through Task Manager.

def _stop_with(monkeypatch, tmp_path, platform, returncode=0):
    """Run bootstrap.stop() against a fake kill, returning (exit_code, command)."""
    import bootstrap

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        class R:
            pass
        R.returncode = returncode
        return R

    monkeypatch.setattr(bootstrap, "DATA", tmp_path)
    monkeypatch.setattr(bootstrap.sys, "platform", platform)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(bootstrap.os, "kill", lambda *a: None)
    return bootstrap.stop(), seen.get("cmd")


def test_stop_kills_the_whole_tree_on_windows(monkeypatch, tmp_path):
    """The recorded pid is a LEAF. run_orchestration.py re-execs twice (system
    interpreter -> venv -> engine), so killing it alone leaves its parents running and
    they read as a live sweep. /T is what makes this correct, not tidy."""
    status.write(tmp_path, pid=LIVE_PID, interval_hours=4, started_at=time.time())

    code, cmd = _stop_with(monkeypatch, tmp_path, "win32")

    assert code == 0
    assert "/T" in cmd and str(LIVE_PID) in cmd
    assert status.is_running(tmp_path) is False


def test_stop_on_a_dead_sweep_succeeds_and_clears(monkeypatch, tmp_path):
    """Nothing to kill is not a failure — and the stale file still has to go, because a
    document claiming "running" is the more harmful of the two ways to be wrong."""
    status.write(tmp_path, pid=4242, interval_hours=4)
    doc = status.read(tmp_path)
    doc["heartbeat"] = time.time() - (status.STALE_AFTER_S + 1)
    status.status_path(tmp_path).write_text(__import__("json").dumps(doc), encoding="utf-8")

    code, cmd = _stop_with(monkeypatch, tmp_path, "win32")

    assert code == 0
    assert cmd is None, "a dead sweep must not be killed"
    assert status.read(tmp_path) is None


def test_stop_clears_the_status_file_even_when_the_kill_fails(monkeypatch, tmp_path):
    """Reported, not raised. Leaving the file behind would block a restart on the
    strength of a process that may already be gone."""
    status.write(tmp_path, pid=LIVE_PID, interval_hours=4, started_at=time.time())

    code, _ = _stop_with(monkeypatch, tmp_path, "win32", returncode=1)

    assert code == 1
    assert status.read(tmp_path) is None
