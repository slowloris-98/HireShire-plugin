"""The pid file that replaced the orchestration status document.

What it replaced is the point. The old module answered "is a sweep running" from a
heartbeat, a five-minute staleness window and a liveness veto, and every one of those
parts existed to serve `--status` and a session-scoped teardown that has since been
removed. What is left is a single integer, read by two callers: `--stop`, and the guard
that refuses to become a second writer on the same database.

These tests are about the failure modes of that file, not its happy path. Both callers
have something sensible to do with "no" and nothing sensible to do with an exception.
"""
from __future__ import annotations

from hireshire import sweep_pid


def test_a_pid_survives_the_round_trip(tmp_path):
    sweep_pid.write(4321, tmp_path)
    assert sweep_pid.read(tmp_path) == 4321


def test_no_file_reads_as_no_sweep(tmp_path):
    assert sweep_pid.read(tmp_path) is None


def test_clearing_is_idempotent(tmp_path):
    """`--stop` clears unconditionally, including when it found nothing."""
    sweep_pid.clear(tmp_path)
    sweep_pid.write(99, tmp_path)
    sweep_pid.clear(tmp_path)
    sweep_pid.clear(tmp_path)
    assert sweep_pid.read(tmp_path) is None


def test_a_corrupt_file_reads_as_absent_rather_than_raising(tmp_path):
    """A half-written or hand-edited file must not make `--stop` fail in front of the
    user, nor make the duplicate guard refuse a sweep it cannot justify refusing."""
    sweep_pid.pid_path(tmp_path).write_text("not a pid", encoding="utf-8")
    assert sweep_pid.read(tmp_path) is None


def test_a_nonsense_pid_reads_as_absent(tmp_path):
    """0 and negatives are not pids. Passing them to a kill would be worse than
    treating the file as empty — on POSIX `kill(0, ...)` signals the whole process
    group, which on a scheduled run is not this plugin's to touch."""
    for junk in ("0", "-1", "", "   "):
        sweep_pid.pid_path(tmp_path).write_text(junk, encoding="utf-8")
        assert sweep_pid.read(tmp_path) is None, junk


def test_an_unwritable_directory_does_not_raise(tmp_path):
    """Losing the record costs `--stop` and the duplicate guard; it must not cost the
    user a twenty-minute sweep. The same trade the reports make."""
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    sweep_pid.write(123, blocked)          # must not raise
    assert sweep_pid.read(blocked) is None
