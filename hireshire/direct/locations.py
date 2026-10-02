"""Rolling an employer's location string up to a country and US state.

`scraper._matches_location` compares `settings.location_filter` against a job's
location as a case-insensitive substring. The two sides are written by different
people and almost never agree: a user's terms are country- or state-level
("united states", "california"), while employers print city+abbreviation.

    Google  -> "Mountain View, CA, USA"
    Intuit  -> "Atlanta, Georgia"
    Lever   -> "Foster City, CA"

None of those contains "united states" and only one contains "california", so a
raw substring test drops the job. It did: on one real install **15% of every job
scraped** was discarded this way, including all 236 of Zoox's postings bar the
few whose alternate office happened to be a city someone had typed out by hand.

Two entry points, and the difference matters:

* `normalize_location` appends the inferred **country** and nothing else. The six
  direct-portal handlers call it at parse time and **store** what it returns, so
  its output is a location a user reads. Do not widen it.
* `filter_haystack` is `scraper._matches_location`'s own text — country, spelled-out
  state, and every alias of the country. Never stored, never displayed, matched
  against only. New synonyms belong here, not in `normalize_location`.

Countries, regions and cities come from `portal_locations.COUNTRIES`, the same
table the portals are scoped from — a country the sweep can search for is one
this module can name. Every name is matched as a whole word: a plain substring
test read "Indianapolis, Indiana" as India and "Busan" (which contains "usa") as
the United States. The same rule is what lets `_US_MARKER_RE` carry bare "us"
without claiming Belarus, Cyprus and Aarhus.

`infer_country` answers with ONE country, trying India, then the US, then the rest
in table order. That order is load-bearing: it keeps "New Mexico", "Scotland, PA"
and "Holland, MI" American, and it is why a US city added to the table must be
qualified ("chantilly, va") when a foreign city shares its name. A string naming
two countries resolves to whichever is tried first, so "United States | India"
resolves to India and carries India's aliases — it still matches a literal
"united states" term from the raw text, but not "usa".
"""

from __future__ import annotations

import functools
import re

from hireshire.direct.portal_locations import (
    BY_NAME, COUNTRIES, INDIA_CITIES, US_STATE_ABBREVS, US_STATE_BY_ABBREV, US_STATES,
)

__all__ = ["INDIA", "INDIA_CITIES", "UNITED_STATES", "US_STATES",
           "filter_haystack", "infer_country", "normalize_location"]

UNITED_STATES = "United States"
INDIA = "India"

# "City, ST" / "City, ST +3 locations". Anchored on a comma so it can't fire on
# a bare two-letter word elsewhere in the string.
_US_ABBREV = re.compile(r",\s*(" + "|".join(US_STATE_ABBREVS) + r")(?=\b|\s|,|$)")


def _words(terms) -> re.Pattern:
    """One pattern matching any of `terms` as a whole word, on lowercased text."""
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(?:" + alternation + r")(?![a-z])")


_US = BY_NAME[UNITED_STATES]
# Already-present US spellings — a string carrying one needs no inference. Taken from
# the table's own aliases so this cannot drift from `scope._LOOKUP`, which reads the
# same tuple. A hand-copy did drift: it lacked bare "us", so "Remote - US",
# "US Remote" and "Remote (US)" — thousands of real postings — inferred nothing.
# `_words` keeps every one whole-word, so "Belarus", "Aarhus" and "Cyprus" are safe.
_US_MARKER_RE = _words(_US.aliases)
_US_REGION_RE = _words(_US.regions)
_US_CITY_RE = _words(_US.cities)
_INDIA_RE = _words(("india",) + BY_NAME[INDIA].regions + INDIA_CITIES)
# Aliases are included: without them "Bristol, UK", "Leeds, England" and
# "Rotterdam, Holland" inferred nothing, so a user scoped to a country lost every city
# of it outside the handful listed. `test_no_term_maps_to_two_countries_by_accident`
# already pins every term here as globally unique, so this cannot make a location
# ambiguous between two countries — it only changes which stage of `infer_country`
# answers, and the India -> US -> others order protects "New Mexico", "Scotland, PA"
# and "Holland, MI".
_OTHERS = tuple(
    (c.name, _words((c.name.lower(),) + c.aliases + c.regions + c.cities))
    for c in COUNTRIES if c.name not in (UNITED_STATES, INDIA)
)


@functools.lru_cache(maxsize=8192)
def infer_country(raw: str) -> str | None:
    """Best-effort country for a portal location string, or None if unknown.

    Cached because the gate calls it once per job per office while the whole
    corpus is a small vocabulary — one real install had 10,836 distinct location
    strings behind 447,742 jobs. Bounded rather than unbounded: Meta's
    "+3 locations" suffixes mint new strings indefinitely in a long-lived monitor.
    A test that monkeypatches the tables must call `.cache_clear()`.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    low = raw.lower()

    if _INDIA_RE.search(low):
        return INDIA
    if _US_MARKER_RE.search(low) or _US_REGION_RE.search(low):
        return UNITED_STATES
    if _US_ABBREV.search(raw) or _US_CITY_RE.search(low):
        return UNITED_STATES
    for name, pattern in _OTHERS:
        if pattern.search(low):
            return name
    return None


@functools.lru_cache(maxsize=8192)
def normalize_location(raw: str) -> str:
    """Append the inferred country when the portal did not spell it out.

    Leaves the string alone when the country's own name is already in it, when
    it cannot be inferred (so a location outside the table stays unmatched and
    gets filtered out), or when the input is empty. "Mountain View, CA, USA"
    still gains ", United States": it reads as US to a human, but a filter term
    of "united states" is not a substring of "usa".
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    country = infer_country(raw)
    if country is None or country.lower() in raw.lower():
        return raw
    return f"{raw}, {country}"


@functools.lru_cache(maxsize=8192)
def filter_haystack(raw: str) -> str:
    """Lowercased location text for `scraper._matches_location`. MATCHING ONLY.

    `normalize_location` plus the full name of a US state the employer wrote as an
    abbreviation, so a filter term of "california" matches "Foster City, CA". The
    country alone is not enough: a user who names a state rather than a country
    matched ~11% of one real install's jobs, against ~28% once the state is spelled
    out.

    Deliberately NOT folded into `normalize_location`, whose return value the six
    direct-portal handlers *store* on the job and whose exact output is pinned by
    tests. This string is never stored or displayed — it exists to be matched
    against and is lowercased for that one caller.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    out = normalize_location(raw)
    match = _US_ABBREV.search(raw)
    if match:
        state = US_STATE_BY_ABBREV.get(match.group(1))
        if state and state not in out.lower():
            out = f"{out}, {state}"

    # Every spelling of the country, because the filter term is whatever the user
    # typed: "usa", "us" and "america" are not substrings of "United States". The
    # terms are matched as written (setup records them verbatim), so the haystack
    # is what has to carry the synonyms.
    country = infer_country(raw)
    if country is not None:
        low = out.lower()
        extra = [a for a in BY_NAME[country].aliases if a not in low]
        if extra:
            out = out + ", " + ", ".join(extra)
    return out.lower()
