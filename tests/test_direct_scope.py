"""The direct portals are searched for the user's own countries.

Offline: every URL here is built, never fetched. The US + India URLs are pinned
character for character to the ones the handlers hard-coded before scoping
existed, so an install whose list resolves to those two sweeps exactly as before.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from hireshire.direct.portal_locations import COUNTRIES
from hireshire.direct.scope import EVERYWHERE, WORLDWIDE, Scope, resolve_scope
from hireshire.scrapers.handlers import apple, google, intuit
from scraper import _matches_location

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

# The list /hireshire:setup drafted on a real install (107 terms): countries,
# states, metros and remote spellings mixed together.
SETUP_LIST = [
    "united states", "usa", "u.s.", "remote - us", "remote, us", "remote (us",
    "us remote", "us-remote", "alabama", "alaska", "arizona", "arkansas",
    "california", "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "wisconsin", "wyoming", "san francisco",
    "san jose", "bay area", "mountain view", "sunnyvale", "palo alto",
    "santa clara", "menlo park", "cupertino", "redwood city", "san mateo",
    "oakland", "los angeles", "san diego", "seattle", "redmond", "bellevue",
    "nyc", "boston", "cambridge, ma", "chicago", "austin", "dallas", "houston",
    "denver", "boulder", "atlanta", "raleigh", "durham", "pittsburgh",
    "philadelphia", "miami", "salt lake", "phoenix", "minneapolis", "detroit",
    "portland", "india", "bengaluru", "bangalore", "hyderabad", "pune",
    "mumbai", "delhi", "noida", "gurgaon", "gurugram", "chennai", "kolkata",
    "ahmedabad",
]

US_IN = resolve_scope(["united states", "india"])


# --------------------------------------------------------------------------
# resolving the user's list
# --------------------------------------------------------------------------

def test_the_setup_list_resolves_to_exactly_todays_scope():
    assert len(SETUP_LIST) == 107
    scope = resolve_scope(SETUP_LIST)
    assert scope.countries == {"United States", "India"}
    assert scope.unresolved == ()


@pytest.mark.parametrize("terms,expected", [
    (["berlin"], ["Germany"]),
    (["georgia"], ["United States"]),        # the state, never the country
    (["london", "berlin"], ["United Kingdom", "Germany"]),
    (["cambridge, ma"], ["United States"]),
    (["austin, tx"], ["United States"]),
    (["pune, india"], ["India"]),
    (["remote - us"], ["United States"]),
    (["  Germany "], ["Germany"]),
])
def test_terms_resolve_to_countries(terms, expected):
    assert resolve_scope(terms).ordered == expected


@pytest.mark.parametrize("terms", [
    [],
    ["", "  "],
    ["remote"],                     # no country in it
    ["united states", "mars"],      # one unknown term widens the whole scope
    ["berlin, de"],                 # Berlin (Germany) vs DE (Delaware): conflict
])
def test_anything_unresolvable_means_everywhere(terms):
    scope = resolve_scope(terms)
    assert scope.countries is None
    assert scope.for_portal("apple") is None


def test_unresolved_terms_are_reported_for_the_log():
    assert resolve_scope(["united states", "mars"]).unresolved == ("mars",)


def test_a_portal_missing_one_country_is_left_unscoped_not_narrowed():
    """Intuit has no Germany facet; narrowing it to the US alone would hide the
    Germany half of the user's list."""
    scope = resolve_scope(["united states", "germany"])
    assert scope.for_portal("apple") == ["united-states-USA", "germany-DEU"]
    assert scope.for_portal("intuit") is None
    assert scope.describe("intuit") == "everywhere"


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def test_apple_us_india_url_is_unchanged():
    assert apple.list_url(US_IN, 3) == (
        "https://jobs.apple.com/en-us/search"
        "?location=united-states-USA+india-INDC&sort=newest&page=3"
    )


