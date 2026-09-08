"""How the LLM spend is decided, and what happens to the jobs it is not spent on.

Two mechanisms, and keeping them apart is most of what these tests are for:

  * `rerank.min_score` is the CUTOFF — a verdict about one job, applied per batch,
    which is what lets a job be judged the moment it is scraped.
  * `funnel.top_k` is the CAP — a fuse that stops a mis-set cutoff handing the judge
    an unbounded number of calls. It is not how jobs are chosen.

They differ in what they mean afterwards, too: a cutoff drop is retired, a cap drop
stays eligible for the next sweep. Getting that backwards either re-runs the same
deterministic computation six times a day or permanently discards a job that was only
unlucky in a busy run.

No encoder weights and no network: the reranker is stubbed, which is the point. What
matters here is the selection and bookkeeping around it, not the model.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from matcher import (
    CAP_SKIP_REASON,
    CUTOFF_SKIP_REASON,
    DUPLICATE_SKIP_REASON,
    _CallBudget,
    _RETRYABLE_SKIP_REASONS,
    _process_batch,
)
from hireshire.funnel.config import RerankConfig
from hireshire.funnel.rerank import RERANK_STAGE, Reranker
from hireshire.models.job import Job

RUN_ID = "test-run"


def make_job(
    job_id: str,
    title: str | None = None,
    board: str = "acme",
    age_days: int = 0,
    content_text: str | None = None,
) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=board,
        title=title if title is not None else f"Engineer {job_id}",
        job_id=job_id,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now - timedelta(days=age_days),
        # Distinct by default, and distinct by a wide margin. Clustering keys on the
        # DESCRIPTION, so a shared constant here would collapse every job in a test
        # into one cluster and the cutoff tests would pass for the wrong reason.
        # Note the margin has to exceed `max_word_diff`: "a description of job 1" and
        # "a description of job 2" are only 2 words apart and would still merge.
        # Tests that want a cluster pass the same `content_text` explicitly.
        content_text=(
            content_text if content_text is not None
            else " ".join(f"unique-{job_id}-token-{i}" for i in range(12))
        ),
        scraped_at=now,
    )


class FakeReranker:
    """Scores each job by the integer in its job_id, so the expected outcome is
    obvious from the fixture alone."""

    usable = True

    def __init__(self, scores: dict[str, float] | None = None):
        self._scores = scores
        self.calls = 0

    async def rank(self, jobs):
        self.calls += 1
        return [
            self._scores[j.job_id] if self._scores is not None else float(j.job_id)
            for j in jobs
        ]


def run_batch(jobs, reranker=None, min_score=0.0, cap=0, dedupe=True):
    """`_process_batch` with a fresh budget. `cap=0` means uncapped, as top_k does."""
    return asyncio.run(
        _process_batch(
            jobs, reranker or FakeReranker(), min_score, _CallBudget(cap), RUN_ID, dedupe
        )
    )


# --- the cutoff ------------------------------------------------------------------

def test_only_jobs_reaching_the_cutoff_are_judged():
    jobs = [make_job(str(i)) for i in range(10)]
    winners, dropped, _ = run_batch(jobs, min_score=7.0)

    assert sorted(j.job_id for j, _ in winners) == ["7", "8", "9"]
    assert {r.job_id for r in dropped} == {"0", "1", "2", "3", "4", "5", "6"}


def test_a_below_cutoff_job_is_retired_because_the_answer_will_not_change():
    """The asymmetry with a cap drop, and the reason it exists.

    Same profile, same model, same description means the same logit next sweep. With
    `max_age_hours=24` and a 4h poll a job resurfaces ~6 times, so leaving these
    retryable would re-run one deterministic computation six times to reach the same
    conclusion — and it could never win, because there is nothing to compete against.
    """
    _, dropped, _ = run_batch([make_job("1")], min_score=5.0)

    assert [r.skip_reason for r in dropped] == [CUTOFF_SKIP_REASON]
    assert CUTOFF_SKIP_REASON not in _RETRYABLE_SKIP_REASONS


def test_the_cutoff_drop_keeps_its_score_so_the_user_can_see_how_close_it_came():
    _, dropped, _ = run_batch([make_job("3")], min_score=5.0)

    row = dropped[0]
    assert row.skipped is True
    assert row.rerank_score == 3.0
    assert row.rerank_stage == RERANK_STAGE
    # One model now, so nothing writes the old wide column.
    assert row.rerank_score_wide is None


def test_an_unusable_reranker_is_not_treated_as_a_verdict():
    """0.0 from an unusable reranker is an absence of information, not a score.

    Gating on it would silently drop an entire sweep the moment the profile file went
    missing — a failure the setup skill already warns about, and one that produces no
    error anywhere. Everything falls through to the cap instead.
    """
    class Unusable:
        usable = False

        async def rank(self, jobs):
            return [0.0] * len(jobs)

    winners, dropped, _ = run_batch([make_job("1"), make_job("2")], Unusable(), min_score=5.0)

    assert len(winners) == 2
    assert dropped == []


# --- the cap ---------------------------------------------------------------------

def test_the_cap_bounds_a_run_however_the_cutoff_is_set():
    jobs = [make_job(str(i)) for i in range(10)]
    winners, dropped, _ = run_batch(jobs, min_score=-99.0, cap=3)

    assert len(winners) == 3
    assert len(dropped) == 7
    assert {r.skip_reason for r in dropped} == {CAP_SKIP_REASON}


def test_the_cap_spends_its_last_calls_on_the_batch_s_best():
    """Ordering no longer decides who is judged — the cutoff does — but it still
    decides who gets the final calls when the run is nearly out."""
    jobs = [make_job(str(i)) for i in range(6)]
    winners, _, _ = run_batch(jobs, min_score=-99.0, cap=2)

    assert [j.job_id for j, _ in winners] == ["5", "4"]


def test_cap_drops_stay_eligible_for_the_next_sweep():
    """A cap drop is a statement about competition for a scarce budget, not about the
    job. It may well be the best thing in a quieter run."""
    jobs = [make_job(str(i)) for i in range(4)]
    _, dropped, _ = run_batch(jobs, min_score=-99.0, cap=1)

    assert dropped, "fixture should produce cap drops"
    for r in dropped:
        assert r.skip_reason == CAP_SKIP_REASON
        assert r.skip_reason in _RETRYABLE_SKIP_REASONS

    # ...whereas an ordinary title rejection is a fact about the job, and retires it.
    from hireshire.matcher.title_filter import filtered_result
    excluded = filtered_result(make_job("99"), "title_excluded", RUN_ID)
    assert excluded.skip_reason not in _RETRYABLE_SKIP_REASONS


@pytest.mark.parametrize("cap", [0, -1, 100])
def test_a_non_positive_or_oversized_cap_judges_everything_above_the_cutoff(cap):
    jobs = [make_job(str(i)) for i in range(6)]
    winners, dropped, _ = run_batch(jobs, min_score=-99.0, cap=cap)
    assert len(winners) == 6
    assert dropped == []


def test_the_cap_is_shared_across_batches():
    """The streaming invariant. Batches arrive one employer at a time, so a per-batch
    count would let a 30-company sweep make 30 caps' worth of calls."""
    budget = _CallBudget(3)
    # Distinct titles, or the normaliser groups them into one cluster and each batch
    # spends a single call — which would make this pass for the wrong reason.
    titles = ["Account Manager", "Data Analyst"]
    for company in ("acme", "globex", "initech"):
        jobs = [
            make_job(f"{company}-{i}", title=t, board=company)
            for i, t in enumerate(titles)
        ]
        asyncio.run(
            _process_batch(
                jobs,
                FakeReranker({f"{company}-0": 1.0, f"{company}-1": 2.0}),
                -99.0, budget, RUN_ID,
            )
        )
    assert budget.spent == 3
    assert budget.refused == 3


