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

US_STATE_ABBREVS = (
    "AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI",
    "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA",
    "WI", "WV", "WY",
)

INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "new delhi", "delhi",
    "noida", "gurgaon", "gurugram", "pune", "chennai", "kolkata", "ahmedabad",
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
