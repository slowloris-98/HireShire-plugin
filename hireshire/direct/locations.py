"""Country normalisation for direct career-portal locations.

`scraper.py` filters jobs with a case-insensitive substring match of
`settings.location_filter` against `job.location.name`. The configured terms are
country-level ("united states", "india"), but these portals print city+state
with no country at all:

    Google  -> "Mountain View, CA, USA"
    Intuit  -> "Atlanta, Georgia"
    Meta    -> "Menlo Park, CA"

None of those contain "united states", so an unnormalised pass drops *every* US
job. Rather than special-case the filter, each handler runs its raw location
through `normalize_location`, which appends the inferred country. The existing
filter in `scraper.py` then works untouched.
"""

from __future__ import annotations

import re

US_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
)

# "City, ST" / "City, ST +3 locations". Anchored on a comma so it can't fire on
# a bare two-letter word elsewhere in the string.
_US_ABBREV = re.compile(
    r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])(?=\b|\s|,|$)"
)

INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "new delhi", "delhi",
    "noida", "gurgaon", "gurugram", "pune", "chennai", "kolkata", "ahmedabad",
)

UNITED_STATES = "United States"
INDIA = "India"

# Already-present country markers — never append a duplicate.
_US_MARKERS = ("united states", "usa", "u.s.a", "u.s.")
_IN_MARKERS = ("india",)


def infer_country(raw: str) -> str | None:
    """Best-effort country for a portal location string, or None if unknown."""
    if not raw:
        return None
    low = raw.lower()

    if any(m in low for m in _IN_MARKERS) or any(c in low for c in INDIA_CITIES):
        return INDIA
    if any(m in low for m in _US_MARKERS):
        return UNITED_STATES
    if any(s in low for s in US_STATES):
        return UNITED_STATES
    if _US_ABBREV.search(raw):
        return UNITED_STATES
    return None


def normalize_location(raw: str) -> str:
    """Append the inferred country when the portal omitted it.

    Leaves the string alone when the country is already named, when it cannot be
    inferred (so genuinely foreign locations like "Dublin, Ireland" stay
    unmatched and get filtered out), or when the input is empty.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    country = infer_country(raw)
    if country is None:
        return raw

    low = raw.lower()
    if country == INDIA and any(m in low for m in _IN_MARKERS):
        return raw
    if country == UNITED_STATES and any(m in low for m in _US_MARKERS):
        # "Mountain View, CA, USA" already reads as US to a human, but the
        # configured filter term is "united states" — spell it out.
        return f"{raw}, {UNITED_STATES}"

    return f"{raw}, {country}"
