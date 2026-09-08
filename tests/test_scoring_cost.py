"""What a sweep costs the user, and the three mechanisms that keep it down.

Scoring runs on the user's Claude subscription, drawing on the same rolling 5-hour
and weekly windows as their own chat. That makes cost a correctness concern rather
than an optimisation: a plugin that quietly eats someone's allowance is broken even
when every score it produces is right.

Three things are pinned here, all of them silent when they break:

  * the resume rides in the SYSTEM PROMPT, so the unchanging prefix can be cached;
  * `--no-session-persistence`, so 150 scored jobs do not leave 150 transcripts and
    150 background title-generation calls behind;
  * `structured_output` is read, so the current CLI's payload is actually found.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hireshire.matcher.config import MatcherSettings
from hireshire.matcher.prompts import SCORER_SYSTEM_PROMPT
from hireshire.matcher.scorer import (
    ClaudeCodeBackend,
    JobScorer,
    ScoringSchema,
    UsageTally,
)
from hireshire.models.job import Job
from datetime import datetime, timezone


def make_job() -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token="acme",
        title="Account Manager",
        job_id="j1",
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now,
        content_text="We are hiring an account manager. Salesforce required.",
        scraped_at=now,
    )


class RecordingBackend:
    def __init__(self):
        self.system_prompt = None
        self.prompt = None

    async def call(self, prompt, system_prompt):
        self.prompt = prompt
        self.system_prompt = system_prompt
        return ScoringSchema(
            core_skills_score=30, core_skills_rationale="ok",
            experience_score=30, experience_rationale="ok",
            education_bonus_score=10, education_rationale="ok",
            match_reasons=[], disqualifiers=[], recommend=True,
        )


# --- the cached prefix -----------------------------------------------------

def test_the_resume_rides_in_the_system_prompt_not_the_message():
    """The whole caching design rests on this, and nothing fails if it regresses.

    Caching is a prefix match over system prompt then conversation. The rubric,
    resume and schema are identical for every job in a run; only the posting
    changes. With the resume in the system prompt that entire prefix is cacheable
    and bills at ~10% from the second job on. Move the resume back into the message
    and it sits behind changing content, can never be cached, and the run silently
    pays full price to re-read the same resume once per job.
    """
    backend = RecordingBackend()
    scorer = JobScorer(settings=MatcherSettings(), backend=backend)
    asyncio.run(scorer.score(make_job(), "RESUME BODY", "run-1"))

    assert "RESUME BODY" in backend.system_prompt
    assert "RESUME BODY" not in backend.prompt
    # ...and the rubric leads it, so the prefix is stable across users too.
    assert backend.system_prompt.startswith(SCORER_SYSTEM_PROMPT)


def test_only_the_posting_varies_between_two_jobs_in_a_run():
    """The property caching actually needs: byte-identical prefixes."""
    backend = RecordingBackend()
    scorer = JobScorer(settings=MatcherSettings(), backend=backend)

    asyncio.run(scorer.score(make_job(), "RESUME", "run-1"))
    first = backend.system_prompt
    job2 = make_job()
    job2.job_id, job2.title, job2.content_text = "j2", "Client Partner", "Different text"
    asyncio.run(scorer.score(job2, "RESUME", "run-1"))

    assert backend.system_prompt == first
    assert backend.prompt != first


def test_optional_projects_stay_in_the_cached_half():
    """`projects_path` is per-user, not per-job, so it belongs with the resume. In
    the message it would be re-sent at full price on every call."""
    backend = RecordingBackend()
    scorer = JobScorer(settings=MatcherSettings(), backend=backend)
    asyncio.run(scorer.score(make_job(), "RESUME", "run-1", "SIDE PROJECTS"))

    assert "SIDE PROJECTS" in backend.system_prompt
    assert "SIDE PROJECTS" not in backend.prompt


# --- the CLI argv ----------------------------------------------------------

def _fake_exec(captured, payload):
    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv

        class _Proc:
            returncode = 0

            async def communicate(self, input=None):
                return json.dumps(payload).encode(), b""

        return _Proc()
    return fake_exec


def _backend(monkeypatch, captured, payload):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec(captured, payload))
    return ClaudeCodeBackend(MatcherSettings(request_interval_s=0), asyncio.Semaphore(1))


_OK = {
    "structured_output": {
        "core_skills_score": 40, "core_skills_rationale": "ok",
        "experience_score": 30, "experience_rationale": "ok",
        "education_bonus_score": 5, "education_rationale": "ok",
        "match_reasons": [], "disqualifiers": [], "recommend": True,
    },
    "total_cost_usd": 0.031,
    "usage": {
        "input_tokens": 900,
        "output_tokens": 400,
        "cache_read_input_tokens": 2500,
        "cache_creation": {"ephemeral_1h_input_tokens": 120},
    },
}


def test_scoring_leaves_no_session_behind(monkeypatch):
    """150 scored jobs otherwise write 150 transcripts AND bill ~150 extra calls:
    Claude Code generates a title for every unnamed session with a background model
    request, against the same allowance the user's own chat draws on."""
    captured: dict = {}
    backend = _backend(monkeypatch, captured, _OK)
    asyncio.run(backend.call("prompt", "system"))

    assert "--no-session-persistence" in captured["argv"]


