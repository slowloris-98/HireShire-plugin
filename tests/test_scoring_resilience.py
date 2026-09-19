"""Guards on what happens when the scoring backend is broken.

A misused `--json-schema` argument once failed all 100 scoring calls in a real sweep.
Three things went wrong at once and each is pinned here: the flag was wrong, the
failures permanently retired every job they touched, and the run still reported
"0 new matches" as though the jobs had simply not been good enough.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import matcher as matcher_mod
from hireshire import claude_cli
from hireshire.matcher import scorer as scorer_mod
from hireshire.matcher.config import MatcherSettings
from hireshire.matcher.scorer import (
    SCORING_ERROR_SKIP_REASONS,
    ClaudeCodeBackend,
    CLILaunchError,
    MatchResult,
    ScoringSchema,
)
from hireshire.matcher.seen import SeenStore
from hireshire.storage.db import Database


def _result(job_id: str, skip_reason: str | None, score: int = 0) -> MatchResult:
    return MatchResult(
        job_id=job_id,
        board_token="acme",
        title="Account Manager",
        location="Remote",
        absolute_url="https://example.com/jobs/" + job_id,
        relevance_score=score,
        match_reasons=[],
        disqualifiers=[],
        recommend=False,
        skipped=skip_reason is not None,
        skip_reason=skip_reason,
        scored_at=datetime.now(timezone.utc),
        source_run_id="run-1",
    )


# --- the flag itself -------------------------------------------------------

def test_the_cli_receives_the_schema_itself_not_a_path(monkeypatch):
    """`--json-schema` parses its argument as JSON. Passing a filename failed every
    call with `Unexpected identifier "C"` — the drive letter of C:\\Users\\..."""
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv

        class _Proc:
            returncode = 0

            async def communicate(self, input=None):
                payload = ScoringSchema(
                    requirements=[],
                    core_skills_rationale="ok", core_skills_band=5,
                    experience_rationale="ok", experience_band=4,
                    education_rationale="ok", education_band=1,
                    match_reasons=[], disqualifiers=[], recommend=True,
                ).model_dump_json()
                return json.dumps({"result": payload}).encode(), b""

        return _Proc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    settings = MatcherSettings(request_interval_s=0)
    backend = ClaudeCodeBackend(settings, asyncio.Semaphore(1))
    asyncio.run(backend.call("prompt", "system"))

    argv = list(captured["argv"])
    value = argv[argv.index("--json-schema") + 1]
    assert json.loads(value)["type"] == "object", "the CLI must get the schema, not a path"


_DLL_INIT_FAILED = 3221225794  # 0xC0000142, as Windows reports it


def _scripted_backend(monkeypatch, exit_codes: list[int]):
    """A backend whose CLI exits with each code in turn, then succeeds."""
    calls: list[int] = []

    async def fake_exec(*argv, **kwargs):
        rc = exit_codes[len(calls)] if len(calls) < len(exit_codes) else 0
        calls.append(rc)

        class _Proc:
            returncode = rc

            async def communicate(self, input=None):
                if rc:
                    return b"", b""
                payload = ScoringSchema(
                    requirements=[],
                    core_skills_rationale="ok", core_skills_band=5,
                    experience_rationale="ok", experience_band=4,
                    education_rationale="ok", education_band=1,
                    match_reasons=[], disqualifiers=[], recommend=True,
                ).model_dump_json()
                return json.dumps({"result": payload}).encode(), b""

        return _Proc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(scorer_mod, "_LAUNCH_RETRY_DELAYS_S", (0, 0, 0))
    backend = ClaudeCodeBackend(MatcherSettings(request_interval_s=0), asyncio.Semaphore(1))
    return backend, calls


def test_a_launch_failure_is_retried(monkeypatch):
    """The host refusing to start claude.exe is not an answer from the backend."""
    backend, calls = _scripted_backend(monkeypatch, [_DLL_INIT_FAILED] * 2)
    result = asyncio.run(backend.call("prompt", "system"))
    assert isinstance(result, ScoringSchema)
    assert len(calls) == 3


def test_a_persistent_launch_failure_gives_up_and_says_why(monkeypatch):
    backend, calls = _scripted_backend(monkeypatch, [_DLL_INIT_FAILED] * 10)
    with pytest.raises(CLILaunchError, match="0xC0000142"):
        asyncio.run(backend.call("prompt", "system"))
    assert len(calls) == len(scorer_mod._LAUNCH_RETRY_DELAYS_S) + 1


def test_an_ordinary_exit_is_never_retried(monkeypatch):
    """Exit 1 carries the API's own answer; asking again changes nothing."""
    backend, calls = _scripted_backend(monkeypatch, [1] * 10)
    with pytest.raises(RuntimeError, match="exited 1") as info:
        asyncio.run(backend.call("prompt", "system"))
    assert not isinstance(info.value, CLILaunchError)
    assert len(calls) == 1


@pytest.mark.parametrize("rc", [3221225794, -1073741502])
def test_both_spellings_of_the_launch_failure_are_recognised(rc):
    assert claude_cli.is_launch_failure(rc)
    assert "0xC0000142" in claude_cli.describe_exit(rc)


def test_ordinary_exit_codes_are_described_as_themselves():
    assert not claude_cli.is_launch_failure(1)
    assert not claude_cli.is_launch_failure(None)
    assert claude_cli.describe_exit(1) == "1"


