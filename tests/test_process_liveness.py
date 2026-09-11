"""Is a pid alive, and can `--stop` reach the sweep it names?

Most of what used to live here is gone with the machinery it covered. The sweep no
longer ties itself to a Claude Code session: there is no `SessionEnd` hook and no
`CLAUDE_PID` watchdog, because both needed a session identity the host does not always
publish, and on a host that did not publish it they reaped every sweep on the machine
instead of degrading quietly. See the note in `scripts/run_orchestration.py`.

`is_alive` survived that removal for one caller: the guard that refuses to start a
second sweeper onto the same SQLite database. It was never the thing that broke. It
answers correctly about a pid the plugin recorded **about itself**, and every failure in
this area came from feeding it an identity that had been guessed at instead — a shell's
`$$` from MSYS, which keeps its own pid namespace, and later `CLAUDE_PID`.
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
    """The assertion whose absence let an MSYS pid through. A pid that is definitely
    running must read as running, or the duplicate guard reports every live sweep as
    dead and cheerfully starts a second writer."""
    assert is_alive(os.getpid()) is True


def test_a_process_that_has_exited_is_not_alive():
    """The other half, and what makes a leftover pid file harmless: a sweep killed from
    Task Manager must not block the user's next sweep forever."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert is_alive(proc.pid) is False


@pytest.mark.parametrize("pid", [0, -1, -99999, None, "1234", 1.5, True, False])
def test_nonsense_never_raises(pid):
    """The caller is deciding whether to start a sweep. An exception there would refuse
    the user's run over a malformed file."""
    assert is_alive(pid) is False


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

    monkeypatch.setattr(bootstrap.sweep_pid, "read", lambda _d: 4321)
    monkeypatch.setattr(bootstrap.sweep_pid, "clear", lambda _d: None)

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


def test_stopping_with_nothing_recorded_is_not_an_error(monkeypatch):
    """`--stop` is the only deliberate way to end a sweep now, so the user will run it
    speculatively. "Nothing to stop" is an answer, not a failure."""
    import bootstrap

    monkeypatch.setattr(bootstrap.sweep_pid, "read", lambda _d: None)
    cleared: list[bool] = []
    monkeypatch.setattr(bootstrap.sweep_pid, "clear", lambda _d: cleared.append(True))

    assert bootstrap.stop() == 0
    assert cleared == [True], "a corrupt pid file should still be tidied away"