def test_empty_pool_never_loads_the_model():
    fake = FakeReranker()
    winners, dropped, siblings = run_batch([], fake)
    assert (winners, dropped, siblings) == ([], [], {})
    assert fake.calls == 0


# --- clustering ------------------------------------------------------------------

def test_repeat_postings_share_one_call():
    """31 copies of one requisition once consumed 31 of 100 slots. One cluster, one
    call — and the copies come back as siblings so their score can be filled in.

    This still works per batch, which is what made streaming possible: postings group
    by employer and description, and the scraper emits one employer per queue item,
    so every member of a cluster is in the same batch by construction.

    The five copies carry *different* titles here, which is the point: the title is
    not consulted at all.
    """
    dupes = [
        make_job(str(i), content_text="one requisition, posted five times")
        for i in range(5)
    ]
    other = make_job("99", title="Client Success Manager")

    winners, dropped, siblings = run_batch([*dupes, other], min_score=-99.0)

    assert len(winners) == 2, "one call per cluster, not per posting"
    rep_ids = {j.job_id for j, _ in winners}
    assert "99" in rep_ids
    # The representative is the cluster's best-scoring member.
    assert "4" in rep_ids
    assert len(siblings["4"]) == 4
    assert dropped == [], "siblings are not drops — they inherit a verdict"


