"""End-to-end matcher run with LLM scoring disabled.

This is the path `orchestrate.py --no-llm` takes, and the one to use for testing a
fresh install without spending scoring quota. It is worth a real test because it is
easy to leave behind: every code path that emits results has to be implemented
twice, once for the scorer and once for the passthrough, and the passthrough is the
one nobody runs day to day.

The regression it guards specifically: cluster siblings are deliberately absent
from `_spend_budget`'s `dropped` list, because normally the scorer emits them. A
passthrough that forgot to do the same would drop them on the floor — no database
row, no export, never marked seen.

No network, no model weights, no LLM: the funnel is off (so torch is never
imported) and the reranker is a no-op without a search profile.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import matcher as matcher_mod
from hireshire.matcher.config import MatcherConfig
from hireshire.models.job import Job
from hireshire.storage.db import Database

RUN_ID = "2026-08-14T00-00-00Z"


def make_job(job_id: str, title: str, board: str = "acme") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=board,
        job_id=job_id,
        title=title,
        location={"name": f"City {job_id}"},
        absolute_url=f"https://example.com/{job_id}",
        updated_at=now,
        content_text="a description",
        scraped_at=now,
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")

    cfg = MatcherConfig.model_validate({
        "settings": {
            "threshold": 85,
            "skip_llm": True,
            "resume_path": str(tmp_path / "resume.pdf"),
        },
        # Funnel off keeps torch out of the test entirely; top_k and dedupe are read
        # from this block regardless, which is what we are exercising.
        "funnel": {"enabled": False, "top_k": 3},
    })

    monkeypatch.setattr(matcher_mod, "load_matcher_config", lambda: cfg)
    monkeypatch.setattr(matcher_mod, "get_db", lambda *a, **k: db)
    monkeypatch.setattr(matcher_mod, "extract_resume_text", lambda *a, **k: "resume text")
    # No profile -> Reranker.usable is False -> every score is 0.0 and no model
    # is ever loaded.
    monkeypatch.setattr(matcher_mod, "_load_search_profile", lambda settings: "")
    return db


def run_queue_mode(jobs):
    """Drive matcher.main the way orchestrate does: one company batch, then the
    sentinel."""
    async def go():
        in_q: asyncio.Queue = asyncio.Queue()
        out_q: asyncio.Queue = asyncio.Queue()
        await in_q.put(("acme", jobs))
        await in_q.put(None)
        await matcher_mod.main(
            in_queue=in_q, out_queue=out_q, quiet=True, run_id=RUN_ID, skip_llm=True
        )
        forwarded = []
        while True:
            item = await out_q.get()
            if item is None:
                break
            forwarded.append(item)
        return forwarded

    return asyncio.run(go())


def test_no_llm_run_emits_every_job_including_cluster_siblings(harness):
    db = harness
    jobs = [
        make_job("d1", "Multi-Media Account Executive"),
        make_job("d2", "Multi-Media Account Executive"),
        make_job("d3", "Multi-Media Account Executive - 101.5"),
        make_job("solo", "Client Success Manager"),
    ]
    forwarded = run_queue_mode(jobs)

    rows = {r["job_id"]: r for r in db.load_all_matches(RUN_ID)}
    # The regression: all four jobs must have a row. Three of them are one cluster,
    # and the two siblings are emitted by the passthrough, not by _spend_budget.
    assert set(rows) == {"d1", "d2", "d3", "solo"}, "a sibling was dropped on the floor"

    siblings = [r for r in rows.values() if r.get("cluster_representative")]
    assert len(siblings) == 2
    for s in siblings:
        assert s["cluster_size"] == 3
        # Each keeps its own identity, so the user can still apply per location.
        assert s["absolute_url"].endswith(s["job_id"])

    # Only the representative reaches the apply queue: three copies of one
    # requisition must not become three applications.
    assert len(forwarded) == 2, "one queued row per cluster"
    queued = {r.job_id for r, _ in forwarded}
    assert "solo" in queued
    # Which of the three copies represents the cluster is not pinned: without a
    # search profile every rerank score is 0.0, so the tie falls to `updated_at`,
    # which is identical across the fixture. Exactly one of them must be queued.
    assert len(queued & {"d1", "d2", "d3"}) == 1
    for r, _ in forwarded:
        assert r.cluster_representative is None, "a sibling reached the apply queue"


def test_no_llm_run_makes_no_scoring_calls(harness, monkeypatch):
    """The whole point: nothing may reach a backend."""
    def explode(*a, **k):
        raise AssertionError("skip_llm must not construct an LLM backend")

    monkeypatch.setattr(matcher_mod, "make_backend", explode)
    run_queue_mode([make_job("j1", "Account Manager")])
    assert len(harness.load_all_matches(RUN_ID)) == 1


def test_passthrough_rows_are_shortlisted_despite_a_high_threshold(harness):
    """skip_llm means "no verdict", and `is_shortlisted` treats an absent score as
    passing. With threshold 85 — which nothing ever reached — this is what makes a
    --no-llm run produce a non-empty shortlist to exercise the rest of the pipeline.
    """
    run_queue_mode([make_job("j1", "Account Manager")])
    rows = harness.load_all_matches(RUN_ID)
    assert rows[0]["relevance_score"] is None
    assert rows[0]["shortlisted"] is True


def test_top_k_still_bounds_a_no_llm_run(harness):
    """top_k is 3 in the fixture, and clusters — not postings — are what it counts."""
    jobs = [make_job(f"j{i}", f"Account Manager {i}") for i in range(6)]
    run_queue_mode(jobs)

    rows = harness.load_all_matches(RUN_ID)
    dropped = [r for r in rows if r.get("skip_reason") == matcher_mod.BUDGET_SKIP_REASON]
    passed = [r for r in rows if r.get("skip_reason") == "llm_skipped"]
    assert len(passed) == 3
    assert len(dropped) == 3