def test_bare_mode_is_never_passed(monkeypatch):
    """`--bare` never reads OAuth or the keychain, so it would break the very
    subscription path that makes scoring free. The docs recommend it for scripted
    calls, which makes it a plausible future 'cleanup' — hence the guard."""
    captured: dict = {}
    backend = _backend(monkeypatch, captured, _OK)
    asyncio.run(backend.call("prompt", "system"))

    assert "--bare" not in captured["argv"]


def test_the_structured_output_key_is_read(monkeypatch):
    """Where the current CLI puts a `--json-schema` result. The older keys stay
    supported because this backend cannot know which version is on PATH."""
    captured: dict = {}
    backend = _backend(monkeypatch, captured, _OK)
    result = asyncio.run(backend.call("prompt", "system"))

    assert result.core_skills_score == 40


@pytest.mark.parametrize("key", ["result", "response", "content", "output"])
def test_older_payload_keys_still_work(monkeypatch, key):
    captured: dict = {}
    payload = {key: json.dumps(_OK["structured_output"])}
    backend = _backend(monkeypatch, captured, payload)

    assert asyncio.run(backend.call("p", "s")).core_skills_score == 40


# --- the meters ------------------------------------------------------------

def test_usage_is_recorded_from_the_envelope(monkeypatch):
    captured: dict = {}
    backend = _backend(monkeypatch, captured, _OK)
    asyncio.run(backend.call("prompt", "system"))

    assert backend.usage.calls == 1
    assert backend.usage.input == 900
    assert backend.usage.output == 400
    assert backend.usage.cache_read == 2500
    assert backend.usage.cache_write == 120
    assert backend.usage.cost_usd == pytest.approx(0.031)


def test_a_renamed_or_missing_usage_field_never_breaks_a_run():
    """Instrumentation hanging off the scoring path must not be able to turn a
    working sweep into a failed one. A missing number reads as zero, which is the
    correct signal that the meters are not being read."""
    tally = UsageTally()
    for envelope in ({}, {"usage": "not a dict"}, {"usage": {"renamed": 1}},
                     {"total_cost_usd": "free"}, None, []):
        tally.record(envelope)

    assert tally.input == 0
    assert tally.cost_usd == 0.0


def test_the_tally_serialises_for_the_run_row(monkeypatch):
    """`runs.stats_json` is how the meters reach the reports and the all-jobs CSV.
    Before this, the only copy of a monitor sweep's cost was a line in a log file,
    because `quiet=True` suppresses the console summary."""
    captured: dict = {}
    backend = _backend(monkeypatch, captured, _OK)
    asyncio.run(backend.call("prompt", "system"))

    assert backend.usage.as_dict() == {
        "calls": 1, "input": 900, "output": 400,
        "cache_read": 2500, "cache_write": 120, "cost_usd": pytest.approx(0.031),
    }


def test_an_unmeasured_run_reports_no_usage_at_all():
    """Only backends that read their own meters have a tally. A run without one
    records nothing rather than a row of zeros that would read as 'this was free'."""
    import matcher

    class _Scorer:
        def __init__(self, usage):
            self.usage = usage

    assert matcher._usage_stats(None) is None
    assert matcher._usage_stats(_Scorer(None)) is None
    assert matcher._usage_stats(_Scorer(UsageTally())) is None, "empty tally, no call made"

    tally = UsageTally()
    tally.record(_OK)
    assert matcher._usage_stats(_Scorer(tally))["calls"] == 1


def test_finalise_omits_the_usage_key_when_nothing_was_measured(tmp_path):
    """Absent, not zeroed: a reader that finds no key knows the run was never
    measured, which is a different fact from a sweep that cost nothing."""
    from hireshire.matcher.store import MatchStore

    class _DB:
        def __init__(self):
            self.stats = None

        def finalise_run(self, run_id, phase, started_at, finished_at=None, stats=None):
            self.stats = stats

    db = _DB()
    store = MatchStore("run-1", threshold=75, db=db)
    started = datetime(2026, 9, 7, tzinfo=timezone.utc)

    store.finalise([], [], started, 75, "claude-sonnet-5", 0)
    assert "usage" not in db.stats

    store.finalise([], [], started, 75, "claude-sonnet-5", 0, {"calls": 3, "cost_usd": 0.5})
    assert db.stats["usage"] == {"calls": 3, "cost_usd": 0.5}


def test_a_flat_cache_creation_count_is_accepted():
    """Older CLI versions reported a number where current ones report a per-TTL
    object."""
    tally = UsageTally()
    tally.record({"usage": {"cache_creation_input_tokens": 700}})
    assert tally.cache_write == 700
