"""Offline tests for the direct career-portal scrapers.

Everything here parses fixture strings — no network. The fixtures are trimmed
copies of what each portal actually served, so a handler regression shows up
without waiting on a live scrape.

The raw->staged normalisation tests that used to live here went with
hireshire/direct/normalize.py, which only served the browser-driven
/scrape-direct path that this plugin does not ship.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hireshire.direct.locations import infer_country, normalize_location
from hireshire.direct.staging import make_job_id
from hireshire.scrapers.handlers import amazon, apple, google, intuit, meta, microsoft

NOW = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# location normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Atlanta, Georgia", "United States"),           # Intuit: full state name
    ("Mountain View, CA, USA", "United States"),      # Google: abbrev + USA
    ("Menlo Park, CA +3 locations", "United States"),  # Meta: abbrev + suffix
    ("United States, Washington, Redmond", "United States"),  # Microsoft
    ("Bengaluru, Karnataka, India", "India"),
    ("India", "India"),
    ("Dublin, Ireland", "Ireland"),                  # any country in the table
    ("Toronto, Ontario", "Canada"),
    ("Indianapolis, Indiana", "United States"),       # whole words: not India
    ("Busan, South Korea", "South Korea"),            # whole words: "usa" is in Busan
    ("Albuquerque, New Mexico", "United States"),     # the state, not Mexico
    ("Hong Kong", None),
    ("", None),
])
def test_infer_country(raw, expected):
    assert infer_country(raw) == expected


def test_normalize_appends_country_so_the_existing_filter_matches():
    terms = ["united states", "remote", "india"]
    # The whole point: these three would ALL fail a naive substring match.
    for raw in ("Atlanta, Georgia", "Mountain View, CA, USA", "Menlo Park, CA"):
        assert any(t in normalize_location(raw).lower() for t in terms), raw


def test_normalize_leaves_foreign_locations_unmatched():
    terms = ["united states", "remote", "india"]
    for raw in ("Dublin, Ireland", "Hong Kong", "London, UK"):
        assert not any(t in normalize_location(raw).lower() for t in terms), raw


def test_normalize_names_every_table_country_so_a_country_filter_matches():
    assert normalize_location("London, UK") == "London, UK, United Kingdom"
    assert normalize_location("Toronto, Ontario") == "Toronto, Ontario, Canada"
    assert normalize_location("Dublin, Ireland") == "Dublin, Ireland"


def test_normalize_does_not_append_a_country_the_string_already_names():
    raw = "United States, Washington, Redmond"
    assert normalize_location(raw) == raw
    # "USA" reads as US to a human but is not a substring of "united states".
    assert normalize_location("Mountain View, CA, USA") == "Mountain View, CA, USA, United States"


def test_normalize_does_not_duplicate_an_existing_country():
    assert normalize_location("Bengaluru, India") == "Bengaluru, India"
    assert normalize_location("") == ""


# --------------------------------------------------------------------------
# handler parsing
# --------------------------------------------------------------------------

APPLE_HTML = (
    'window.__staticRouterHydrationData = JSON.parse(' + json.dumps(json.dumps({
        "loaderData": {"root": {"searchResults": [{
            "positionId": "200676458",
            "postingTitle": "Display Algorithm Engineer",
            "transformedPostingTitle": "display-algorithm-engineer",
            "jobSummary": "The Display Intelligence team ...",
            "postingDate": "Aug 06, 2026",
            # deliberately the request time — must be ignored
            "postDateInGMT": "2026-08-07T02:06:08.986570653Z",
            "reqId": "200676458-3401",
            "team": {"teamName": "Hardware"},
            "locations": [{"city": "Cupertino", "stateProvince": "California",
                           "countryName": "United States of America"}],
        }]}}
    })) + ');'
)


def test_apple_parses_hydration_blob():
    jobs = apple._extract(APPLE_HTML, NOW)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == make_job_id("apple", "200676458")
    assert job.source == "direct" and job.board_token == "apple"
    assert str(job.absolute_url).endswith("/200676458/display-algorithm-engineer")
    assert job.content_text, "Apple carries its description in the list payload"
    assert "United States" in job.location.name


def test_apple_ignores_postDateInGMT():
    """postDateInGMT is the server's 'now' — using it makes every job look new."""
    job = apple._extract(APPLE_HTML, NOW)[0]
    assert job.updated_at.date() == datetime(2026, 8, 6).date()


def test_apple_tolerates_missing_hydration():
    assert apple._extract("<html><body>nothing here</body></html>", NOW) == []


GOOGLE_HTML = """
<html><body>
<a href="jobs/results/95656825013052102-product-manager-pixel-ai-hardware?page=1">x</a>
<a href="jobs/results/113671223522534086-field-sales-manager-iii?page=1">y</a>
<a href="jobs/results/95656825013052102-product-manager-pixel-ai-hardware?page=1">dup</a>
</body></html>
"""


def test_google_regex_finds_ids_that_a_dom_parse_would_miss():
    pairs = list(dict.fromkeys(google._RESULT.findall(GOOGLE_HTML)))
    assert len(pairs) == 2, "must dedupe repeated anchors"
    assert pairs[0][0] == "95656825013052102"


