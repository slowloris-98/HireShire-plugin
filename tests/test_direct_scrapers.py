"""Offline tests for the direct career-portal scrapers.

Everything here parses fixture strings — no network. The fixtures are trimmed
copies of what each portal actually served, so a handler regression shows up
without waiting on a live scrape.

The raw->staged normalisation tests that used to live here went with
hireshire/direct/normalize.py, which only served the browser-driven
/scrape-direct path that this plugin does not ship.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hireshire.direct.locations import infer_country, normalize_location
from hireshire.direct.staging import make_job_id
from hireshire.scrapers.handlers import apple, google, intuit

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
    ("Dublin, Ireland", None),
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
    for raw in ("Dublin, Ireland", "Hong Kong"):
        assert not any(t in normalize_location(raw).lower() for t in terms), raw


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
