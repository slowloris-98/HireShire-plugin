"""Top-K budget tests — how the LLM spend is bounded, and what happens to the
jobs that lose the race.

No encoder weights and no network: the reranker is stubbed, which is the point.
What matters here is the selection and bookkeeping around it, not the model.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from matcher import (
    BUDGET_SKIP_REASON,
    DUPLICATE_SKIP_REASON,
    _RETRYABLE_SKIP_REASONS,
    _spend_budget,
)
from hireshire.funnel.config import FunnelConfig, RerankConfig
from hireshire.funnel.rerank import RerankScores, Reranker
from hireshire.models.job import Job

RUN_ID = "test-run"


def make_job(job_id: str, title: str | None = None, board: str = "acme", age_days: int = 0) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=board,
        # Distinct by default: identical titles at one employer are a *cluster*, and
        # the budget tests below are about ranking, not grouping.
        title=title if title is not None else f"Engineer {job_id}",
        job_id=job_id,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now - timedelta(days=age_days),
        content_text="a description",
        scraped_at=now,
    )


class FakeReranker:
    """Scores each job by the integer in its job_id, so the expected ranking is
    obvious from the fixture alone."""

    def __init__(self, scores: dict[str, float] | None = None, refined: bool = True):
        self._scores = scores
        self._refined = refined
        self.calls = 0

    async def rank(self, jobs):
        self.calls += 1
        out = []
        for j in jobs:
            value = self._scores[j.job_id] if self._scores is not None else float(j.job_id)
            out.append(
                RerankScores(wide=value, refined=value if self._refined else None)
            )
        return out


def test_top_k_keeps_the_highest_scoring_jobs():
    jobs = [make_job(str(i)) for i in range(10)]
    winners, dropped, _ = asyncio.run(_spend_budget(jobs, FakeReranker(), 3, RUN_ID))

    assert [j.job_id for j, _ in winners] == ["9", "8", "7"]
    assert len(dropped) == 7
    # Winners come back in descending rank order so the caller can spend the
    # budget best-first even if it later truncates further.
    keys = [s.sort_key for _, s in winners]
    assert keys == sorted(keys, reverse=True)


def test_over_budget_jobs_are_marked_retryable_and_keep_their_score():
    jobs = [make_job(str(i)) for i in range(5)]
    _, dropped, _ = asyncio.run(_spend_budget(jobs, FakeReranker(), 2, RUN_ID))

    assert {r.job_id for r in dropped} == {"0", "1", "2"}
    for r in dropped:
        assert r.skipped is True
        assert r.skip_reason == BUDGET_SKIP_REASON
        # The score is retained so a user can see how close a job came.
        assert r.rerank_score == float(r.job_id)
        assert r.rerank_score_wide == float(r.job_id)


def test_budget_drops_are_not_retired_into_seen_jobs():
    """The regression this guards: a job that misses the cut in one crowded sweep
    must stay eligible for the next one. Marking it seen would retire it forever
    on the strength of a single run's competition."""
    jobs = [make_job(str(i)) for i in range(4)]
    _, dropped, _ = asyncio.run(_spend_budget(jobs, FakeReranker(), 1, RUN_ID))

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
    winners, dropped, _ = asyncio.run(_spend_budget(jobs, FakeReranker(), top_k, RUN_ID))
    assert len(winners) == 6
    assert dropped == []


def test_empty_pool_never_loads_the_model():
    fake = FakeReranker()
    winners, dropped, siblings = asyncio.run(_spend_budget([], fake, 10, RUN_ID))
    assert (winners, dropped, siblings) == ([], [], {})
    assert fake.calls == 0


# --- the two rerank stages are on different scales -------------------------------