def test_google_slug_becomes_a_title_the_gate_can_match():
    job = google._parse_job("123", "software-engineer-iii-performance", NOW)
    assert "software engineer" in job.title.lower()
    assert job.detail_path == "software-engineer-iii-performance"
    assert job.content_text is None, "Google defers its description to the funnel"


def test_google_list_location_survives_the_scraper_location_filter():
    """Regression: the list exposes no per-job location. Emitting "N/A" made
    scraper.py's filter drop the entire board before the funnel could hydrate it."""
    from scraper import _matches_location

    job = google._parse_job("123", "software-engineer", NOW)
    assert _matches_location(job, ["united states", "remote", "india"])


INTUIT_FRAGMENT = """
<ul id="search-results-list">
  <li><a class="sr-item" data-job-id="23185"
         href="/job/san-diego/manager-2-software-engineer/27595/98862572160"
         data-title="Manager 2, Software Engineer">
      <span class="job-location">San Diego, California</span></a></li>
</ul>
"""


def test_intuit_parses_the_html_fragment():
    from bs4 import BeautifulSoup
    anchor = BeautifulSoup(INTUIT_FRAGMENT, "lxml").select_one("a.sr-item[data-job-id]")
    job = intuit._parse_job(anchor, NOW)
    assert job.job_id == make_job_id("intuit", "23185")
    assert job.title == "Manager 2, Software Engineer"
    assert str(job.absolute_url).startswith("https://jobs.intuit.com/job/")
    assert "United States" in job.location.name


class _Response:
    def __init__(self, text: str = "", payload=None):
        self.text = text if payload is None else json.dumps(payload)
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _Ctx:
    """Stands in for DirectScraper: canned responses, every request recorded."""

    def __init__(self, *responses, max_pages=5, cutoff=None, scope=None):
        self._responses = list(responses)
        self.max_pages, self.cutoff, self.scope = max_pages, cutoff, scope
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, None))
        return self._responses.pop(0) if self._responses else _Response(payload={})

    async def post(self, url, data, headers=None):
        self.calls.append(("POST", url, {**data, **(headers or {})}))
        return self._responses.pop(0)


AMAZON_ENTRY = {
    "id_icims": "10558337",
    "title": "SCE - Deal Lead, Startups ",
    "posted_date": "August  6, 2026",
    "updated_time": "13 minutes",   # relative, moves on every edit — must be ignored
    "normalized_location": "San Francisco, California, USA",
    "location": "US, CA, San Francisco",
    "job_path": "/en/jobs/10558337/sce-deal-lead-startups",
    "job_category": "Sales, Advertising, & Account Management",
    "description": "Amazon Web Services (AWS) is seeking a Deal Lead.<br/>",
    "basic_qualifications": "- 7+ years of experience working with customers",
    "preferred_qualifications": "- MBA",
}


def test_amazon_parses_the_search_json():
    job = amazon._extract({"jobs": [AMAZON_ENTRY]}, NOW)[0]
    assert job.job_id == make_job_id("amazon", "10558337")
    assert job.title == "SCE - Deal Lead, Startups"
    assert str(job.absolute_url) == (
        "https://www.amazon.jobs/en/jobs/10558337/sce-deal-lead-startups")
    assert "United States" in job.location.name
    assert job.updated_at.date() == datetime(2026, 8, 6).date()


def test_amazon_carries_the_qualifications_the_yoe_gate_reads():
    job = amazon._extract({"jobs": [AMAZON_ENTRY]}, NOW)[0]
    assert "7+ years" in job.content_text and "Deal Lead" in job.content_text
    assert "<br" not in job.content_text


def test_amazon_stops_paging_once_a_whole_page_is_past_the_cutoff():
    old = {"jobs": [AMAZON_ENTRY]}
    ctx = _Ctx(_Response(payload=old), _Response(payload=old),
               cutoff=NOW + timedelta(days=30))
    jobs = asyncio.run(amazon.fetch_list(ctx, "amazon"))
    assert len(jobs) == 1 and len(ctx.calls) == 1


MICROSOFT_ENTRY = {
    "id": 1970393557002687,
    "displayJobId": "200057083",
    "name": "Senior Media Systems & Engineer Lead",
    "locations": ["United States, Washington, Redmond"],
    "standardizedLocations": ["US"],   # the short form names no country
    "postedTs": 1786068000,
    "department": "Technical Support Engineering",
}


def test_microsoft_reads_the_long_location_and_posting_time():
    job = microsoft._parse_job(MICROSOFT_ENTRY, NOW)
    assert job.job_id == make_job_id("microsoft", "1970393557002687")
    assert "United States" in job.location.name
    assert job.updated_at == datetime.fromtimestamp(1786068000, tz=timezone.utc)
    assert job.requisition_id == "200057083"
    assert job.content_text is None, "Microsoft defers its description to the funnel"
    assert str(job.absolute_url).endswith("/careers/job/1970393557002687")


