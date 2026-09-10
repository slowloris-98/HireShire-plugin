"""The sweep must not outlive the session that started it.

Two mechanisms enforce that and neither is sufficient alone: the `SessionEnd` hook
(`bootstrap.session_end`) for an orderly exit, and a pid watchdog inside the monitor
(`run_orchestration`) for the crash and force-kill cases where no hook fires. These
tests cover the decision each one makes, not the killing itself.

The bug they exist for is real and was observed twice: on Windows an orphaned monitor
is re-parented in silence — no process group, no SIGHUP — and one survived its session
with a shortlist in hand and auto-apply enabled.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hireshire import paths
from hireshire.process_liveness import is_alive

sys.path.insert(0, str(paths.ROOT / "scripts"))


# --- is_alive -------------------------------------------------------------------


def test_a_process_is_alive_to_itself():
    """Load-bearing, not a triviality: on POSIX `exec` preserves the pid, so the
    HIRESHIRE_SHELL_PID the monitor is handed is its own. That check has to answer
    "alive" forever rather than kill the sweep on its first tick."""
    assert is_alive(os.getpid()) is True


def test_a_process_that_has_exited_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert is_alive(proc.pid) is False


@pytest.mark.parametrize("pid", [0, -1, -99999, None, "1234", 1.5, True, False])
def test_nonsense_never_raises(pid):
    """The only caller is a watchdog deciding whether to keep sweeping. An exception
    there would end the run for entirely the wrong reason."""
    assert is_alive(pid) is False


# --- which pids the monitor watches ----------------------------------------------


def _session_pid(monkeypatch, value=None):
    import run_orchestration

    if value is None:
        monkeypatch.delenv(run_orchestration._SESSION_PID_VAR, raising=False)
    else:
        monkeypatch.setenv(run_orchestration._SESSION_PID_VAR, value)
    return run_orchestration._session_pid()


def test_the_session_pid_comes_from_claude_pid(monkeypatch):
    """Claude Code publishes its own pid there, in the OS namespace `is_alive` needs.

    The predecessor took `$$`/`$PPID` from Git Bash, which are MSYS pids, and killed a
    healthy sweep on its first tick. See
    `test_the_launcher_never_passes_a_shell_pid_to_the_watchdog`.
    """
    assert _session_pid(monkeypatch, "31292") == 31292


@pytest.mark.parametrize("junk", [None, "", "   ", "not-a-pid", "0", "-5"])
def test_the_watchdog_fails_open_when_there_is_no_trustworthy_pid(monkeypatch, junk):
    """Unknown must never mean kill. A plain terminal, another interface and the OS
    scheduler route all arrive with no session behind them, and a watchdog that
    guessed there would stop sweeps nobody asked it to."""
    assert _session_pid(monkeypatch, junk) is None


def test_a_live_session_pid_reads_as_live(monkeypatch):
    """The regression guard, and the assertion whose absence let an MSYS pid through:
    a pid that is definitely running must be seen as running. Everything else in this
    file can pass while the watchdog still kills every sweep it watches."""
    pid = _session_pid(monkeypatch, str(os.getpid()))
    assert pid == os.getpid()
    assert is_alive(pid) is True


@pytest.mark.skipif(
    not os.environ.get("CLAUDE_PID", "").isdigit(),
    reason="only meaningful when running under Claude Code",
)
def test_the_real_claude_pid_is_an_os_pid():
    """End to end, against the live host: whatever Claude Code puts in CLAUDE_PID has
    to be a pid this platform's API recognises. Under Claude Code this fails loudly on
    any future namespace mismatch; anywhere else it skips."""
    assert is_alive(int(os.environ["CLAUDE_PID"])) is True


# --- the SessionEnd hook's decision ----------------------------------------------


def _session_end(monkeypatch, payload: str) -> list[str]:
    import bootstrap

    stopped: list[str] = []
    monkeypatch.setattr(bootstrap, "stop", lambda: stopped.append("stop") or 0)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(payload))
    assert bootstrap.session_end() == 0
    return stopped


@pytest.mark.parametrize("reason", ["clear", "resume"])
def test_a_session_that_continues_does_not_stop_the_sweep(monkeypatch, reason):
    """`/clear` leaves the user sitting in a live session. Killing their sweep there
    would be a new bug of exactly the kind this hook exists to prevent."""
    assert _session_end(monkeypatch, '{"reason": "%s"}' % reason) == []


@pytest.mark.parametrize("reason", ["logout", "prompt_input_exit", "other"])
def test_a_real_session_end_stops_the_sweep(monkeypatch, reason):
    assert _session_end(monkeypatch, '{"reason": "%s"}' % reason) == ["stop"]


def test_an_unfamiliar_reason_still_stops_the_sweep(monkeypatch):
    """Deny-list, not allow-list. A reason this code has never heard of still means
    the session ended, and failing toward "stop" costs a restart rather than an
    orphan the user can only reach through Task Manager."""
    assert _session_end(monkeypatch, '{"reason": "something-new"}') == ["stop"]
    # An empty payload is the same situation: the hook fired, so the session ended,
    # and only the reason is missing.
    assert _session_end(monkeypatch, "{}") == ["stop"]
    assert _session_end(monkeypatch, "") == ["stop"]


# --- whose sweep is it? ------------------------------------------------------------


def _session_end_owned(monkeypatch, tmp_path, owner, ending, payload='{"reason": "other"}'):
    """Run the hook against a status file owned by `owner`, as session `ending`."""
    import bootstrap
    from hireshire import orchestration_status as status

    fields = {"pid": os.getpid(), "interval_hours": 4}
    if owner is not None:
        fields["session_pid"] = owner
    status.write(tmp_path, **fields)

    stopped: list[str] = []
    monkeypatch.setattr(bootstrap, "DATA", tmp_path)
    monkeypatch.setattr(bootstrap, "stop", lambda: stopped.append("stop") or 0)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(payload))
    if ending is None:
        monkeypatch.delenv("CLAUDE_PID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PID", str(ending))

    assert bootstrap.session_end() == 0
    return stopped


def test_another_sessions_sweep_is_left_alone(monkeypatch, tmp_path):
    """The bug this exists to stop. The hook fires for EVERY session that ends, and it
    used to kill whatever the status file named. With several Claude Code sessions open
    — seven were measured on one machine — any of them ending took down another
    session's sweep, which looks exactly like the sweep crashing: tree-killed, exit 1,
    no traceback, no unwind."""
    assert _session_end_owned(monkeypatch, tmp_path, owner=1111, ending=2222) == []


def test_the_owning_session_ending_does_stop_the_sweep(monkeypatch, tmp_path):
    """The scoping must not defeat the hook's whole purpose."""
    assert _session_end_owned(monkeypatch, tmp_path, owner=1111, ending=1111) == ["stop"]


