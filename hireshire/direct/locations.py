"""Country normalisation for direct career-portal locations.

`scraper.py` filters jobs with a case-insensitive substring match of
`settings.location_filter` against `job.location.name`. Filter terms are often
country-level ("united states", "india"), but these portals print city+state
with no country at all:

    Google  -> "Mountain View, CA, USA"
    Intuit  -> "Atlanta, Georgia"
    Meta    -> "Menlo Park, CA"

None of those contain "united states", so an unnormalised pass drops *every* US
job. Rather than special-case the filter, each handler runs its raw location
through `normalize_location`, which appends the inferred country. The existing
filter in `scraper.py` then works untouched.

Countries, regions and cities come from `portal_locations.COUNTRIES`, the same
table the portals are scoped from — a country the sweep can search for is one
this module can name. Every name is matched as a whole word: a plain substring
test read "Indianapolis, Indiana" as India and "Busan" (which contains "usa") as
the United States.
"""

from __future__ import annotations

import re

from hireshire.direct.portal_locations import (
    BY_NAME, COUNTRIES, INDIA_CITIES, US_STATE_ABBREVS, US_STATES,
)

__all__ = ["INDIA", "INDIA_CITIES", "UNITED_STATES", "US_STATES",
           "infer_country", "normalize_location"]

UNITED_STATES = "United States"
INDIA = "India"

# "City, ST" / "City, ST +3 locations". Anchored on a comma so it can't fire on
# a bare two-letter word elsewhere in the string.
_US_ABBREV = re.compile(r",\s*(" + "|".join(US_STATE_ABBREVS) + r")(?=\b|\s|,|$)")

# Already-present US spellings — a string carrying one needs no inference.
_US_MARKERS = ("united states", "usa", "u.s.a", "u.s.")


def _words(terms) -> re.Pattern:
    """One pattern matching any of `terms` as a whole word, on lowercased text."""
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(?:" + alternation + r")(?![a-z])")


_US = BY_NAME[UNITED_STATES]
_US_MARKER_RE = _words(_US_MARKERS)
_US_REGION_RE = _words(_US.regions)
_US_CITY_RE = _words(_US.cities)
_INDIA_RE = _words(("india",) + BY_NAME[INDIA].regions + INDIA_CITIES)
_OTHERS = tuple(
    (c.name, _words((c.name.lower(),) + c.regions + c.cities))
    for c in COUNTRIES if c.name not in (UNITED_STATES, INDIA)
)


def infer_country(raw: str) -> str | None:
    """Best-effort country for a portal location string, or None if unknown."""
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