def test_google_us_india_url_is_unchanged():
    assert google.list_url(US_IN, 3) == (
        "https://www.google.com/about/careers/applications/jobs/results"
        "?location=United%20States&location=India&sort_by=date&page=3"
    )


def test_intuit_scopes_by_country_facet():
    url = intuit.list_url(US_IN, 2)
    assert "ActiveFacetID=6252001&CurrentPage=2" in url
    assert "FacetFilters%5B0%5D.ID=6252001" in url
    assert "FacetFilters%5B1%5D.ID=1269750" in url
    assert "FacetFilters%5B1%5D.Display=India" in url
    assert url.count("IsApplied=true") == 2


@pytest.mark.parametrize("scope", [None, EVERYWHERE])
def test_unscoped_urls_carry_no_location(scope):
    assert "location=" not in apple.list_url(scope, 1)
    assert "location=" not in google.list_url(scope, 1)
    url = intuit.list_url(scope, 1)
    assert "FacetFilters" not in url and "ActiveFacetID=0" in url


# --------------------------------------------------------------------------
# placeholder locations
# --------------------------------------------------------------------------

def test_google_placeholder_names_what_was_searched():
    assert google.search_scope(US_IN) == "United States | India"
    assert google.search_scope(resolve_scope(["london"])) == "United Kingdom"
    assert google.search_scope(None) == WORLDWIDE


@pytest.mark.parametrize("terms", [["california"], ["remote"], ["london"]])
def test_a_google_list_job_is_never_dropped_for_its_placeholder(terms):
    """City or state terms never contain a country name; the placeholder used to
    drop the entire board before the funnel could hydrate a real location."""
    job = google._parse_job("123", "software-engineer", NOW, resolve_scope(terms))
    assert job.location_is_placeholder
    assert _matches_location(job, terms)


def _intuit_anchor(location: str):
    html = (
        '<a class="sr-item" data-job-id="1" href="/job/x/y/27595/1" '
        f'data-title="Engineer"><span class="job-location">{location}</span></a>'
    )
    return BeautifulSoup(html, "lxml").select_one("a.sr-item")


def test_intuit_multiple_locations_passes_the_filter_under_a_scoped_search():
    job = intuit._parse_job(_intuit_anchor("Multiple Locations"), NOW, US_IN)
    assert job.location_is_placeholder
    assert job.location.name == "Multiple Locations (United States | India)"
    assert _matches_location(job, ["united states", "india"])


def test_an_intuit_job_with_a_real_location_is_still_filtered():
    job = intuit._parse_job(_intuit_anchor("Petach Tikva, Israel"), NOW, US_IN)
    assert not job.location_is_placeholder
    assert not _matches_location(job, ["united states", "india"])


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

def test_every_country_has_apple_and_google_values():
    for c in COUNTRIES:
        assert c.apple and c.google, c.name


def test_apple_slugs_have_the_shape_the_portal_accepts():
    for c in COUNTRIES:
        assert re.fullmatch(r"[a-z0-9-]+-[A-Z]{3,4}", c.apple), c.apple


def test_no_term_maps_to_two_countries_by_accident():
    """First writer wins in the lookup, so a duplicate silently resolves to
    whichever row comes first. The only intended overlap is none at all."""
    seen: dict[str, str] = {}
    for c in COUNTRIES:
        for term in (c.name.lower(), *c.aliases, *c.regions, *c.cities):
            assert seen.setdefault(term, c.name) == c.name, term


def test_the_scraper_logs_what_each_portal_searches(caplog):
    from hireshire.scrapers.direct import DirectScraper
    with caplog.at_level(logging.INFO, logger="hireshire.scrapers.direct"):
        DirectScraper(None, None, scope=resolve_scope(["united states", "mars"]))
    assert "no country known for location 'mars'" in caplog.text
    assert "Direct portal google searches: everywhere" in caplog.text


def test_scope_is_hashable_and_comparable():
    assert resolve_scope(["india", "usa"]) == Scope(frozenset({"India", "United States"}))
