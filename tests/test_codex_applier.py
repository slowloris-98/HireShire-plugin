"""The `codex` applier: driving the browser on a ChatGPT plan through `codex exec`.

Every rule pinned here was learned by probing codex-cli 0.157.0 against a real local
form, and each one fails in a way the user pays for:

  * MCP tool calls are DENIED under `approval_policy="never"` alone — the browser never
    moves — so `default_tools_approval_mode="approve"` is not optional, and `"auto"`
    does not do it;
  * the sandbox is `workspace-write`, not the judge's `read-only`, which blocked even a
    `file://` navigation, and the working root is where the screenshots go;
  * Codex exposes MCP tools under their BARE names, so the prompt's tool naming has to
    follow the provider or the session is told to call tools it does not have;
  * the answer is the LAST `agent_message`, and here that is load-bearing: a real run
    emitted three premature `{"status":"submitted","screenshot":null}` messages before
    the form had been touched;
  * a clean exit whose answer cannot be read is a VERDICT, not a deferral, because the
    form may already be submitted — the opposite of the judge's mapping;
  * a non-zero exit IS a deferral, so a logged-out CLI costs no shortlist;
  * API keys are stripped so the ChatGPT plan pays, never a metered key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hireshire import codex_cli, paths
from hireshire.applier import sessions, worker
from hireshire.applier.config import ApplierSettings
from hireshire.applier.sessions import (
    ClaudeApplySession,
    CodexApplySession,
    UnreadableResult,
    make_session,
)

_OUTCOME = {"status": "submitted", "screenshot": "C:/ws/run/applied/acme-j1.png",
            "error": None, "location": None}


def _events(*events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _stream(*answers, usage=True, error=None) -> bytes:
    """An event stream shaped like a real run: the routine skills-budget `error` item,
    then one `agent_message` per answer, then the turn."""
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "error",
                                            "message": "Exceeded skills context budget."}},
    ]
    for i, a in enumerate(answers):
        events.append({"type": "item.completed",
                       "item": {"id": f"i{i + 1}", "type": "agent_message",
                                "text": json.dumps(a) if not isinstance(a, str) else a}})
    if error:
        events.append({"type": "turn.failed", "error": {"message": error}})
    elif usage:
        events.append({"type": "turn.completed",
                       "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 5,
                                 "reasoning_output_tokens": 0}})
    return _events(*events)


@pytest.fixture
def codex_dir(tmp_path, monkeypatch):
    d = tmp_path / "codex"
    monkeypatch.setattr(paths, "CODEX_DIR", d)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(codex_cli, "available_features",
                        lambda: set(codex_cli.APPLY_DISABLED_FEATURES))
    return d


def _settings(**kw) -> ApplierSettings:
    base = dict(provider="codex", model="gpt-5.6-terra", effort="low")
    base.update(kw)
    return ApplierSettings(**base)


def _dirs(tmp_path) -> worker.SessionDirs:
    out = tmp_path / "run" / "applied"
    out.mkdir(parents=True, exist_ok=True)
    return worker.SessionDirs(cwd=tmp_path, out_dir=out,
                              resume_path=tmp_path / "resume.pdf")


def _argv(tmp_path, codex_dir, **kw) -> list[str]:
    dirs = _dirs(tmp_path)
    session = CodexApplySession(_settings(**kw))
    return session.argv(dirs.out_dir / ".browser" / "j1", dirs)


def _pairs(argv: list[str]) -> dict[str, str]:
    """The `-c key=value` overrides, as a dict."""
    out = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "-c" and "=" in value:
            key, _, val = value.partition("=")
            out[key] = val
    return out


# --- the configuration that makes the browser move at all --------------------------

def test_the_browser_tools_are_pre_approved_or_every_call_is_denied(tmp_path, codex_dir):
    # `approval_policy="never"` on its own answers every MCP tool call with
    # "MCP tool call requires approval, but approval policy is never".
    pairs = _pairs(_argv(tmp_path, codex_dir))
    assert pairs["approval_policy"] == '"never"'
    assert pairs["mcp_servers.playwright.default_tools_approval_mode"] == '"approve"'


def test_the_apply_session_is_not_sandboxed_read_only_like_the_judge(tmp_path, codex_dir):
    # Under read-only, `browser_navigate` failed outright: Codex's sandbox reaches the
    # MCP server's operations, not just its own shell.
    argv = _argv(tmp_path, codex_dir)
    assert argv[argv.index("-s") + 1] == "workspace-write"


def test_the_codex_session_runs_where_the_screenshots_go(tmp_path, codex_dir):
    # `-C` is both Codex's working root and `workspace-write`'s writable root, and
    # `session_dirs` guarantees cwd contains out_dir — so the screenshot path is inside
    # the writable root by construction. CODEX_DIR would put it outside.
    argv = _argv(tmp_path, codex_dir)
    assert argv[argv.index("-C") + 1] == str(_dirs(tmp_path).cwd)
    assert str(paths.CODEX_DIR) != argv[argv.index("-C") + 1]


def test_the_codex_session_reaches_the_browser_through_the_shipped_mcp_config(
        tmp_path, codex_dir):
    shipped = json.loads((paths.ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = shipped["mcpServers"]["playwright"]
    pairs = _pairs(_argv(tmp_path, codex_dir))
    assert pairs["mcp_servers.playwright.command"] == codex_cli.toml_path(server["command"])
    args = pairs["mcp_servers.playwright.args"]
    for shipped_arg in server["args"]:
        assert codex_cli.toml_path(shipped_arg) in args
    # This job's scratch dir, as a TOML literal: forward slashes, single-quoted, or a
    # Windows path would fail to parse as a basic string.
    scratch = _dirs(tmp_path).out_dir / ".browser" / "j1"
    assert codex_cli.toml_path(scratch) in args
    assert "\\" not in args


def test_the_codex_session_keeps_codexs_own_browser_switched_off(tmp_path, codex_dir):
    # Codex's own browser would honour neither `--output-dir` nor the roots rule, so a
    # session holding two browsers could screenshot a page that is not the form filled.
    argv = _argv(tmp_path, codex_dir)
    disabled = {v for f, v in zip(argv, argv[1:]) if f == "--disable"}
    assert {"browser_use", "computer_use", "shell_tool", "unified_exec"} <= disabled


def test_an_unknown_feature_name_is_not_sent_to_this_cli(tmp_path, monkeypatch):
    # `--disable` with a name the CLI does not know fails the call, so a feature renamed
    # in a later release must not break every application.
    monkeypatch.setattr(paths, "CODEX_DIR", tmp_path / "codex")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(codex_cli, "available_features", lambda: {"shell_tool"})
    argv = CodexApplySession(_settings()).argv(tmp_path, _dirs(tmp_path))
    assert {v for f, v in zip(argv, argv[1:]) if f == "--disable"} == {"shell_tool"}


def test_the_apply_schema_goes_to_codex_as_a_path_in_the_strict_dialect(
        tmp_path, codex_dir):
    argv = _argv(tmp_path, codex_dir)
    path = Path(argv[argv.index("--output-schema") + 1])
    assert path.parent == paths.CODEX_DIR      # DATA, so it survives an update
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "screenshot", "error", "location"}
    assert "default" not in json.dumps(schema)


def test_the_prompt_goes_on_stdin_not_argv(tmp_path, codex_dir):
    assert _argv(tmp_path, codex_dir)[-1] == "-"


def test_the_apply_session_never_bills_an_api_key(tmp_path, codex_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-passed")
    monkeypatch.setenv("CODEX_API_KEY", "sk-nor-this")
    env = CodexApplySession(_settings()).env()
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env


# --- reading the answer back --------------------------------------------------------

def test_the_codex_outcome_is_read_off_the_last_agent_message(tmp_path, codex_dir):
    # A real run emitted three premature `submitted` messages with a null screenshot
    # before the form was touched; reading the first would record an application for a
    # form nobody had filled.
    premature = {"status": "submitted", "screenshot": None, "error": None,
                 "location": None}
    outcome = CodexApplySession(_settings()).parse(
        _stream(premature, premature, premature, _OUTCOME))
    assert outcome.status == "submitted"
    assert outcome.screenshot == _OUTCOME["screenshot"]


def test_an_item_level_error_event_is_not_a_failed_application(tmp_path, codex_dir):
    # "Exceeded skills context budget" arrives on every call; `_stream` always includes
    # it, so a readable outcome here is the assertion.
    assert CodexApplySession(_settings()).parse(_stream(_OUTCOME)).status == "submitted"


def test_a_failed_codex_turn_is_unreadable_not_a_launch_failure(tmp_path, codex_dir):
    # The mapping that deliberately differs from `CodexBackend`'s: a session that drove
    # a form for ten minutes and then failed its turn may already have submitted it, so
    # it must not go back to the backlog for a second application.
    with pytest.raises(UnreadableResult):
        CodexApplySession(_settings()).parse(_stream(error="rate limited"))


def test_an_answer_that_is_not_an_outcome_is_unreadable(tmp_path, codex_dir):
    session = CodexApplySession(_settings())
    with pytest.raises(UnreadableResult):
        session.parse(_stream("I could not find the form."))
    with pytest.raises(UnreadableResult):
        session.parse(_stream({"status": "vanished"}))


def test_the_exit_detail_reports_the_reason_codex_gave(tmp_path, codex_dir):
    detail = CodexApplySession(_settings()).exit_detail(
        _stream(error="not logged in"), b"", 1)
    assert "not logged in" in detail


# --- which session runs, and what happens when none can ----------------------------

def test_an_unset_provider_is_the_claude_session():
    # Empty means claude_code, so an install predating this keeps what it had.
    assert isinstance(make_session(ApplierSettings()), ClaudeApplySession)
    assert isinstance(make_session(ApplierSettings(provider="claude_code")),
                      ClaudeApplySession)


def test_the_two_sessions_name_the_browser_tools_differently():
    # Claude Code namespaces MCP tools; Codex exposes the bare names. One placeholder in
    # `apply_one.md` carries the difference, so there is no second copy of those rules.
    assert ClaudeApplySession(ApplierSettings()).tool_prefix == "mcp__playwright__"
    assert CodexApplySession.tool_prefix == ""


def test_a_claude_model_name_is_refused_for_the_codex_applier(tmp_path, codex_dir):
    for name in ("sonnet", "claude-sonnet-5", "opus", ""):
        with pytest.raises(ValueError):
            make_session(_settings(model=name))


def test_a_missing_codex_cli_is_refused_rather_than_guessed_at(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(EnvironmentError):
        make_session(_settings())


def test_an_unknown_provider_cannot_be_written_or_built():
    # The pydantic validator rejects a typo, so "unset" and "misspelled" never collapse
    # into each other; `make_session` refuses one that reaches it another way.
    with pytest.raises(Exception):
        ApplierSettings(provider="openai")
    with pytest.raises(ValueError):
        make_session(ApplierSettings.model_construct(provider="openai"))


def test_there_is_no_failover_between_the_two_clis():
    # Mirrors `matcher.make_backend`: one provider, one session, no retry on the other.
    # A second browser session for one job risks a second application.
    assert set(sessions._SESSIONS) == {"claude_code", "codex"}
    # And the choice comes from config alone: no environment variable may move the
    # browser onto another CLI the way `matcher.provider` falls back to `LLM_PROVIDER`.
    src = Path(sessions.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "os.environ" not in code and "getenv" not in code
