"""How the apply skill is handed to the `claude` CLI.

One real run lost its entire apply phase to this. `_launch_skill` passed the SKILL.md
body as a positional argument, and a SKILL.md opens with `---` YAML frontmatter, which
the CLI parses as an option:

    error: unknown option '---\nname: apply...'

Every sweep exited 1 before opening a browser. Nothing surfaced it except a single
ERROR line in a 1.4 MB log, and `apply_enabled` stayed true in the status file, so the
user was told applying was on while it had never once run.

The fix is stdin, and the invariant worth pinning is the narrow one: **the prompt must
never appear in argv.** Asserting the exact flag list instead would break every time
someone adds a CLI option, and would not have caught this bug any earlier.
"""
from __future__ import annotations

import asyncio

import pytest

import orchestrate


class _FakeProc:
    returncode = 0

    def __init__(self) -> None:
        self.communicated: bytes | None = None

    async def communicate(self, payload: bytes | None = None):
        self.communicated = payload
        return b"", b""


@pytest.fixture
def launched(monkeypatch):
    """Run `_launch_skill('apply')` against a fake subprocess, capturing the call."""
    seen: dict = {}
    proc = _FakeProc()

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args
        seen["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_exec, raising=True
    )
    ok = asyncio.run(orchestrate._launch_skill("apply"))
    seen["ok"] = ok
    seen["proc"] = proc
    return seen


def test_the_skill_body_is_never_passed_as_an_argument(launched):
    """The regression. Frontmatter starts with `-`, so argv is the one place it
    cannot go."""
    for arg in launched["argv"]:
        assert not arg.startswith("---"), (
            f"skill body passed in argv as {arg[:40]!r} — the CLI reads a leading "
            "dash as an option and exits 1"
        )
    # Nothing long enough to be the skill body should be there at all.
    assert all(len(arg) < 200 for arg in launched["argv"])


def test_the_skill_body_reaches_the_process_on_stdin(launched):
    sent = launched["proc"].communicated
    assert sent is not None, "nothing was written to stdin"
    assert b"name: apply" in sent, "the SKILL.md body did not reach stdin"
    assert launched["kwargs"]["stdin"] is asyncio.subprocess.PIPE
    assert launched["ok"] is True


def test_the_subscription_is_used_rather_than_a_pay_as_you_go_key(launched, monkeypatch):
    """Load-bearing and easy to drop while editing the call: the CLI prefers an API
    key over the claude.ai login, which bills credits and dies with "Credit balance is
    too low" when they run out."""
    env = launched["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