def test_a_refined_job_outranks_an_unrefined_one_with_a_higher_number():
    """The regression that would be invisible without this test.

    A wide-pass logit and a refined logit come from different models. Sorting them
    as plain floats would let an unrefined job with a numerically larger score beat
    a refined one — silently spending the budget on jobs the better model never
    endorsed. Refinement must dominate the raw number.
    """
    refined_low = make_job("1")
    unrefined_high = make_job("2")
    scores = {
        "1": RerankScores(wide=-9.0, refined=-5.0),  # refined, but a small number
        "2": RerankScores(wide=99.0),                # unrefined, huge number
    }

    class Staged:
        async def rank(self, jobs):
            return [scores[j.job_id] for j in jobs]

    winners, dropped, _ = asyncio.run(
        _spend_budget([unrefined_high, refined_low], Staged(), 1, RUN_ID)
    )
    assert [j.job_id for j, _ in winners] == ["1"]
    assert [r.job_id for r in dropped] == ["2"]


def test_rerank_stage_is_recorded_on_every_row():
    jobs = [make_job("1"), make_job("2")]
    _, dropped, _ = asyncio.run(
        _spend_budget(jobs, FakeReranker(refined=False), 1, RUN_ID)
    )
    assert dropped[0].rerank_stage == "wide"
    assert dropped[0].rerank_score is None
    assert dropped[0].rerank_score_wide is not None


# --- clustering ------------------------------------------------------------------

def test_repeat_postings_share_one_budget_slot():
    """31 copies of one requisition once consumed 31 of 100 slots. One cluster, one
    slot — and the copies come back as siblings so their score can be filled in."""
    dupes = [make_job(str(i), title="Multi-Media Account Executive") for i in range(5)]
    other = make_job("99", title="Client Success Manager")

    winners, dropped, siblings = asyncio.run(
        _spend_budget([*dupes, other], FakeReranker(), 2, RUN_ID)
    )

    assert len(winners) == 2, "one slot per cluster, not per posting"
    rep_ids = {j.job_id for j, _ in winners}
    assert "99" in rep_ids
    # The representative is the cluster's best-scoring member.
    assert "4" in rep_ids
    assert len(siblings["4"]) == 4
    assert dropped == [], "siblings are not budget drops — they inherit a verdict"


def test_siblings_of_a_winning_cluster_are_not_retryable():
    """A sibling has been judged, just by proxy. Re-queuing it next sweep would
    spend the budget re-deriving a score it already carries."""
    assert DUPLICATE_SKIP_REASON not in _RETRYABLE_SKIP_REASONS


def test_losing_cluster_members_are_all_retryable_budget_drops():
    dupes = [make_job(str(i), title="Account Executive") for i in range(3)]
    winner = make_job("50", title="Client Success Manager")

    _, dropped, _ = asyncio.run(_spend_budget([*dupes, winner], FakeReranker(), 1, RUN_ID))

    assert {r.job_id for r in dropped} == {"0", "1", "2"}
    for r in dropped:
        assert r.skip_reason == BUDGET_SKIP_REASON
        assert r.skip_reason in _RETRYABLE_SKIP_REASONS
        assert r.cluster_size == 3


def test_dedupe_can_be_switched_off():
    dupes = [make_job(str(i), title="Account Executive") for i in range(4)]
    winners, _, siblings = asyncio.run(
        _spend_budget(dupes, FakeReranker(), 4, RUN_ID, dedupe=False)
    )
    assert len(winners) == 4
    assert siblings == {}


def test_clusters_never_span_two_employers():
    """Same title at two companies is two jobs, not a duplicate."""
    a = make_job("1", title="Account Manager", board="acme")
    b = make_job("2", title="Account Manager", board="globex")
    winners, _, siblings = asyncio.run(_spend_budget([a, b], FakeReranker(), 5, RUN_ID))
    assert len(winners) == 2
    assert siblings == {}


# --- config ----------------------------------------------------------------------

def test_refine_depth_below_top_k_is_rejected():
    """Depth < top_k would fill the tail of the budget by comparing the two stages'
    incomparable scores. Fail at config load, not silently at rank time."""
    with pytest.raises(ValueError, match="depth"):
        FunnelConfig(top_k=100, rerank=RerankConfig(refine={"depth": 50}))


