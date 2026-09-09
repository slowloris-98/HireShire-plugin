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


def _watched(monkeypatch, **env):
    import run_orchestration

    for name in run_orchestration._SESSION_PID_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return run_orchestration._watched_session_pids()


def test_the_watchdog_is_inert_without_the_environment(monkeypatch):
    """Opt-in by construction. `hireshire.sh --monitor` is the only thing that sets
    these, so a sweep with no session behind it — the scheduled route, or a bare
    checkout — can never acquire one by accident."""
    assert _watched(monkeypatch) == {}


def test_both_session_pids_are_watched(monkeypatch):
    watched = _watched(monkeypatch, HIRESHIRE_CLI_PID="111", HIRESHIRE_SHELL_PID="222")
    assert watched == {"HIRESHIRE_CLI_PID": 111, "HIRESHIRE_SHELL_PID": 222}


@pytest.mark.parametrize("junk", ["", "   ", "not-a-pid", "0", "-5"])
def test_unusable_pids_are_skipped_rather_than_guessed(monkeypatch, junk):
    watched = _watched(monkeypatch, HIRESHIRE_CLI_PID=junk, HIRESHIRE_SHELL_PID="222")
    assert watched == {"HIRESHIRE_SHELL_PID": 222}


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