def test_a_sweep_with_no_recorded_owner_is_still_stoppable(monkeypatch, tmp_path):
    """Sweeps started before `session_pid` existed must not become unstoppable by the
    hook meant to reap them. Absence of an owner is not evidence of another owner."""
    assert _session_end_owned(monkeypatch, tmp_path, owner=None, ending=2222) == ["stop"]


def test_an_unidentifiable_ending_session_leaves_an_owned_sweep_alone(monkeypatch, tmp_path):
    """The opposite fallback to the one above, and deliberately so: here we know the
    sweep belongs to *somebody*, and guessing is what caused the bug. `--stop` and the
    sweep's own watchdog remain as backstops."""
    assert _session_end_owned(monkeypatch, tmp_path, owner=1111, ending=None) == []
    assert _session_end_owned(monkeypatch, tmp_path, owner=1111, ending="not-a-pid") == []


@pytest.mark.parametrize("payload", ["not json", "[]", "null", '"a string"'])
def test_an_unreadable_payload_leaves_the_sweep_alone(monkeypatch, payload):
    """Silence is the default, as in the permission guard: a broken payload must not
    be guessed at in the direction of killing something the user is watching."""
    assert _session_end(monkeypatch, payload) == []


# --- stop() must signal the sweeper itself, not only its children ----------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill path")
def test_stop_signals_the_sweeper_even_when_it_has_a_child(monkeypatch):
    """`pkill -P` signals the CHILDREN of a pid and never the pid itself.

    This used to gate the SIGTERM on pkill having *failed*, so whenever pkill
    succeeded — that is, whenever the sweep had a child — the sweeper survived and was
    reported as stopped. The sweep has a child in exactly one situation: while
    `claude -p` drives a browser through the apply phase. So the stop path failed at
    the one moment it mattered most.
    """
    import bootstrap

    monkeypatch.setattr(bootstrap.orchestration_status, "read", lambda _d: {"pid": 4321})
    monkeypatch.setattr(bootstrap.orchestration_status, "is_running", lambda _d: True)
    monkeypatch.setattr(bootstrap.orchestration_status, "clear", lambda _d: None)

    # returncode 0 == pkill found and signalled children.
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", ""),
    )
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(bootstrap.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert bootstrap.stop() == 0
    assert signalled == [(4321, bootstrap.signal.SIGTERM)], (
        "the recorded sweeper must be signalled even when pkill reached its children"
    )