def test_siblings_of_a_winning_cluster_are_not_retryable():
    """A sibling has been judged, just by proxy. Re-queuing it next sweep would
    spend a call re-deriving a score it already carries."""
    assert DUPLICATE_SKIP_REASON not in _RETRYABLE_SKIP_REASONS


def test_every_member_of_a_dropped_cluster_gets_its_own_row():
    """A member with no row never reaches the all-jobs export and is never marked
    seen — it simply vanishes from the run, which is the one outcome clustering is
    supposed to make impossible."""
    dupes = [make_job(str(i), content_text="one requisition") for i in range(3)]
    other = make_job("50", title="Client Success Manager")

    _, dropped, _ = run_batch([*dupes, other], min_score=99.0)

    assert {r.job_id for r in dropped} == {"0", "1", "2", "50"}
    for r in dropped:
        assert r.skip_reason == CUTOFF_SKIP_REASON
    # Every member of the cluster carries the cluster's size, not its own row count.
    assert {r.cluster_size for r in dropped if r.job_id != "50"} == {3}


def test_dedupe_can_be_switched_off():
    dupes = [make_job(str(i), content_text="one requisition") for i in range(4)]
    winners, _, siblings = run_batch(dupes, min_score=-99.0, dedupe=False)
    assert len(winners) == 4
    assert siblings == {}


def test_clusters_never_span_two_employers():
    """The same description at two companies is two jobs, not a duplicate — job
    boards are full of shared agency boilerplate."""
    a = make_job("1", board="acme", content_text="identical boilerplate")
    b = make_job("2", board="globex", content_text="identical boilerplate")
    winners, _, siblings = run_batch([a, b], min_score=-99.0)
    assert len(winners) == 2
    assert siblings == {}


# --- reranker no-ops -------------------------------------------------------------

def test_reranker_without_a_profile_is_a_no_op():
    """No profile means no query to compare against. `usable` is how the caller is
    told, because the 0.0 it returns is indistinguishable from a real score."""
    r = Reranker(RerankConfig(), profile="")
    assert r.usable is False
    jobs = [make_job(str(i)) for i in range(3)]
    assert asyncio.run(r.rank(jobs)) == [0.0, 0.0, 0.0]


def test_reranker_disabled_is_a_no_op_even_with_a_profile():
    r = Reranker(RerankConfig(enabled=False), profile="a profile")
    assert r.usable is False
    assert asyncio.run(r.rank([make_job("1")])) == [0.0]


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
