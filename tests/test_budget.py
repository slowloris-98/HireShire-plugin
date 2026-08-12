"""Top-K budget tests — how the LLM spend is bounded, and what happens to the
jobs that lose the race.

No encoder weights and no network: the reranker is stubbed, which is the point.
What matters here is the selection and bookkeeping around it, not the model.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import matcher as matcher_mod
from matcher import BUDGET_SKIP_REASON, _RETRYABLE_SKIP_REASONS, _spend_budget
from hireshire.funnel.config import RerankConfig
from hireshire.funnel.rerank import Reranker
from hireshire.models.job import Job

RUN_ID = "test-run"


def make_job(job_id: str, title: str = "Engineer") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token="acme",
        job_id=job_id,
        title=title,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now,
        content_text="a description",
        scraped_at=now,
    )


class FakeReranker:
    """Scores each job by the integer in its job_id, so the expected ranking is
    obvious from the fixture alone."""

    def __init__(self, scores: dict[str, float] | None = None):
        self._scores = scores
        self.calls = 0

    async def rank(self, jobs):
        self.calls += 1
        if self._scores is not None:
            return [self._scores[j.job_id] for j in jobs]
        return [float(j.job_id) for j in jobs]


def test_top_k_keeps_the_highest_scoring_jobs():
    jobs = [make_job(str(i)) for i in range(10)]
    winners, dropped = asyncio.run(_spend_budget(jobs, FakeReranker(), 3, RUN_ID))

    assert [j.job_id for j, _ in winners] == ["9", "8", "7"]
    assert len(dropped) == 7
    # Winners come back in descending score order so the caller can spend the
    # budget best-first even if it later truncates further.
    assert [s for _, s in winners] == sorted((s for _, s in winners), reverse=True)


def test_over_budget_jobs_are_marked_retryable_and_keep_their_score():
    jobs = [make_job(str(i)) for i in range(5)]
    _, dropped = asyncio.run(_spend_budget(jobs, FakeReranker(), 2, RUN_ID))

    assert {r.job_id for r in dropped} == {"0", "1", "2"}
    for r in dropped:
        assert r.skipped is True
        assert r.skip_reason == BUDGET_SKIP_REASON
        # The score is retained so a user can see how close a job came.
        assert r.rerank_score == float(r.job_id)


def test_budget_drops_are_not_retired_into_seen_jobs():
    """The regression this guards: a job that misses the cut in one crowded sweep
    must stay eligible for the next one. Marking it seen would retire it forever
    on the strength of a single run's competition."""
    jobs = [make_job(str(i)) for i in range(4)]
    _, dropped = asyncio.run(_spend_budget(jobs, FakeReranker(), 1, RUN_ID))

    assert dropped, "fixture should produce budget drops"
    for r in dropped:
        assert r.skip_reason in _RETRYABLE_SKIP_REASONS

    # ...whereas an ordinary title rejection is not retryable and should be retired.
    from hireshire.matcher.title_filter import filtered_result
    excluded = filtered_result(make_job("99"), "title_excluded", RUN_ID)
    assert excluded.skip_reason not in _RETRYABLE_SKIP_REASONS


@pytest.mark.parametrize("top_k", [0, -1, 100])
def test_non_positive_or_oversized_top_k_scores_everything(top_k):
    jobs = [make_job(str(i)) for i in range(6)]
    winners, dropped = asyncio.run(_spend_budget(jobs, FakeReranker(), top_k, RUN_ID))
    assert len(winners) == 6
    assert dropped == []


def test_empty_pool_never_loads_the_model():
    fake = FakeReranker()
    winners, dropped = asyncio.run(_spend_budget([], fake, 10, RUN_ID))
    assert (winners, dropped) == ([], [])
    assert fake.calls == 0


def test_reranker_without_a_profile_is_a_no_op():
    """No profile means no query to compare against. Scoring everything 0.0 keeps
    top-K bounded rather than dropping the whole pool."""
    r = Reranker(RerankConfig(), profile="")
    assert r.usable is False
    jobs = [make_job(str(i)) for i in range(3)]
    assert asyncio.run(r.rank(jobs)) == [0.0, 0.0, 0.0]


def test_reranker_disabled_is_a_no_op_even_with_a_profile():
    r = Reranker(RerankConfig(enabled=False), profile="a profile")
    assert r.usable is False
    assert asyncio.run(r.rank([make_job("1")])) == [0.0]


def test_rerank_document_is_truncated_but_keeps_the_title():
    """The title survives truncation because it carries real signal and the
    cross-encoder caps the combined pair at 512 tokens."""
    r = Reranker(RerankConfig(max_doc_chars=10), profile="p")
    job = make_job("1", title="Staff Platform Engineer")
    job.content_text = "x" * 500
    doc = r._doc(job)
    assert doc.startswith("Staff Platform Engineer")
    assert doc.count("x") == 10
