"""Funnel wiring + list-only scraping tests (no network, no encoder weights).

Covers the matcher-entry funnel's staging (exclude → include fast-pass → encoder →
hydrate) with stubbed encoder/detail-fetcher, plus the scrapers' list-only parse and
the model_validate-based hydration that strips HTML.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import hireshire.funnel.funnel as funnel_mod
from hireshire.funnel.config import FunnelConfig
from hireshire.funnel.funnel import Funnel
from hireshire.matcher.config import TitleFilterConfig
from hireshire.models.job import Job

RUN_ID = "test-run"


def make_job(title: str, source: str = "greenhouse", content_text: str | None = "desc") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source=source,
        board_token="acme",
        job_id=title,  # unique-enough handle for assertions
        title=title,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now,
        content_text=content_text,
        scraped_at=now,
    )


class FakeRelevance:
    """Stub encoder: a title is 'relevant' iff it contains 'developer'."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.seen: list[str] = []

    async def score(self, titles):
        self.seen.extend(titles)
        # 0.9 clears any sane threshold, 0.1 clears none — so the funnel's own
        # comparison against cfg.threshold is what is under test, not this stub.
        return [0.9 if "developer" in t.lower() else 0.1 for t in titles]

    async def relevant_mask(self, titles):
        return [s >= self.cfg.threshold for s in await self.score(titles)]


class FakeDetailFetcher:
    """Stub hydrator: fills content for list->detail jobs lacking it; records calls."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hydrated: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def hydrate(self, jobs):
        out = []
        for j in jobs:
            if j.source in ("workday", "bamboohr") and not j.content_text:
                self.hydrated.append(j.job_id)
                out.append(j.model_validate({**j.model_dump(), "content_text": "hydrated desc"}))
            else:
                out.append(j)
        return out


@pytest.fixture
def patched_funnel(monkeypatch):
    """Funnel with the encoder + detail fetcher replaced by in-memory stubs."""
    monkeypatch.setattr(funnel_mod, "EncoderRelevance", FakeRelevance)
    monkeypatch.setattr(funnel_mod, "DetailFetcher", FakeDetailFetcher)


def _run(coro):
    return asyncio.run(coro)


def test_funnel_stages(patched_funnel):
    title_cfg = TitleFilterConfig(include_keywords=["engineer"], exclude_keywords=["manager"])
    cfg = FunnelConfig(enabled=True, encoder={"targets": ["software"], "threshold": 0.5})
    jobs = [
        make_job("Software Engineer", source="greenhouse", content_text="x"),  # include fast-pass
        make_job("Engineering Manager", source="greenhouse"),                  # excluded
        make_job("Backend Developer", source="workday", content_text=None),    # encoder-pass → hydrate
        make_job("Barista", source="bamboohr", content_text=None),             # encoder-fail
    ]

    async def go():
        async with Funnel(cfg, title_cfg, RUN_ID) as f:
            to_score, filtered, scores = await f.process(jobs)
            return to_score, filtered, scores, f._relevance, f._detail

    to_score, filtered, scores, relevance, detail = _run(go())

    kept_titles = {j.title for j in to_score}
    assert kept_titles == {"Software Engineer", "Backend Developer"}

    reasons = {r.title: r.skip_reason for r in filtered}
    assert reasons == {
        "Engineering Manager": "title_excluded",
        "Barista": "title_low_relevance",
    }

    # The fast-pass still bypasses the *gate* — "Software Engineer" is kept on the
    # include keyword alone, and would survive even scoring 0.1. But it is scored
    # anyway, so the column is never mysteriously blank for fast-passed jobs.
    assert set(relevance.seen) == {"Software Engineer", "Backend Developer", "Barista"}
    assert scores["Software Engineer"] == 0.1
    assert scores["Backend Developer"] == 0.9

    # The score that caused a drop is recorded on the row that records the drop.
    barista = next(r for r in filtered if r.title == "Barista")
    assert barista.encoder_score == 0.1
    # A keyword exclusion never reaches the encoder, so it has no score to carry.
    excluded = next(r for r in filtered if r.title == "Engineering Manager")
    assert excluded.encoder_score is None

    # Only the surviving list->detail job with no content was hydrated.
    assert detail.hydrated == ["Backend Developer"]
    hydrated_job = next(j for j in to_score if j.title == "Backend Developer")
    assert hydrated_job.content_text == "hydrated desc"


def test_funnel_no_targets_falls_back_to_include_rule(patched_funnel):
    title_cfg = TitleFilterConfig(include_keywords=["engineer"], exclude_keywords=[])
    cfg = FunnelConfig(enabled=True, encoder={"targets": []})  # encoder disabled
    jobs = [
        make_job("Software Engineer", content_text="x"),   # include match → keep
        make_job("Backend Developer", content_text="x"),   # no include, no encoder → dropped
    ]

    async def go():
        async with Funnel(cfg, title_cfg, RUN_ID) as f:
            return await f.process(jobs)

    to_score, filtered, scores = _run(go())
    assert {j.title for j in to_score} == {"Software Engineer"}
    assert [r.skip_reason for r in filtered] == ["title_no_include_match"]
    # No targets means no encoder ran at all, so there is nothing to record.
    assert scores == {}


# --- List-only scraping + hydration primitives (no network) ---

def test_workday_parse_deferred_is_not_a_failure():
    from hireshire.scrapers.workday import _WorkdayUrls, _parse_job

    urls = _WorkdayUrls("acme|wd5|careers")
    entry = {
        "title": "Software Engineer",
        "externalPath": "/job/req-123",
        "bulletFields": ["REQ-123"],
        "locationsText": "Remote",
        "postedOn": "Posted Today",
    }
    now = datetime.now(timezone.utc)

    deferred = _parse_job("acme|wd5|careers", urls, entry, None, now, deferred=True)
    assert deferred.content_text is None
    assert deferred.detail_fetch_failed is False   # deferred, not failed
    assert deferred.detail_path == "/job/req-123"  # key the funnel needs to re-fetch

    failed = _parse_job("acme|wd5|careers", urls, entry, None, now, deferred=False)
    assert failed.detail_fetch_failed is True       # detail was expected but absent


def test_bamboohr_parse_deferred_is_not_a_failure():
    from hireshire.scrapers.bamboohr import _parse_job

    entry = {"id": 42, "jobOpeningName": "Backend Engineer", "location": {"city": "NYC"}}
    now = datetime.now(timezone.utc)

    deferred = _parse_job("acme", entry, None, now, deferred=True)
    assert deferred.content_text is None
    assert deferred.detail_fetch_failed is False
    assert deferred.job_id == "42"

    failed = _parse_job("acme", entry, None, now, deferred=False)
    assert failed.detail_fetch_failed is True


def test_hydration_validate_strips_html():
    """The scrapers' fetch_detail rebuilds the Job through validation so the
    content_text HTML->text stripping runs. Assert that round-trip strips markup."""
    job = make_job("Software Engineer", source="workday", content_text=None)
    rebuilt = job.model_validate({
        **job.model_dump(),
        "content_text": "<p>Build <b>backend</b> services</p>",
        "detail_fetch_failed": False,
    })
    assert rebuilt.content_text == "Build backend services"
