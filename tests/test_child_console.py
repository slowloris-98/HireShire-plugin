"""Which children get their own console on Windows, and the two that must not.

A Windows child inherits its parent's console by default, and `USER32.dll`'s
initialisation attaches the process to that console's window station and desktop
*before any user code runs*. So when the console a long-lived parent inherited goes
stale -- the Claude Code session that started the background shell task ended or
restarted, its terminal was closed -- that attach fails and **every** subsequent spawn
dies with 0xC0000142 in ~20 ms, no stdout, no stderr, while the parent carries on
because it only does HTTP.

What the log showed, and what makes it diagnosable: it is per *sweeper process*, and an
event rather than an accumulation. One process scored cleanly at 23:25, 00:33 and 01:39,
then failed every cycle from 02:46 to 09:26 and never recovered; a restart cured it
instantly. Onset was 3h20m into that process and ~74 min into another. A census found
344 processes and zero orphaned children, so desktop heap, leaked processes, a locked
screen and sleep are all ruled out.

`CREATE_NO_WINDOW` fixes it by giving each child its own invisible console, which
grandchildren inherit. `DETACHED_PROCESS` is the wrong flag: with no console at all,
`npx` and `cmd` grandchildren may allocate **visible** ones on the user's desktop.

The rule these tests keep is in two halves, and the second is the dangerous one:

* every spawn with fully piped stdio asks for its own console, and
* the two re-exec launchers never do, because their child's stdout IS the sweep's only
  channel to the agent.

Getting the first wrong costs a dead child that logs an exit code. Getting the second
wrong costs silence.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from hireshire import claude_cli, paths

_SPAWNERS = {
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_output"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
}

#: Every shipped file that starts a process. Listed rather than globbed, so that putting
#: a spawn in a new module is a deliberate act that updates this list.
_FILES = (
    "hireshire/matcher/scorer.py",
    "hireshire/applier/worker.py",
    "hireshire/codex_cli.py",
    "scripts/bootstrap.py",
    "scripts/run_orchestration.py",
    "scripts/run_engine.py",
)

#: (file, enclosing function) for the spawns that MUST keep inheriting their stdio.
#: Keyed by function rather than line number, so an ordinary edit above them cannot
#: silently move the exemption onto a different call.
_INHERITS_STDIO = {
    ("run_orchestration.py", "_reexec_in_venv"),
    ("run_engine.py", "run"),
}


def _spawn_calls(path: Path):
    """Every process-spawning call in `path`, with its enclosing function name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(id(child), node.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if (func.value.id, func.attr) not in _SPAWNERS:
            continue
        yield node, enclosing.get(id(node), "<module>")


def _argv_literals(call: ast.Call) -> list[str]:
    """The string constants of the call's first argument, when it is a list literal."""
    if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
        return []
    return [e.value for e in call.args[0].elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def _asks_for_its_own_console(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "creationflags":
            return True
        # `**own_console_kwargs()` -- the double-star form every call site uses.
        if kw.arg is None and isinstance(kw.value, ast.Call):
            fn = kw.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "own_console_kwargs":
                return True
    return False


def _passes(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


# --- the helper itself ---------------------------------------------------------------


def test_a_piped_child_gets_its_own_console_only_on_windows(monkeypatch):
    """The platform test lives in one place, and POSIX gets a literal no-op.

    It has to be `{}` rather than `creationflags=0` on POSIX: `subprocess` rejects a
    non-zero `creationflags` there, and the flag it would be derived from does not even
    exist on a POSIX build of CPython.
    """
    monkeypatch.setattr(claude_cli.sys, "platform", "win32")
    assert claude_cli.own_console_kwargs() == {"creationflags": 0x08000000}
    for posix in ("darwin", "linux"):
        monkeypatch.setattr(claude_cli.sys, "platform", posix)
        assert claude_cli.own_console_kwargs() == {}


@pytest.mark.skipif(sys.platform != "win32", reason="the flag only exists on Windows")
def test_the_no_window_flag_matches_the_one_python_ships():
    """Pins the hand-copied constant against a typo, without making the suite
    Windows-only. It is spelled out rather than read off `subprocess` because that
    attribute does not exist off Windows -- the same reason `process_liveness.py` names
    `_SYNCHRONIZE` itself."""
    assert claude_cli._CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW


def test_the_stale_console_help_names_the_cause_and_the_cure():
    """One sentence, spelled once, so the scoring breaker and the applier breaker cannot
    drift. It must not blame a locked screen or sleep: the 21:05 sweep ran almost
    entirely locked and made ~30 apply sessions and 166 codex calls before failing, and
    there were no power events."""
    help_text = claude_cli.STALE_CONSOLE_HELP.lower()
    assert "console" in help_text
    assert "restart" in help_text
    assert "0xc0000142" in help_text
    assert "asleep" not in help_text and "locked" not in help_text


# --- the rule, over the shipped spawn sites ------------------------------------------


def test_every_piped_spawn_in_the_engine_asks_for_its_own_console():
    """No shipped spawn may inherit a console it did not ask for, except the two that
    must.

    This is the structural half of the fix. One missed call site is one more way for a
    sweep to go quiet for hours, and the failure is invisible until it happens on
    someone's machine -- so the guard is mechanical rather than a review habit.
    """
    missing = []
    for rel in _FILES:
        path = paths.ROOT / rel
        for call, func in _spawn_calls(path):
            if (path.name, func) in _INHERITS_STDIO:
                continue
            # The POSIX branches of the two kill paths; they sit inside an `else:`
            # already guarded on `sys.platform == "win32"`.
            if "pkill" in _argv_literals(call):
                continue
            if not _asks_for_its_own_console(call):
                missing.append(f"{rel}:{call.lineno} in {func}()")
    assert not missing, (
        "These spawns would inherit the parent's console, so they die with 0xC0000142 "
        "once that console goes stale. Pass `**claude_cli.own_console_kwargs()` -- "
        "unless the child's stdout is INHERITED rather than piped, in which case it "
        "belongs in _INHERITS_STDIO here and you need to read why that list exists:\n  "
        + "\n  ".join(missing)
    )


def test_nothing_in_the_relaunch_chain_gets_its_own_console():
    """The re-exec hops must keep inheriting stdio, and must not be quietly reclassified.

    `run_orchestration._reexec_in_venv` and `run_engine.run` pass no `stdout`/`stderr`
    on purpose: the child's one-line-per-cycle `print(..., flush=True)` is the only thing
    the background shell task surfaces to the agent, including the `auto-apply is OFF`
    line. Give either of them its own console and the sweep runs perfectly while the user
    sees nothing.

    Asserting they pipe NOTHING is what makes the exemption self-justifying: anyone who
    later pipes one of them fails here and has to deal with the summary-line rule, rather
    than just adding the flag because this file told them to.
    """
    seen = set()
    for rel in _FILES:
        path = paths.ROOT / rel
        for call, func in _spawn_calls(path):
            if (path.name, func) not in _INHERITS_STDIO:
                continue
            seen.add((path.name, func))
            assert not _asks_for_its_own_console(call), (
                f"{rel}:{call.lineno} in {func}() must keep inheriting the console, or "
                "the sweep's output stops reaching the agent"
            )
            for stream in ("stdout", "stderr"):
                assert not _passes(call, stream), (
                    f"{rel}:{call.lineno} in {func}() now pipes {stream}. If that is "
                    "deliberate, the sweep's per-cycle summary lines no longer reach the "
                    "agent -- fix that first, then reconsider this exemption."
                )
    assert seen == _INHERITS_STDIO, (
        f"the exemption list names spawns that no longer exist: {_INHERITS_STDIO - seen}"
    )
