"""The file that decides whether a recurring sweep is running.

Two bugs motivated it, and both are asserted here. The skill used to *claim* sweeps
had started, so a user could be told a sweep was live while nothing ran. And a session
that started one by hand had no way to notice an existing sweeper, leaving two writers
on one SQLite database.
"""
from __future__ import annotations

import time

from hireshire import orchestration_status as status


def test_nothing_written_reads_as_not_running(tmp_path):
    assert status.read(tmp_path) is None
    assert status.is_running(tmp_path) is False
    assert "not running" in status.describe(tmp_path)


def test_a_fresh_heartbeat_reads_as_running(tmp_path):
    status.write(tmp_path, pid=4242, interval_hours=4, started_at=time.time())

    assert status.is_running(tmp_path) is True
    described = status.describe(tmp_path)
    assert "running" in described and "4242" in described


def test_a_stale_heartbeat_reads_as_stopped(tmp_path):
    """The sweeper stops writing when it dies with the session, is killed, or crashes.
    None of those get a chance to clean up, so silence has to be what marks it gone —
    otherwise a stale file blocks every future start."""
    status.write(tmp_path, pid=4242, interval_hours=4)
    doc = status.read(tmp_path)
    doc["heartbeat"] = time.time() - (status.STALE_AFTER_S + 1)
    status.status_path(tmp_path).write_text(__import__("json").dumps(doc), encoding="utf-8")

    assert status.is_running(tmp_path) is False


def test_liveness_never_probes_the_pid(tmp_path):
    """`os.kill(pid, 0)` is the usual trick and is not portable to Windows, which is
    most of this plugin's audience. A recycled PID would also read as alive. The pid is
    recorded for humans only, so a live heartbeat under an impossible pid still counts."""
    status.write(tmp_path, pid=999_999_999, interval_hours=4)

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