def test_the_schema_stays_well_inside_the_argv_limit():
    """The schema rides on argv now. Windows caps a command line at 32,767 chars;
    stay an order of magnitude clear of it."""
    assert len(json.dumps(ScoringSchema.model_json_schema())) < 8000


# --- errors must not retire jobs -------------------------------------------

@pytest.mark.parametrize("reason", sorted(SCORING_ERROR_SKIP_REASONS))
def test_a_scoring_failure_does_not_retire_a_job(reason):
    """A job may be retired on a verdict, never on an error — otherwise fixing the
    backend cannot bring back what it failed on."""
    assert reason in matcher_mod._RETRYABLE_SKIP_REASONS


@pytest.mark.parametrize("reason", ["no_content_text", "llm_skipped", None])
def test_verdicts_still_retire_a_job(reason):
    assert reason not in matcher_mod._RETRYABLE_SKIP_REASONS


def test_seen_store_releases_jobs_retired_by_a_scoring_error(tmp_path):
    db = Database(tmp_path / "test.db")
    now = datetime.now(timezone.utc).isoformat()
    for job_id, skipped, reason in [
        ("failed", 1, "api_error"),
        ("judged", 0, None),
        ("empty", 1, "no_content_text"),
    ]:
        db.upsert_match(
            run_id="run-1", job_id=job_id, board_token="acme", title="Account Manager",
            relevance_score=0, shortlisted=False, skipped=bool(skipped),
            skip_reason=reason, source_run_id="run-1", scored_at=now, raw_json="{}",
        )
    db.mark_seen(["failed", "judged", "empty"])

    seen = SeenStore(db=db)

    assert "failed" not in seen, "a job the backend failed on must become eligible again"
    assert "judged" in seen and "empty" in seen, "real outcomes must still retire a job"


def test_a_job_scored_successfully_later_is_not_released(tmp_path):
    """One bad run followed by a good one must not un-retire the job."""
    db = Database(tmp_path / "test.db")
    now = datetime.now(timezone.utc).isoformat()
    for run_id, skipped, reason in [("run-1", 1, "api_error"), ("run-2", 0, None)]:
        db.upsert_match(
            run_id=run_id, job_id="j1", board_token="acme", title="Account Manager",
            relevance_score=80, shortlisted=True, skipped=bool(skipped),
            skip_reason=reason, source_run_id=run_id, scored_at=now, raw_json="{}",
        )
    db.mark_seen(["j1"])

    assert "j1" in SeenStore(db=db)


# --- the circuit breaker ---------------------------------------------------

def test_the_breaker_trips_after_five_consecutive_failures():
    breaker = matcher_mod._ScoringBreaker()
    for i in range(4):
        breaker.record(_result(f"j{i}", "api_error"))
        assert not breaker.tripped

    breaker.record(_result("j4", "api_error"))
    assert breaker.tripped


def test_a_success_resets_the_breaker():
    """A backend that fails occasionally must not abort a run that is working."""
    breaker = matcher_mod._ScoringBreaker()
    for i in range(4):
        breaker.record(_result(f"j{i}", "api_error"))
    breaker.record(_result("good", None, score=80))
    for i in range(4):
        breaker.record(_result(f"k{i}", "api_error"))

    assert not breaker.tripped


def test_budget_drops_do_not_trip_the_breaker():
    """Over-budget jobs are skipped in their thousands and say nothing about the
    backend's health."""
    breaker = matcher_mod._ScoringBreaker()
    for i in range(20):
        breaker.record(_result(f"j{i}", matcher_mod.BUDGET_SKIP_REASON))

    assert not breaker.tripped


def test_the_breaker_summary_names_the_error_and_promises_a_retry():
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "claude CLI exited 1: --json-schema is not valid JSON"
    for i in range(5):
        breaker.record(_result(f"j{i}", "api_error"))

    summary = breaker.summary()
    assert "--json-schema" in summary
    assert "rescored" in summary
    assert "machine" not in summary


def test_a_breaker_tripped_by_launch_failures_blames_the_machine():
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "claude CLI exited 3221225794 (0xC0000142 STATUS_DLL_INIT_FAILED)"
    breaker.launch_failed = True
    for i in range(5):
        breaker.record(_result(f"j{i}", "api_error"))

    summary = breaker.summary()
    assert breaker.tripped
    assert "0xC0000142" in summary and "machine" in summary
    assert "No jobs were retired" in summary


def test_the_scorer_flags_a_launch_failure_for_the_breaker():
    """The flag the breaker reads must follow the most recent failure, either way."""
    class _Backend:
        def __init__(self, exc):
            self.exc = exc

        async def call(self, prompt, system_prompt):
            raise self.exc

    from hireshire.matcher.scorer import JobScorer
    from hireshire.models.job import Job

    now = datetime.now(timezone.utc)
    job = Job(
        source="greenhouse", board_token="acme", title="Account Manager", job_id="j1",
        location={"name": "Remote"}, absolute_url="https://example.com/j1",
        updated_at=now, scraped_at=now, content_text="Sell things.",
    )
    scorer = JobScorer(MatcherSettings(), _Backend(CLILaunchError("exited 0xC0000142")))
    result = asyncio.run(scorer.score(job, "resume", "run-1"))
    assert result.skip_reason == "api_error" and scorer.last_error_was_launch

    scorer._backend = _Backend(RuntimeError("claude CLI exited 1"))
    asyncio.run(scorer.score(job, "resume", "run-1"))
    assert not scorer.last_error_was_launch
