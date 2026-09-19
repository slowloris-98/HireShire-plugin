"""How a sweep ends, now that nothing watches a session to end it.

The previous answer was two mechanisms that both needed `CLAUDE_PID`: a watchdog inside
the sweep and a `SessionEnd` hook outside it. On a fresh Windows machine that variable
was simply absent, and they did not degrade — the watchdog never armed, and the hook's
null-owner fallback let every ending Claude Code session reap the sweep. Since the sweep
spawns a `claude -p` per scoring call, it manufactured its own killers and died on the
first one: 60 s in, exit code 1, no traceback, on every sweep path including `--once`.

So a sweep now ends in exactly two ways: `--stop`, or its process being killed. A
24-hour runtime bound used to be a third, and it was removed because it stopped sweeps
the user wanted running. These tests keep both decisions in place: no session
dependency, and no bound.
"""
from __future__ import annotations

import ast
import sys

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import run_orchestration  # noqa: E402


def _executable_source(path) -> str:
    """The file with comments and docstrings removed.

    Both are stripped because this file documents the bug at length on purpose, and the
    assertion is about what the module *runs*, not what it explains. Docstrings need the
    AST rather than a startswith check: the prose that names `CLAUDE_PID` sits in the
    middle of a triple-quoted block, where no line begins with a quote.
    """
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for i in range(first.lineno - 1, first.end_lineno):
                lines[i] = ""
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


# --- no runtime bound ---------------------------------------------------------------


def test_the_recurring_sweep_has_no_runtime_bound():
    """A recurring sweep runs until `--stop`. The 24-hour cap it used to have ended
    sweeps the user wanted running, and that is the failure this test prevents."""
    for removed in ("_MAX_RUNTIME_S", "_another_cycle_fits"):
        assert not hasattr(run_orchestration, removed), (
            f"{removed} is back: the recurring sweep must run until --stop"
        )


# --- no session dependency, ever again ---------------------------------------------


def test_the_sweep_never_reads_a_session_pid():
    """This file must not learn about Claude Code sessions again.

    Mirrors `test_the_launcher_never_passes_a_shell_pid_to_the_watchdog`: that one keeps
    MSYS pids out of the launcher, this one keeps session identity out of the sweep. Both
    failures had the same shape — an identity the plugin could not verify, trusted to
    decide whether to destroy the user's work.
    """
    code = _executable_source(paths.ROOT / "scripts" / "run_orchestration.py")
    for forbidden in ("CLAUDE_PID", "HIRESHIRE_CLI_PID", "HIRESHIRE_SHELL_PID"):
        assert forbidden not in code, (
            f"{forbidden} must not reach the sweep: it is not published on every host, "
            "and its absence used to read as 'stop every sweep on this machine'"
        )
