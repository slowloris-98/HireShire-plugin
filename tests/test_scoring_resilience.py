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
from hireshire.matcher.config import MatcherSettings
from hireshire.matcher.scorer import (
    SCORING_ERROR_SKIP_REASONS,
    ClaudeCodeBackend,
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
                    core_skills_score=40, core_skills_rationale="ok",
                    experience_score=30, experience_rationale="ok",
                    education_bonus_score=5, education_rationale="ok",
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
