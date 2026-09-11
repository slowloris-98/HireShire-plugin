"""PreToolUse guard: auto-approve the plugin's own commands, and nothing else.

Claude Code prompts per *exact command string*, so a plugin that runs a dozen
different commands during setup asks a dozen times. Hooks are the supported way
out: a PreToolUse hook that prints `permissionDecision: "allow"` skips the prompt,
and plugin hooks need no permission of their own.

That makes this file a security boundary. Whatever it approves runs with no
prompt, ever, so it is written to say **no** by default:

* Silence is the normal answer. Anything unrecognised produces no output and the
  user is asked exactly as they are today.
* It never approves the launcher's bare `<script.py>` form. That form runs an
  arbitrary file, which is the whole game; `scripts/setup_cli.py` exists so that
  nothing legitimate needs it any more.
* It refuses any command carrying a shell operator, redirection or substitution,
  so an approved prefix cannot smuggle a second command behind `&&` or `;`.
* The launcher it approves must be *this* install's, matched by real path, not
  any file that happens to be named `hireshire.sh`.

Runs on the system interpreter from the SessionStart-style hook path, before the
venv exists, so it imports nothing outside the stdlib — the same rule bootstrap.py
follows.

Reads the hook payload on stdin, writes the allow object (or nothing) to stdout,
and **always exits 0**: a guard that fails closed by crashing would break every
Bash call in the session.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "hireshire.sh"

# Characters that end one command and start another, plus substitution. `shlex`
# in non-posix mode returns unquoted runs of `();<>|&` as their own tokens, so a
# job title like "Sales & Marketing Manager" stays one quoted token and passes,
# while a real `&&` does not.
_OPERATOR_CHARS = "();<>|&"
# `$` covers command substitution, variable expansion and process substitution in
# one rule. Nothing the plugin runs contains one — Claude Code expands
# `${CLAUDE_PLUGIN_ROOT}` in skill content before the agent reads it — so the only
# cost of the blunt rule is that an unusual payload prompts.
_SUBSTITUTION = ("`", "$")

# The launcher's read-only and lifecycle modes. Every one of these is a fixed
# command that installs nothing surprising: --bootstrap and --monitor do the work
# the user has just been told about, in the skill that told them.
_FLAG_MODES = frozenset({"--check", "--paths", "--bootstrap", "--monitor",
                         # --sweep is one cycle of --monitor and is what find-jobs runs.
                         # It has to be here or the plugin's most common command starts
                         # prompting on every use — which is the friction `setup_cli.py`
                         # and this guard exist to remove.
                         "--sweep",
                         # --stop is the only way to end a sweep on purpose now that
                         # nothing reaps one automatically, so it must not be the one
                         # command that prompts. It kills a process this plugin started,
                         # at the user's request, and the cost of being wrong is a sweep
                         # they restart — against a runaway they cannot stop.
                         "--stop"})

# Engine entrypoints, each with the arguments it may carry. A value of None means
# the first argument must be a known subcommand and the rest is data (a JSON
# payload, a file path) that argv delivers safely.
_SCRIPTS: dict[str, frozenset[str] | None] = {
    "orchestrate.py": frozenset({"--once"}),
    "scripts/verify_bad_slugs.py": frozenset({"--prune"}),
    # Read-only: reads past runs out of the database, prints a table, writes
    # nothing. Empty set means the bare form only — `--run-id` and `--recall` take
    # values, and allowing a flag that carries an argument means allowing the
    # argument too, which is a wider hole than a diagnostic is worth. Someone
    # narrowing the calibration to one run can answer one prompt.
    "scripts/calibrate_cutoffs.py": frozenset(),
    "scripts/setup_cli.py": None,
    "scripts/applied_cli.py": None,
}
_SUBCOMMANDS = {
    "scripts/setup_cli.py": frozenset({
        "install-config", "init-workspace", "find-resumes", "install-resume",
        "resume-text", "get", "field-docs", "set", "write-profile", "warm-models",
    }),
    "scripts/applied_cli.py": frozenset({"list", "record"}),
}

# Browser tools that only look. Everything that changes state on an employer's
# page — click, type, fill_form, select_option, file_upload — is deliberately
# absent: those actions submit a real application, and the prompt is the last human
# checkpoint before that happens. That mattered more once `dry_run` was removed:
# `enable_applier` is now the only other thing in the way, so do not widen this set.
_READ_ONLY_BROWSER_TOOLS = frozenset({
    "mcp__plugin_hireshire_playwright__browser_navigate",
    "mcp__plugin_hireshire_playwright__browser_snapshot",
    "mcp__plugin_hireshire_playwright__browser_take_screenshot",
})

_SHELLS = frozenset({"sh", "bash", "sh.exe", "bash.exe"})


def _tokenize(command: str) -> list[str] | None:
    """Split a command line, keeping quotes attached. None if it will not parse.

    Non-posix mode on purpose. Posix mode treats a backslash as an escape, which
    mangles the Windows paths `${CLAUDE_PLUGIN_ROOT}` expands to, and stripping
    quotes here would lose the one signal that separates a literal `&` inside a
    job title from an operator between two commands.
    """
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None  # unbalanced quote; let the user look at it


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in _OPERATOR_CHARS for c in token)


def _spellings(path: str) -> list[str]:
    """The ways one path can be written on this platform.

    Windows needs this. The plugin's shell commands run under Git Bash, which
    speaks MSYS paths (`/d/Atreya/...`), while `${CLAUDE_PLUGIN_ROOT}` expands to a
    native one (`C:/Users/...`) and `Path.resolve()` produces a third
    (`C:\\Users\\...`). Windows Python cannot resolve the MSYS form at all, so
    comparing without translating it means the guard silently never matches and the
    prompts this file exists to remove all come back.
    """
    out = [path]
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == "/":
        drive = path[1]
        if drive.isalpha():
            out.append(f"{drive}:{path[2:]}")
    return out


def _same_file(a: str, b: Path) -> bool:
    """Compare paths without requiring either to exist on a case-sensitive FS."""
    try:
        want = os.path.normcase(os.path.realpath(b))
        return any(os.path.normcase(os.path.realpath(s)) == want for s in _spellings(a))
    except (OSError, ValueError):
        return False


def _join_continuations(command: str) -> str:
    """Fold `\\` line continuations into spaces.

    A continuation joins one command across lines; a bare newline separates two.
    The skills wrap their longer invocations for legibility, so the two have to be
    told apart rather than both refused.
    """
    return re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", command)


def _bash_is_allowed(command: str) -> bool:
    command = _join_continuations(command)
    # Any newline left is a second command, not a wrapped one.
    if not command or "\n" in command or "\r" in command:
        return False

    tokens = _tokenize(command)
    if not tokens or len(tokens) < 3:
        return False

    for token in tokens:
        if _is_operator(token):
            return False
        if any(marker in token for marker in _SUBSTITUTION):
            return False

    argv = [_unquote(t) for t in tokens]
    if Path(argv[0]).name.lower() not in _SHELLS:
        return False
    if not _same_file(argv[1], LAUNCHER):
        return False

    mode, rest = argv[2], argv[3:]
    if mode in _FLAG_MODES:
        return not rest

    allowed_args = _SCRIPTS.get(mode)
    if mode not in _SCRIPTS:
        # Includes the bare `<script.py>` form, which runs an arbitrary file.
        return False
    if allowed_args is not None:
        return all(arg in allowed_args for arg in rest)

    subcommands = _SUBCOMMANDS[mode]
    return bool(rest) and rest[0] in subcommands


def decide(payload: dict) -> str | None:
    """The reason to allow this call, or None to stay silent and let it prompt."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    if tool in ("Bash", "PowerShell"):
        # PowerShell is listed so the guard is not silently bypassed by running the
        # same launcher through the other shell tool; the parsing rules below are
        # POSIX, so it is checked with them and simply fails closed if it differs.
        if _bash_is_allowed(str(tool_input.get("command") or "")):
            return "HireShire launcher command, checked by scripts/approve.py"
        return None

    if tool in _READ_ONLY_BROWSER_TOOLS:
        return "read-only browser inspection for /hireshire:apply"

    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:  # noqa: BLE001 - a guard must never break the session
        return 0

    if reason is None:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
