"""The `codex` judge: scoring on a ChatGPT plan through `codex exec`.

Every rule pinned here was learned by probing codex-cli 0.154.0, and each fails
silently if it regresses:

  * `--output-schema` is a PATH to a schema in OpenAI's strict dialect — a raw
    pydantic schema is rejected with HTTP 400 on every call;
  * the answer is the LAST `agent_message` (openai/codex#19816), and an item-level
    `error` event is not a failure;
  * the judge is not an agent: tools, skills and environment context are stripped
    (11,207 → 1,769 input tokens on one call), and the sandbox is read-only;
  * API keys are stripped so the ChatGPT plan pays, never a metered key;
  * OpenAI counts cached tokens inside `input_tokens`, and there is no price.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hireshire import codex_cli, config_writer, paths
from hireshire.matcher import scorer as scorer_mod
from hireshire.matcher.config import MatcherSettings
from hireshire.matcher.scorer import (
    CLILaunchError,
    CodexBackend,
    ScoringSchema,
    UsageTally,
    make_backend,
)

_ANSWER = {
    "requirements": [],
    "core_skills_rationale": "ok", "core_skills_band": 5,
    "experience_rationale": "ok", "experience_band": 4,
    "education_rationale": "ok", "education_band": 1,
    "match_reasons": [], "disqualifiers": [], "recommend": True,
}
_USAGE = {"input_tokens": 3000, "cached_input_tokens": 2500, "cache_write_input_tokens": 0,
          "output_tokens": 400, "reasoning_output_tokens": 0}


def _events(*events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _ok_stream(answer=_ANSWER) -> bytes:
    return _events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                            "message": "Exceeded skills context budget."}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                            "text": json.dumps(answer)}},
        {"type": "turn.completed", "usage": _USAGE},
    )


@pytest.fixture
def codex_dir(tmp_path, monkeypatch):
    d = tmp_path / "codex"
    monkeypatch.setattr(paths, "CODEX_DIR", d)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(codex_cli, "available_features",
                        lambda: set(codex_cli.JUDGE_DISABLED_FEATURES))
    return d


def _settings(**kw):
    kw.setdefault("provider", "codex")
    kw.setdefault("model", "gpt-5.6-luna")
    kw.setdefault("request_interval_s", 0)
    return MatcherSettings(**kw)


def _patch_exec(monkeypatch, captured, results):
    """`results` is a list of (returncode, stdout) played back in order."""
    results = list(results)

    async def fake_exec(*argv, **kwargs):
        captured.setdefault("calls", []).append({"argv": argv, "env": kwargs.get("env")})
        rc, out = results.pop(0)

        class _Proc:
            returncode = rc

            async def communicate(self, input=None):
                captured["stdin"] = input
                return out, b""

            def kill(self):
                pass

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _score(monkeypatch, results, **settings):
    captured: dict = {}
    _patch_exec(monkeypatch, captured, results)
    backend = CodexBackend(_settings(**settings), asyncio.Semaphore(1))
    result = asyncio.run(backend.call("<posting>job</posting>", "RUBRIC + RESUME"))
    return backend, result, captured


# --- argv -------------------------------------------------------------------

def test_the_call_is_a_headless_read_only_exec_with_the_schema_by_path(monkeypatch, codex_dir):
    _, result, captured = _score(monkeypatch, [(0, _ok_stream())])
    argv = list(captured["calls"][0]["argv"])

    assert isinstance(result, ScoringSchema) and result.core_skills_band == 5
    assert argv[1] == "exec" and argv[-1] == "-"
    for flag in ("--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in argv
    # A path, not inline JSON: `--output-schema` reads a file.
    schema_path = argv[argv.index("--output-schema") + 1]
    assert json.loads(open(schema_path, encoding="utf-8").read())["type"] == "object"
    assert not any(a.startswith("--dangerously") or a == "--full-auto" for a in argv)
    # The posting goes on stdin.
    assert captured["stdin"] == b"<posting>job</posting>"


def test_the_judge_runs_with_no_tools_and_no_injected_context(monkeypatch, codex_dir):
    _, _, captured = _score(monkeypatch, [(0, _ok_stream())])
    argv = list(captured["calls"][0]["argv"])

    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "multi_agent", "apps", "plugins"} <= disabled
    for override in ("project_doc_max_bytes=0", "include_environment_context=false",
                     "agents.enabled=false", 'web_search="disabled"'):
        assert override in argv
    # The working root is the judge's own empty folder, never the user's project.
    assert argv[argv.index("-C") + 1] == str(codex_dir)


def test_the_system_prompt_replaces_codex_instructions_via_a_file(monkeypatch, codex_dir):
    _, _, captured = _score(monkeypatch, [(0, _ok_stream())])
    argv = list(captured["calls"][0]["argv"])

    override = next(a for a in argv if a.startswith("model_instructions_file="))
    value = override.split("=", 1)[1]
    # A TOML literal string with forward slashes, so a Windows path parses.
    assert value.startswith("'") and value.endswith("'") and "\\" not in value
    assert open(value.strip("'"), encoding="utf-8").read() == "RUBRIC + RESUME"


def test_one_instructions_file_per_distinct_system_prompt(monkeypatch, codex_dir):
    captured: dict = {}
    _patch_exec(monkeypatch, captured, [(0, _ok_stream())] * 3)
    backend = CodexBackend(_settings(), asyncio.Semaphore(1))
    for system in ("A", "A", "B"):
        asyncio.run(backend.call("p", system))

    assert len(list(codex_dir.glob("instructions-*.md"))) == 2


def test_a_feature_this_cli_does_not_know_is_not_disabled(monkeypatch, codex_dir):
    """`--disable` with an unknown name is an error, which would fail every call."""
    monkeypatch.setattr(codex_cli, "available_features", lambda: {"shell_tool"})
    _, _, captured = _score(monkeypatch, [(0, _ok_stream())])
    argv = list(captured["calls"][0]["argv"])

    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"] == ["shell_tool"]


def test_api_keys_are_stripped_so_the_chatgpt_plan_pays(monkeypatch, codex_dir):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CODEX_API_KEY", "sk-y")
    _, _, captured = _score(monkeypatch, [(0, _ok_stream())])
    env = captured["calls"][0]["env"]

    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env


# --- the schema ---------------------------------------------------------------

def test_the_schema_is_in_openai_strict_dialect():
    strict = codex_cli.strict_schema(ScoringSchema.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert "default" not in node and "maxLength" not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(strict)
    assert "RequirementCheck" in strict["$defs"]


# --- reading the answer ---------------------------------------------------------

def test_the_last_agent_message_is_the_answer():
    """#19816: the schema also shapes intermediate messages, so the first
    schema-shaped message is not necessarily the answer."""
    early = dict(_ANSWER, core_skills_band=0)
    stream = _events(
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(early)}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_ANSWER)}},
        {"type": "turn.completed", "usage": _USAGE},
    )
    answer, usage, error = codex_cli.parse_events(b"not json\n" + stream)

    assert json.loads(answer)["core_skills_band"] == 5
    assert usage == _USAGE and error is None


def test_an_item_level_error_is_not_a_failure():
    answer, _, error = codex_cli.parse_events(_ok_stream())
    assert answer is not None and error is None


def test_a_failed_turn_is_an_api_error_carrying_its_message(monkeypatch, codex_dir):
    stream = _events({"type": "turn.failed", "error": {"message": "model not supported"}})
    captured: dict = {}
    _patch_exec(monkeypatch, captured, [(1, stream)])
    backend = CodexBackend(_settings(), asyncio.Semaphore(1))

    with pytest.raises(RuntimeError, match="model not supported"):
        asyncio.run(backend.call("p", "s"))


def test_an_ordinary_failure_is_not_retried(monkeypatch, codex_dir):
    captured: dict = {}
    _patch_exec(monkeypatch, captured, [(1, b"")])
    backend = CodexBackend(_settings(), asyncio.Semaphore(1))

    with pytest.raises(RuntimeError):
        asyncio.run(backend.call("p", "s"))
    assert len(captured["calls"]) == 1


def test_a_launch_failure_is_retried(monkeypatch, codex_dir):
    monkeypatch.setattr(scorer_mod, "_LAUNCH_RETRY_DELAYS_S", (0, 0, 0))
    _, result, captured = _score(monkeypatch, [(0xC0000142, b""), (0, _ok_stream())])

    assert len(captured["calls"]) == 2 and result.recommend is True


def test_launch_failures_that_never_clear_raise_the_launch_type(monkeypatch, codex_dir):
    monkeypatch.setattr(scorer_mod, "_LAUNCH_RETRY_DELAYS_S", (0, 0, 0))
    captured: dict = {}
    _patch_exec(monkeypatch, captured, [(0xC0000142, b"")] * 4)
    backend = CodexBackend(_settings(), asyncio.Semaphore(1))

    with pytest.raises(CLILaunchError):
        asyncio.run(backend.call("p", "s"))


# --- usage -----------------------------------------------------------------------

def test_usage_moves_cached_tokens_out_of_input_and_has_no_price(monkeypatch, codex_dir):
    backend, _, _ = _score(monkeypatch, [(0, _ok_stream())])
    usage = backend.usage

    assert (usage.calls, usage.input, usage.cache_read, usage.output) == (1, 500, 2500, 400)
    assert usage.cost_usd is None
    assert "$" not in usage.summary()
    assert usage.as_dict()["cost_usd"] is None


def test_a_call_with_an_unparseable_answer_is_still_metered(monkeypatch, codex_dir):
    stream = _events(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "not json"}},
        {"type": "turn.completed", "usage": _USAGE},
    )
    captured: dict = {}
    _patch_exec(monkeypatch, captured, [(0, stream)])
    backend = CodexBackend(_settings(), asyncio.Semaphore(1))

    with pytest.raises(RuntimeError, match="not JSON"):
        asyncio.run(backend.call("p", "s"))
    assert backend.usage.calls == 1


@pytest.mark.parametrize("caches, warns", [(True, True), (False, False)])
def test_the_no_cache_warning_only_fires_where_caching_is_possible(caplog, caches, warns):
    """Separate `codex exec` calls cannot share a cache (openai/codex#21796), so the
    warning — which blames a leak into the system prompt — would be wrong there."""
    import logging

    import matcher as matcher_mod

    tally = UsageTally(priced=caches, caches=caches)
    tally.record_codex({"input_tokens": 3000})
    tally.record_codex({"input_tokens": 3000})
    with caplog.at_level(logging.WARNING):
        matcher_mod._log_usage(type("S", (), {"usage": tally})(), quiet=True)

    assert ("served from cache" in caplog.text) is warns


def test_the_claude_tally_still_prices():
    tally = UsageTally()
    tally.record({"total_cost_usd": 0.5, "usage": {}})
    assert "$0.50" in tally.summary()


# --- configuration -----------------------------------------------------------------

@pytest.mark.parametrize("model", ["sonnet", "claude-sonnet-5", "opus", ""])
def test_a_claude_model_name_is_refused_up_front(codex_dir, model):
    with pytest.raises(ValueError, match="not a Codex model"):
        CodexBackend(_settings(model=model), asyncio.Semaphore(1))


def test_a_missing_cli_names_the_fix(monkeypatch, codex_dir):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(EnvironmentError, match="codex login"):
        CodexBackend(_settings(), asyncio.Semaphore(1))


def test_the_factory_and_setup_both_know_codex(codex_dir):
    assert isinstance(make_backend(_settings(), asyncio.Semaphore(1)), CodexBackend)
    assert "codex" in config_writer.PROVIDERS


def test_the_setup_check_lists_only_listed_models_and_judge_efforts():
    catalog = {"models": [
        {"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "max", "ultra")]},
        {"slug": "hidden", "visibility": "hide", "supported_reasoning_levels": []},
    ]}
    models = codex_cli._listed_models(json.dumps(catalog).encode())

    assert models == [{"model": "gpt-a", "name": "GPT-A", "efforts": ["low", "max"]}]
