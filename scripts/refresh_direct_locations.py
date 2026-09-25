"""Re-derive the per-portal location codes in `hireshire/direct/portal_locations.py`.

A maintenance tool, never run by the engine. Apple and Google answer a location
value they do not recognise with **zero results and a 200**, so the table is
checked in rather than resolved at sweep time — and this script is how it gets
checked, rather than guessed:

- Apple: `jobs.apple.com/api/v1/refData/postlocation?input=<name>`, taking the
  `level: 1` (country) hit. Never feed it a user's own term: `georgia` comes back
  as the Republic of Georgia.
- Google: a country name is accepted when the search returns any job at all.
- Intuit: the country facet ids (GeoNames) listed in the search page's filters.
  A country Intuit has no jobs in is simply absent, which is correct — the
  table then leaves Intuit unscoped for it.
- Amazon: the ISO3 code already in the table is accepted when the search
  returns any hit for it. An unknown code answers 0 hits and a 200, so a code is
  never guessed; a new row needs its code added by hand, then checked here.
- Microsoft: a country name is accepted when the search counts any job for it.
  Its `location=` is fuzzy (a UK search also returns Nordic roles), which is
  harmless: `scraper.py`'s location filter drops the strays.

Meta has no column. It returns its whole board in one response.

    python scripts/refresh_direct_locations.py            # every country in the table
    python scripts/refresh_direct_locations.py Mexico     # or just the ones named

Prints one line per country, marking every cell that disagrees with the table.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire.direct.portal_locations import COUNTRIES  # noqa: E402
from hireshire.scrapers.handlers import amazon, intuit, microsoft  # noqa: E402

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def apple_slug(client: httpx.Client, name: str) -> str | None:
    res = client.get(
        "https://jobs.apple.com/api/v1/refData/postlocation", params={"input": name}
    ).json().get("res") or []
    countries = [h for h in res if h.get("level") == 1]
    exact = [h for h in countries if h.get("name", "").lower() == name.lower()]
    # Apple spells some countries its own way ("Korea (Republic of)"); a single
    # country-level hit is still unambiguous. Its search keys on the code suffix.
    hit = (exact or (countries if len(countries) == 1 else [None]))[0]
    if hit is None:
        return None
    words = re.sub(r"[^a-z0-9]+", "-", hit["urlName"].lower()).strip("-")
    return f"{words}-{hit['code']}"


def google_accepts(client: httpx.Client, name: str) -> bool:
    html = client.get(
        "https://www.google.com/about/careers/applications/jobs/results"
        f"?location={quote(name)}&sort_by=date&page=1"
    ).text
    return bool(re.search(r"jobs/results/\d+-", html))


def intuit_facets(client: httpx.Client) -> dict[str, str]:
    payload = client.get(
        intuit.list_url(None, 1), headers={"X-Requested-With": "XMLHttpRequest"}
    ).json()
    soup = BeautifulSoup(payload.get("filters") or "", "lxml")
    out = {}
    for inp in soup.select('input[data-facet-type="2"]'):
        label = inp.find_next("label")
        name = inp.get("data-display") or (label.get_text(" ", strip=True) if label else "")
        # Labels carry a count: "United States (475)".
        out[re.sub(r"\s*\(\d+\)\s*$", "", name).lower()] = inp.get("data-id")
    return out


def amazon_accepts(client: httpx.Client, code: str | None) -> bool:
    if not code:
        return False
    url = amazon.list_url(None, 1).replace("result_limit=100", "result_limit=1")
    payload = client.get(url, params={"normalized_country_code[]": code}).json()
    return bool(payload.get("hits"))


def microsoft_accepts(client: httpx.Client, name: str) -> bool:
    payload = client.get(microsoft.list_url(name, 1)).json()
    return bool((payload.get("data") or {}).get("count"))


def main(argv: list[str]) -> int:
    wanted = {a.lower() for a in argv}
    rows = [c for c in COUNTRIES if not wanted or c.name.lower() in wanted]
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        facets = intuit_facets(client)
        for c in rows:
            # Apple may know the country by an alias only ("korea", not "south korea").
            apple = next(filter(None, (apple_slug(client, q) for q in (c.name, *c.aliases))), None)
            google = c.name if google_accepts(client, c.name) else None
            intuit_id = facets.get(c.name.lower())
            amazon_code = c.amazon if amazon_accepts(client, c.amazon) else None
            ms_name = c.name if microsoft_accepts(client, c.name) else None
            cells = []
            for label, live, shipped in (("apple", apple, c.apple),
                                         ("google", google, c.google),
                                         ("intuit", intuit_id, c.intuit),
                                         ("amazon", amazon_code, c.amazon),
                                         ("microsoft", ms_name, c.microsoft)):
                mark = "" if live == shipped else "   <-- table has %r" % (shipped,)
                cells.append(f"{label}={live!r}{mark}")
            print(f"{c.name:<16} " + "  ".join(cells))
        known = {c.name.lower() for c in COUNTRIES}
        extra = sorted(n for n in facets if n not in known)
        if extra:
            print(f"\nIntuit facets with no table row: {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
