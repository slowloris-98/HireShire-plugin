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


def test_title_keywords_match_whole_words_not_substrings(patched_funnel):
    """Excluding "intern" must not retire Internal Tools Developer.

    A `title_excluded` drop is a verdict, not a skip — it is absent from
    `_RETRYABLE_SKIP_REASONS`, so the job lands in `seen_jobs` and is never
    reconsidered. Under the old substring test one over-broad term silently deleted a
    slice of the user's market for the life of the install, and the terms that did it
    were ordinary: "ios" matched Kiosk, "mobile" matched Automobile."""
    title_cfg = TitleFilterConfig(include_keywords=[], exclude_keywords=["intern", "ios"])
    cfg = FunnelConfig(enabled=True, encoder={"targets": []})  # isolate the keyword gate
    jobs = [
        make_job("Intern", content_text="x"),
        make_job("Data Intern (Summer)", content_text="x"),
        make_job("iOS Engineer", content_text="x"),
        make_job("Internal Tools Developer", content_text="x"),
        make_job("Internship Program Coordinator", content_text="x"),
        make_job("Kiosk Manager", content_text="x"),
    ]

    async def go():
        async with Funnel(cfg, title_cfg, RUN_ID) as f:
            return await f.process(jobs)

    to_score, filtered, _ = _run(go())

    assert {r.title for r in filtered} == {"Intern", "Data Intern (Summer)", "iOS Engineer"}
    assert {r.skip_reason for r in filtered} == {"title_excluded"}
    # Nothing is stemmed: the plural and the -ship noun are different words.
    assert {j.title for j in to_score} == {
        "Internal Tools Developer",
        "Internship Program Coordinator",
        "Kiosk Manager",
    }


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


# Lever splits a posting across prose fields and a structured `lists` array. The
# stated years-of-experience lives in the latter, and nothing duplicates it into the
# former, so a parse that reads only the prose leaves the YoE gate with nothing.
LEVER_ENTRY = {
    "id": "abc-123",
    "text": "Senior Data Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "categories": {"location": "Remote", "team": "Data"},
    "opening": "<p>About Acme: we build pipelines.</p>",
    "description": "<p>You will own the warehouse.</p>",
    "additional": "<p>Acme is an equal opportunity employer.</p>",
    "lists": [
        {
            "text": "What You'll Bring to the Team",
            "content": "<ul><li>5+ years of production data engineering</li></ul>",
        },
        {"text": "Nice-to-haves", "content": "<ul><li>dbt</li></ul>"},
    ],
}


def test_lever_carries_the_lists_the_yoe_gate_reads():
    """The regression that matters: the requirement is only in `lists`."""
    from hireshire.scrapers.lever import _parse_job

    job = _parse_job("acme", LEVER_ENTRY, datetime.now(timezone.utc))

    assert "5+ years" in job.content_text
    # The section heading survives, so the cross-encoder sees which part of the
    # posting a requirement came from.
    assert "What You'll Bring to the Team" in job.content_text
    # Nothing the old three-field concat captured was displaced by the reordering.
    assert "we build pipelines" in job.content_text
    assert "own the warehouse" in job.content_text
    assert "equal opportunity" in job.content_text
    # Every later list is included, not just the first.
    assert "dbt" in job.content_text
    # strip_html ran, so no markup reaches the reranker or the judge.
    assert "<" not in job.content_text


def test_lever_content_assembly_edges():
    """A posting whose whole body is in `lists` is the case that gains most; the
    absent/empty/malformed shapes must not raise, because an escape from here fails
    the whole slug rather than the one posting."""
    from hireshire.scrapers.lever import _content

    # The near-blank tail: opening echoes the title, everything else is a list.
    body_only = {
        "opening": "Data Engineer",
        "lists": [{"text": "Requirements", "content": "<ul><li>10 years</li></ul>"}],
    }
    assert "10 years" in _content(body_only)

    assert _content({"lists": []}) is None
    assert _content({"lists": None}) is None
    assert _content({}) is None
    # A list element that is not a dict is skipped, not raised on.
    assert _content({"description": "<p>x</p>", "lists": ["junk", None]}) == "<p>x</p>"
    # A section with no content contributes nothing, heading included.
    assert _content({"lists": [{"text": "Empty", "content": ""}]}) is None
    # A section with no heading still contributes its body.
    assert _content({"lists": [{"content": "<ul><li>3+ years</li></ul>"}]}) == (
        "<ul><li>3+ years</li></ul>"
    )


