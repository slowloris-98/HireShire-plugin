"""Countries the direct portals can be scoped to, and how each portal spells them.

One table serves three readers: `scope.py` resolves the user's `location_filter`
terms against the aliases, regions and cities; `locations.py` infers a country
from a portal's location string with the same names; and each handler turns a
resolved scope into its own query parameter from the portal columns.

**The portal columns are checked in, never resolved at sweep time.** Apple,
Google and Amazon answer a value they do not recognise with zero results and a
200, so a guessed code does not fail — it empties the board, silently, every
sweep. Meta has no column: it returns its whole board in one response, so there
is nothing to scope (see `handlers/meta.py`).
`scripts/refresh_direct_locations.py` re-derives them from the portals. `None`
means that portal cannot be scoped to that country, and a scope naming it
leaves that portal unscoped rather than narrowed to the wrong place.

Everything here is lowercase except `name`, the display name, which is also the
string `normalize_location` appends — so it is what a user's `location_filter`
term has to contain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    name: str                          # display name; appended by normalize_location
    aliases: tuple[str, ...] = ()      # whole-term spellings a user writes
    regions: tuple[str, ...] = ()      # states / provinces
    cities: tuple[str, ...] = ()
    apple: str | None = None           # jobs.apple.com `location=` slug
    google: str | None = None          # google careers `location=` value
    intuit: str | None = None          # jobs.intuit.com country facet id (GeoNames)
    amazon: str | None = None          # amazon.jobs `normalized_country_code[]` (ISO3)
    microsoft: str | None = None       # careers.microsoft.com `location=` (free text)


# One table for the states, so the abbreviation->name map and the two tuples below
# cannot drift apart. `locations.filter_haystack` needs the mapping: employers write
# "Foster City, CA" and a user's filter says "california", so the gate has to be able
# to spell out an abbreviation. The tuples are derived, never hand-kept.
#
# No "AS" (American Samoa): `scope._ABBREVS` lowercases these and matches them against
# the user's own comma-split terms, so "as" would become a US-resolving word.
US_STATE_BY_ABBREV: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska",
    "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey",
    "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
    "DC": "district of columbia", "PR": "puerto rico",
}

# Matched as a whole word inside a location string; order is irrelevant because
# `_words` sorts its alternation by length.
US_STATE_ABBREVS = tuple(US_STATE_BY_ABBREV)
US_STATES = tuple(US_STATE_BY_ABBREV.values())

INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "new delhi", "delhi",
    "noida", "gurgaon", "gurugram", "pune", "chennai", "kolkata", "ahmedabad",
    "mohali",
)

# Intuit has a country facet only where it currently has openings, so its column
# is sparse by fact rather than by omission; a scope naming another country leaves
# Intuit unscoped. `refresh_direct_locations.py` lists any facet with no row here.
#
# Order matters in one place only: `infer_country` tries India, then the United
# States, then the rest in this order, which keeps the pre-table behaviour for
# the two countries every existing install is scoped to.
COUNTRIES: tuple[Country, ...] = (
    Country(
        name="United States",
        aliases=("united states", "united states of america", "usa", "u.s.",
                 "u.s.a.", "u.s.a", "us", "america"),
        regions=US_STATES,
        cities=(
            "san francisco", "san jose", "bay area", "sf bay area", "mountain view",
            "sunnyvale", "palo alto", "santa clara", "menlo park", "cupertino",
            "redwood city", "san mateo", "oakland", "los angeles", "san diego",
            "irvine", "sacramento", "seattle", "redmond", "bellevue", "kirkland",
            "nyc", "new york city", "brooklyn", "boston", "cambridge, ma", "chicago",
            "austin", "dallas", "houston", "san antonio", "denver", "boulder",
            "atlanta", "raleigh", "durham", "charlotte", "pittsburgh", "philadelphia",
            "miami", "orlando", "tampa", "salt lake", "salt lake city", "phoenix",
            "minneapolis", "detroit", "portland", "nashville", "baltimore",
            "washington dc", "washington, dc", "washington d.c.", "st. louis",
            "kansas city", "columbus",
            # Added from locations real employers wrote that nothing here resolved.
            # Each is matched as a whole word, so a bare name would steal a foreign
            # city of the same name: US cities are tried BEFORE the `_OTHERS` loop in
            # `locations.infer_country`. Hence "chantilly, va" (Chantilly, France) and
            # hence no bare "trenton" (Trenton, Ontario) or "hanover" (Hanover,
            # Germany) — those two are deliberately absent.
            "el segundo", "boca raton", "elmhurst", "lehi", "newport beach",
            "long beach", "waco", "grants pass", "west olympia",
            "snowmass village", "santa barbara", "san leandro", "fremont",
            "coconut grove", "chevy chase", "hayward", "foster city", "hawthorne",
            "bastrop", "arlington", "starbase", "chantilly, va", "cape canaveral",
            "garden grove", "suitland", "fort meade", "annapolis junction",
        ),
        apple="united-states-USA", google="United States", intuit="6252001",
        amazon="USA", microsoft="United States",
    ),
    Country(
        name="India",
        aliases=("india",),
        regions=("karnataka", "maharashtra", "telangana", "tamil nadu",
                 "west bengal", "haryana", "uttar pradesh", "gujarat"),
        cities=INDIA_CITIES,
        apple="india-INDC", google="India", intuit="1269750",
        amazon="IND", microsoft="India",
    ),
    Country(
        name="Canada",
        aliases=("canada",),
        regions=("ontario", "quebec", "british columbia", "alberta"),
        cities=("toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo"),
        apple="canada-CANC", google="Canada", amazon="CAN", microsoft="Canada", intuit="6251999",
    ),
    Country(
        name="United Kingdom",
        aliases=("united kingdom", "uk", "u.k.", "great britain", "britain",
                 "england", "scotland"),
        cities=("london", "manchester", "edinburgh", "cambridge, uk"),
        apple="united-kingdom-GBR", google="United Kingdom", intuit="2635167",
        amazon="GBR", microsoft="United Kingdom",
    ),
    Country(name="Ireland", aliases=("ireland",), cities=("dublin", "cork"),
            apple="ireland-IRL", google="Ireland", amazon="IRL", microsoft="Ireland"),
    Country(name="Germany", aliases=("germany", "deutschland"),
            cities=("berlin", "munich", "hamburg", "frankfurt"),
            apple="germany-DEU", google="Germany", amazon="DEU", microsoft="Germany"),
    Country(name="France", aliases=("france",), cities=("paris",),
            apple="france-FRAC", google="France", amazon="FRA", microsoft="France"),
    Country(name="Netherlands", aliases=("netherlands", "the netherlands", "holland"),
            cities=("amsterdam",), apple="netherlands-NLD", google="Netherlands",
            amazon="NLD", microsoft="Netherlands"),
    Country(name="Switzerland", aliases=("switzerland",), cities=("zurich", "zürich"),
            apple="switzerland-CHEC", google="Switzerland", amazon="CHE", microsoft="Switzerland"),
    Country(name="Spain", aliases=("spain",), cities=("madrid", "barcelona"),
            apple="spain-ESPC", google="Spain", amazon="ESP", microsoft="Spain"),
    Country(name="Poland", aliases=("poland",), cities=("warsaw", "krakow"),
            apple="poland-POL", google="Poland", amazon="POL", microsoft="Poland"),
    Country(name="Israel", aliases=("israel",), cities=("tel aviv", "haifa"),
            apple="israel-ISR", google="Israel", intuit="294640",
            amazon="ISR", microsoft="Israel"),
    Country(name="Singapore", aliases=("singapore",),
            apple="singapore-SGP", google="Singapore", amazon="SGP", microsoft="Singapore"),
    Country(name="Australia", aliases=("australia",), cities=("sydney", "melbourne"),
            apple="australia-AUSC", google="Australia", amazon="AUS", microsoft="Australia"),
    Country(name="Japan", aliases=("japan",), cities=("tokyo",),
            apple="japan-JPNC", google="Japan", amazon="JPN", microsoft="Japan"),
    Country(name="China", aliases=("china",), cities=("shanghai", "beijing"),
            apple="china-CHNC", google="China", amazon="CHN", microsoft="China"),
    # Apple's own name is "Korea (Republic of)"; its search keys on the code.
    Country(name="South Korea", aliases=("south korea", "korea"), cities=("seoul",),
            apple="korea-republic-of-KOR", google="South Korea",
            amazon="KOR", microsoft="South Korea"),
    Country(name="Taiwan", aliases=("taiwan",), cities=("taipei",),
            apple="taiwan-TWN", google="Taiwan", amazon="TWN", microsoft="Taiwan"),
    Country(name="Mexico", aliases=("mexico",), cities=("mexico city",),
            apple="mexico-MEXC", google="Mexico", amazon="MEX", microsoft="Mexico"),
    Country(name="Brazil", aliases=("brazil", "brasil"), cities=("sao paulo", "são paulo"),
            apple="brazil-BRAC", google="Brazil", amazon="BRA", microsoft="Brazil"),
)

BY_NAME: dict[str, Country] = {c.name: c for c in COUNTRIES}