def test_microsoft_walks_each_country_and_dedupes():
    from hireshire.direct.scope import resolve_scope
    page = _Response(payload={"data": {"positions": [MICROSOFT_ENTRY]}})
    empty = _Response(payload={"data": {"positions": []}})
    ctx = _Ctx(page, empty, page, empty, scope=resolve_scope(["united states", "india"]))
    jobs = asyncio.run(microsoft.fetch_list(ctx, "microsoft"))
    assert len(jobs) == 1
    assert [u for _, u, _ in ctx.calls if "start=0" in u] == [
        microsoft.list_url("United States", 1), microsoft.list_url("India", 1)]


def test_microsoft_detail_fills_the_description():
    job = microsoft._parse_job(MICROSOFT_ENTRY, NOW)
    html = "<b>Overview</b><div><p>" + "Lead media engineering. " * 20 + "</p></div>"
    ctx = _Ctx(_Response(payload={"data": {"jobDescription": html}}))
    out = asyncio.run(microsoft.fetch_detail(ctx, job))
    assert not out.detail_fetch_failed and out.content_text.startswith("Overview")
    assert "position_id=1970393557002687" in ctx.calls[0][1]


META_PAGE = '<script>...["LSD",[],{"token":"AdS4sleY88c6e6f"},323]...</script>'
META_PAYLOAD = {"data": {"job_search_with_featured_jobs_v2": {
    "all_jobs": [
        {"id": "1747806499828365",
         "title": "Research Scientist,\nMachine Learning for Monetization (PhD)",
         "locations": ["Sunnyvale, CA", "Bengaluru, India"],
         "teams": ["AI Research"], "sub_teams": ["Artificial Intelligence"]},
    ],
    "featured_jobs": [
        {"id": "1747806499828365", "title": "dup", "locations": ["Menlo Park, CA"]},
        {"id": "1690022942358388", "title": "Critical Facility Engineer",
         "locations": ["Henrico, VA"], "teams": []},
    ],
}}}


def test_meta_reads_the_lsd_token_and_posts_it_twice():
    ctx = _Ctx(_Response(META_PAGE), _Response(payload=META_PAYLOAD))
    jobs = asyncio.run(meta.fetch_list(ctx, "meta"))
    method, url, sent = ctx.calls[1]
    assert (method, url) == ("POST", meta.GRAPHQL)
    assert sent["lsd"] == sent["X-FB-LSD"] == "AdS4sleY88c6e6f"
    assert sent["doc_id"] == meta.DOC_ID
    assert json.loads(sent["variables"])["search_input"]["offices"] == [], (
        "a wrong office name empties the board silently; never scope server-side")
    assert len(jobs) == 2, "featured jobs are merged in, once each"


def test_meta_title_newline_is_collapsed_and_locations_resolve():
    job = meta.extract(META_PAYLOAD, NOW)[0]
    assert job.title == "Research Scientist, Machine Learning for Monetization (PhD)"
    assert job.job_id == make_job_id("meta", "1747806499828365")
    assert str(job.absolute_url) == "https://www.metacareers.com/jobs/1747806499828365/"
    assert "India" in job.location.name


def test_meta_never_returns_an_empty_board_for_a_broken_search():
    """A stale doc_id or a missing token must read as an error, not as
    "Meta has no jobs today"."""
    with pytest.raises(meta.MetaSearchError):
        asyncio.run(meta.fetch_list(_Ctx(_Response("<html></html>")), "meta"))
    with pytest.raises(meta.MetaSearchError):
        meta.extract({"errors": [{"message": "PersistedQueryNotFound"}]}, NOW)


def test_meta_detail_reads_the_json_ld_posting():
    job = meta.extract(META_PAYLOAD, NOW)[0]
    posting = {
        "@context": "http://schema.org/", "@type": "JobPosting",
        "description": "From making valuable connections " * 10,
        "responsibilities": "Develop highly scalable classifiers.",
        "qualifications": "3+ years of experience in machine learning.",
        "datePosted": "2026-08-06T11:09:21-07:00",
    }
    html = '<script type="application/ld+json">' + json.dumps(posting) + "</script>"
    out = asyncio.run(meta.fetch_detail(_Ctx(_Response(html)), job))
    assert not out.detail_fetch_failed
    assert "3+ years" in out.content_text and "Qualifications" in out.content_text
    assert out.updated_at == datetime(2026, 8, 6, 18, 9, 21, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# cross-cutting guarantees
# --------------------------------------------------------------------------

def test_job_ids_are_namespaced_against_cross_platform_collision():
    """seen_jobs is keyed on job_id alone, so a bare portal id would collide."""
    assert make_job_id("intuit", "23185") == "direct:intuit:23185"
    assert make_job_id("apple", "23185") != make_job_id("intuit", "23185")


def test_direct_scraper_never_raises_slug_not_found():
    """A single-tenant portal has no wrong slug; SlugNotFoundError would prune it
    into bad_slugs.json permanently on one transient failure."""
    import inspect
    from hireshire.scrapers import direct
    src = inspect.getsource(direct)
    assert "raise SlugNotFoundError" not in src