def test_lever_fields_stay_separated_after_stripping():
    """The fragments are joined, so adjacent sections cannot fuse into one word
    once the markup is stripped."""
    from hireshire.scrapers.lever import _parse_job

    entry = {
        **LEVER_ENTRY,
        "opening": "ends-here",
        "description": "starts-here",
        "additional": "",
        "lists": [],
    }
    job = _parse_job("acme", entry, datetime.now(timezone.utc))
    assert "ends-herestarts-here" not in job.content_text
    assert "ends-here starts-here" == job.content_text


# Greenhouse returns `content` HTML-entity-escaped. `Job.strip_html` makes a single
# BeautifulSoup pass, which on escaped input decodes the entities and leaves the tags
# as visible text -- so the reranker and the judge read tag soup unless the scraper
# unescapes first.
GREENHOUSE_ENTRY = {
    "id": 77001,
    "title": "Staff Platform Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/77001",
    "updated_at": "2026-09-01T12:00:00-04:00",
    "location": {"name": "Remote - US"},
    "content": (
        "&lt;h2&gt;&lt;strong&gt;About Acme&lt;/strong&gt;&lt;/h2&gt;\n"
        "&lt;p&gt;We run the platform.&lt;/p&gt;\n"
        "&lt;ul&gt;&lt;li&gt;5+ years of backend experience&lt;/li&gt;"
        "&lt;li&gt;Go &amp;amp; Kubernetes&lt;/li&gt;&lt;/ul&gt;"
    ),
}


def test_greenhouse_escaped_markup_does_not_reach_the_model():
    """The regression that matters: escaped content left tags as literal text."""
    from hireshire.scrapers.greenhouse import _parse_job

    job = _parse_job("acme", GREENHOUSE_ENTRY, None,
                     datetime.now(timezone.utc), detail_required=False)
    text = job.content_text

    # No markup survives, in either form.
    assert "<" not in text and ">" not in text
    assert "&lt;" not in text and "&gt;" not in text and "&amp;" not in text
    # The posting itself does survive, requirement included.
    assert "About Acme" in text
    assert "We run the platform." in text
    assert "5+ years" in text
    # A nested entity decodes exactly once: `&amp;amp;` -> `&amp;` -> `&`.
    assert "Go & Kubernetes" in text


def test_greenhouse_one_unescape_pass_is_the_contract():
    """Pin that a single pass is enough, so nobody later 'hardens' this into a loop
    -- looping would decode text that legitimately contains an escaped tag."""
    import html as html_mod

    from hireshire.scrapers.greenhouse import _parse_job

    now = datetime.now(timezone.utc)
    once = _parse_job("acme", GREENHOUSE_ENTRY, None, now, detail_required=False)
    twice = _parse_job(
        "acme",
        {**GREENHOUSE_ENTRY, "content": html_mod.unescape(GREENHOUSE_ENTRY["content"])},
        None, now, detail_required=False,
    )
    assert once.content_text == twice.content_text


def test_greenhouse_already_unescaped_content_still_strips():
    """A board returning real HTML is handled by the same line: unescape is a no-op
    when there are no entities, and strip_html removes the tags as it always did."""
    from hireshire.scrapers.greenhouse import _parse_job

    job = _parse_job(
        "acme",
        {**GREENHOUSE_ENTRY, "content": "<p>Plain <b>HTML</b> body</p>"},
        None, datetime.now(timezone.utc), detail_required=False,
    )
    assert job.content_text == "Plain HTML body"


def test_greenhouse_falls_back_to_detail_content_and_unescapes_it():
    from hireshire.scrapers.greenhouse import _parse_job

    now = datetime.now(timezone.utc)
    entry = {k: v for k, v in GREENHOUSE_ENTRY.items() if k != "content"}
    detail = {"content": "&lt;p&gt;Detail body with 7+ years&lt;/p&gt;", "questions": []}

    job = _parse_job("acme", entry, detail, now)
    assert job.content_text == "Detail body with 7+ years"
    assert job.detail_fetch_failed is False

    # No content anywhere: nothing to store, and the detail flag still reports the
    # fetch, not the content.
    missing = _parse_job("acme", entry, None, now, detail_required=True)
    assert missing.content_text is None
    assert missing.detail_fetch_failed is True

    deferred = _parse_job("acme", entry, None, now, detail_required=False)
    assert deferred.content_text is None
    assert deferred.detail_fetch_failed is False