def test_refine_depth_is_unconstrained_when_refining_is_off():
    cfg = FunnelConfig(top_k=100, rerank=RerankConfig(refine={"depth": 1, "enabled": False}))
    assert cfg.top_k == 100


# --- reranker no-ops -------------------------------------------------------------

def test_reranker_without_a_profile_is_a_no_op():
    """No profile means no query to compare against. Scoring everything 0.0 keeps
    top-K bounded rather than dropping the whole pool."""
    r = Reranker(RerankConfig(), profile="")
    assert r.usable is False
    jobs = [make_job(str(i)) for i in range(3)]
    assert [s.wide for s in asyncio.run(r.rank(jobs))] == [0.0, 0.0, 0.0]


def test_reranker_disabled_is_a_no_op_even_with_a_profile():
    r = Reranker(RerankConfig(enabled=False), profile="a profile")
    assert r.usable is False
    scores = asyncio.run(r.rank([make_job("1")]))
    assert [s.wide for s in scores] == [0.0]
    assert scores[0].is_refined is False


def test_rerank_document_is_truncated_but_keeps_the_title():
    """The title survives truncation because it carries real signal, and the doc is
    capped so one pathological posting cannot dominate the batch's cost."""
    r = Reranker(RerankConfig(max_doc_chars=10), profile="p")
    job = make_job("1", title="Staff Platform Engineer")
    job.content_text = "x" * 500
    doc = r._doc(job)
    assert doc.startswith("Staff Platform Engineer")
    assert doc.count("x") == 10


# --- a failed representative must not retire its whole cluster -------------------

def test_a_sibling_inherits_a_retryable_failure_rather_than_the_duplicate_reason():
    """The rule this protects: a job may be retired on a verdict, never on an error.

    If the representative's scoring call failed, its copies were not judged either.
    Stamping them `duplicate_of_cluster` — which is deliberately NOT retryable —
    would retire the entire cluster permanently because one backend call broke.
    """
    from datetime import datetime, timezone
    from matcher import _sibling_result
    from hireshire.matcher.scorer import MatchResult

    failed_rep = MatchResult(
        job_id="rep", board_token="acme", title="AM", location="Remote",
        absolute_url="https://example.com/rep", relevance_score=0,
        match_reasons=[], disqualifiers=[], recommend=False,
        skipped=True, skip_reason="backend_unavailable",
        scored_at=datetime.now(timezone.utc), source_run_id=RUN_ID,
    )
    sib = _sibling_result(make_job("s1", "AM"), failed_rep, RUN_ID, None, 2)

    assert sib.skip_reason == "backend_unavailable"
    assert sib.skip_reason in _RETRYABLE_SKIP_REASONS


def test_a_sibling_of_a_genuinely_scored_representative_is_retired():
    from datetime import datetime, timezone
    from matcher import _sibling_result
    from hireshire.matcher.scorer import MatchResult

    scored_rep = MatchResult(
        job_id="rep", board_token="acme", title="AM", location="Houston",
        absolute_url="https://example.com/rep", relevance_score=72,
        match_reasons=["good fit"], disqualifiers=[], recommend=True,
        skipped=False, skip_reason=None,
        scored_at=datetime.now(timezone.utc), source_run_id=RUN_ID,
    )
    job = make_job("s1", "AM")
    sib = _sibling_result(job, scored_rep, RUN_ID, None, 31)

    assert sib.skip_reason == DUPLICATE_SKIP_REASON
    assert sib.skip_reason not in _RETRYABLE_SKIP_REASONS
    # The verdict travels with it, so the all-jobs export still explains the row...
    assert sib.relevance_score == 72
    assert sib.match_reasons == ["good fit"]
    assert sib.cluster_representative == "rep"
    assert sib.cluster_size == 31
    # ...but its own identity and link are its own, so the user can apply directly.
    assert sib.job_id == "s1"
    assert sib.location == "Remote"
