"""Add new Eightfold tenants to `config/eightfold_tenants.json`, from Common Crawl.

A maintenance tool, never run by the engine; the sibling of `discover_slugs.py`,
whose index plumbing (paging, retries, the cut-off-page check, the cache) it reuses
rather than copies. Read that docstring first: every rule there holds here.

Eightfold differs from Greenhouse or Ashby in one way that decides the shape of
this tool: a tenant is a **host**, not a path. `bostonscientific.eightfold.ai`
is Boston Scientific's board, and every request also needs a `domain=` value
(`bostonscientific.com`). So the index query is the domain `*.eightfold.ai`, the
candidate is the subdomain, and each admitted tenant is a row, not a slug:

    {"token": ..., "label": ..., "base": "https://<host>", "domain": ...}

Where `domain` comes from, in order:

1. **The crawled URLs themselves.** Eightfold's own links carry it as a query
   parameter (`/careers/accounting_services/...?domain=bnymellon.com`), so most
   hosts resolve with no request at all. The most common value wins.
2. **The careers page.** Its embedded config holds `"domain": "<value>"`,
   HTML-escaped. One GET, which also gives the label (`<title>Careers at ...`).

A host with neither is `unverified`. The domain is never guessed as
`<subdomain>.com`: a wrong one answers zero positions, which reads as an empty
board rather than a wrong guess.

Admission is the same gate as everywhere: the tenant's own `/api/pcsx/search`
(the scraper's request, built by `handlers/eightfold.py`) must answer with at
least one position. Add only — nothing here removes a tenant.

Coverage is `*.eightfold.ai` only. Tenants on their own domains
(`careers.qualcomm.com`) are not findable by host, and several tenants have
moved there: measured, BNY and American Express now answer 404 on their
`eightfold.ai` hosts. `--extra` checks a JSON list of hosts or URLs from any
source through the same gate, vanity hosts included, and is their road in.

The index is shared and blocks an IP that queries it hard (Common Crawl's FAQ:
wait 24 hours). The Eightfold domain is about four index pages a crawl, so this
asks slowly (`--pause`, default 5 s) rather than retrying hard.

    python scripts/discover_eightfold.py                 # dry run: report only
    python scripts/discover_eightfold.py --write         # merge into config/
    python scripts/discover_eightfold.py --crawls 1
    python scripts/discover_eightfold.py --crawls 0 --extra hosts.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discover_slugs as ds  # noqa: E402
from hireshire import paths  # noqa: E402
from hireshire.direct import tenants  # noqa: E402
from hireshire.http_client import build_client  # noqa: E402
from hireshire.scrapers.direct import _HEADERS, _HANDLERS  # noqa: E402
from hireshire.scrapers.handlers import eightfold  # noqa: E402

PATTERN = "*.eightfold.ai"
SUFFIX = ".eightfold.ai"
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}")
_CONFIG_DOMAIN_RE = re.compile(r'"domain"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]*?)\s*</title>", re.I | re.S)

# Eightfold's own sites, read off a real crawl: the product, its docs, its
# community forum, its conference (`cultivate`, `cultivateeurope`), its marketing
# pages, and `employee`, a board under Eightfold's placeholder domain.
_EIGHTFOLD_OWN = frozenset({
    "api", "apidocs", "app", "community", "cultivate", "cultivateeurope", "docs",
    "employee", "help", "pages", "partners", "status", "support", "www",
})
# The domain a tenant's page reports when it is not configured on that host:
# measured on kroger.eightfold.ai and employee.eightfold.ai. Volkscience is
# Eightfold's legal name. Never a customer's domain, so never admitted.
_PLACEHOLDER_DOMAINS = frozenset({"volkscience.com", "eightfold.ai"})
# Customers' non-production boards (`aexp-sandbox`, `citigroup-qa-sandbox`). Like
# Gem's rule, a word must START at the match, so a real tenant named `atestco`
# or `devon` is not caught by `test` or `dev`.
_NONPROD = re.compile(r"sandbox|demo|(?:^|-)(?:test|qa|uat|stage|staging|dev|preprod)(?:-|\d|$)")


# The slug boards a tenant token must not collide with. The applier excludes by
# company token (`worker.py`), and every Eightfold tenant is excluded, so a tenant
# named `deloitte` would also switch auto-apply off for Lever's `deloitte` board.
# Workday is absent on purpose: its tokens are compound (`nvidia|wd5|...`).
_SLUG_LISTS = ("greenhouse_companies.json", "lever_companies.json", "ashby_companies.json",
               "gem_companies.json", "bamboohr_companies.json")
CLASH_SUFFIX = "-eightfold"


def other_board_slugs() -> set[str]:
    slugs: set[str] = set()
    for name in _SLUG_LISTS:
        slugs |= {s.lower() for s in ds.read_list(paths.SHIPPED_CONFIG / name)}
    return slugs


def unclash(token: str, slugs: set[str]) -> str:
    return f"{token}{CLASH_SUFFIX}" if token in slugs else token


def reject(token: str) -> bool:
    return token in _EIGHTFOLD_OWN or bool(_NONPROD.search(token))


def tenant_of(url: str) -> tuple[str, str] | None:
    """(host, token) for a `<token>.eightfold.ai` URL, else None."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    if not host.endswith(SUFFIX):
        return None
    token = host[: -len(SUFFIX)]
    return (host, token) if TOKEN_RE.fullmatch(token) else None


def _usable_domain(value: str) -> str | None:
    value = value.strip().lower()
    return value if DOMAIN_RE.fullmatch(value) and value not in _PLACEHOLDER_DOMAINS else None


def _domain_param(url: str) -> str | None:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    return _usable_domain((parse_qs(parts.query).get("domain") or [""])[0])


def candidates_from_cdx(lines: list[str]) -> dict[str, collections.Counter]:
    """host -> how often each `domain=` value appeared in its crawled URLs.
    A host whose URLs never carry one is present with an empty Counter."""
    found: dict[str, collections.Counter] = {}
    for line in lines:
        try:
            url = json.loads(line)["url"]
        except (ValueError, KeyError, TypeError):
            continue
        hit = tenant_of(url)
        if hit is None:
            continue
        counter = found.setdefault(hit[0], collections.Counter())
        if (domain := _domain_param(url)):
            counter[domain] += 1
    return found


def read_careers_page(text: str) -> tuple[str | None, str | None]:
    """(domain, label) from a careers page, either possibly None."""
    page = html.unescape(text)
    domain = next((d for v in _CONFIG_DOMAIN_RE.findall(page) if (d := _usable_domain(v))), None)
    label = None
    if (m := _TITLE_RE.search(page)):
        label = _clean_label(m.group(1))
    return domain, label


def _clean_label(title: str) -> str | None:
    """A company name from a careers page title. Only log lines read it, so this
    is tidying, not parsing: `Match Me Resume Match | Citi Careers` -> `Citi`."""
    segments = [s.strip() for s in title.split("|") if s.strip()]
    if not segments:
        return None
    named = [s for s in segments if re.search(r"\bcareers\b", s, re.I)]
    label = named[-1] if named else segments[0]
    label = re.sub(r"^\s*careers\s+(?:at|@)\s+|\s+careers\s*$", "", label, flags=re.I).strip()
    return label or None


def _host_of(raw: str) -> str | None:
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parts.hostname or "").lower()
    return host or None


def token_for(host: str, domain: str | None) -> str | None:
    """The tenant's token: the subdomain on `eightfold.ai`, else the domain's
    first label (`careers.americanexpress.com` + `aexp.com` -> `aexp`)."""
    if (hit := tenant_of(host)):
        return hit[1]
    if domain:
        token = domain.split(".")[0]
        return token if TOKEN_RE.fullmatch(token) else None
    return None


# --- The live check -------------------------------------------------------------


async def _get_url(client: httpx.AsyncClient, url: str, retry) -> httpx.Response:
    """`discover_slugs._get` for a URL that carries its own query string. Passing
    that URL with `params={}` makes httpx drop the query, so the search would go
    out with no `domain=` at all; the query is handed over as the params instead."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    return await ds._get(client, parts._replace(query="").geturl(), params, retry)


async def check(client: httpx.AsyncClient, host: str, domains: collections.Counter,
                retry) -> tuple[str, dict | None, int]:
    """(`admitted` | `empty` | `not_found` | `unverified`, the row, open postings)."""
    base = f"https://{host}"
    domain = domains.most_common(1)[0][0] if domains else None
    label = None
    # The page is fetched even when the crawl named the domain: it is where the
    # label comes from. A failure here costs only the label (BNY's answers 404
    # while its crawled URLs still carry the domain).
    try:
        page = await ds._get(client, f"{base}/careers", {}, retry)
        if page.status_code == 200:
            page_domain, label = read_careers_page(page.text)
            domain = domain or page_domain
            # A tenant that moved answers its eightfold.ai host with a redirect to
            # its own domain (netflix -> explore.jobs.netflix.net); the scraper
            # should ask there directly.
            if page.url.host and page.url.host.lower() != host:
                base = f"https://{page.url.host.lower()}"
    except httpx.HTTPError:
        pass
    token = token_for(host, domain)
    if domain is None or token is None:
        return "unverified", None, 0

    tenant = eightfold.Tenant(token=token, label=label or token, base=base, domain=domain)
    try:
        response = await _get_url(client, eightfold.list_url(tenant, None, 1), retry)
    except httpx.HTTPError:
        return "unverified", None, 0
    if response.status_code == 404:
        return "not_found", None, 0
    if response.status_code != 200:
        return "unverified", None, 0
    try:
        data = response.json()["data"]
        positions = data["positions"]
    except (ValueError, KeyError, TypeError):
        return "unverified", None, 0
    if not positions:
        return "empty", None, 0
    row = {"token": token, "label": tenant.label, "base": base, "domain": domain}
    return "admitted", row, int(data.get("count") or len(positions))


async def check_all(client, hosts: dict[str, collections.Counter], retry,
                    concurrency: int) -> dict[str, tuple[str, dict | None, int]]:
    gate = asyncio.Semaphore(concurrency)

    async def one(host: str):
        async with gate:
            return host, await check(client, host, hosts[host], retry)

    return dict(await asyncio.gather(*(one(h) for h in sorted(hosts))))


# --- The shipped list -----------------------------------------------------------


def merge(path: Path, admitted: list[dict]) -> int:
    """Add each admitted row whose token the file lacks, keeping its line endings
    and trailing newline. Never removes or edits a row. Returns how many were added."""
    raw = path.read_bytes().decode("utf-8") if path.exists() else "[]\n"
    existing = json.loads(raw)
    known = {str(r.get("token", "")).lower() for r in existing}
    fresh = [r for r in admitted if r["token"] not in known]
    if not fresh:
        return 0
    merged = sorted([*existing, *fresh], key=lambda r: str(r.get("token", "")).lower())
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = json.dumps(merged, indent=2).replace("\n", newline)
    if raw.endswith(("\n", "\r\n")):
        text += newline
    path.write_bytes(text.encode("utf-8"))
    return len(fresh)


# --- CLI ------------------------------------------------------------------------


def _read_extra(files: list[str]) -> dict[str, collections.Counter]:
    hosts: dict[str, collections.Counter] = {}
    for file in files:
        for raw in json.loads(Path(file).read_text(encoding="utf-8")):
            if (host := _host_of(str(raw))):
                counter = hosts.setdefault(host, collections.Counter())
                if (domain := _domain_param(str(raw))):
                    counter[domain] += 1
    return hosts


async def run(args: argparse.Namespace) -> int:
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cdx_retry = ds._retrying(args.cdx_attempts)
    check_retry = ds._retrying(3)
    failed: list[str] = []

    hosts: dict[str, collections.Counter] = {}
    if args.crawls:
        async with build_client(180) as client:
            crawls = await ds.newest_crawls(client, args.crawls, cdx_retry)
            print(f"Crawls: {', '.join(crawls)}")
            for crawl in crawls:
                lines = await ds.cdx_lines(client, crawl, PATTERN, cache, cdx_retry, failed,
                                           pause_s=args.pause)
                for host, counter in candidates_from_cdx(lines).items():
                    hosts.setdefault(host, collections.Counter()).update(counter)
    for host, counter in _read_extra(args.extra).items():
        hosts.setdefault(host, collections.Counter()).update(counter)

    path = paths.SHIPPED_CONFIG / tenants.FILENAME
    taken = set(tenants.tokens(path)) | set(_HANDLERS)
    shipped_hosts = {urlsplit(e["base"]).hostname for e in tenants.entries(path)}
    new = {h: c for h, c in hosts.items()
           if h not in shipped_hosts and (tenant_of(h) is None or tenant_of(h)[1] not in taken)}
    rejected = sorted(h for h in new if (hit := tenant_of(h)) and reject(hit[1]))
    for h in rejected:
        del new[h]

    async with build_client(30) as client:
        client.headers.update(_HEADERS)
        results = await check_all(client, new, check_retry, args.concurrency)

    by_kind: dict[str, list[tuple[str, dict | None, int]]] = {}
    for host, (kind, row, n) in results.items():
        by_kind.setdefault(kind, []).append((host, row, n))
    # A vanity host and its eightfold.ai twin can resolve to one token, and a vanity
    # host's token can clash with a shipped one; the busiest board wins a tie.
    admitted: list[tuple[dict, int]] = []
    seen = set(taken)
    slugs = other_board_slugs()
    ranked = sorted(((row, n) for _, row, n in by_kind.get("admitted", []) if row),
                    key=lambda x: (-x[1], x[0]["token"]))
    for row, n in ranked:
        row["token"] = unclash(row["token"], slugs)
        if row["token"] not in seen:
            seen.add(row["token"])
            admitted.append((row, n))

    print(f"\neightfold: {len(hosts)} candidate hosts, {len(hosts) - len(new) - len(rejected)} "
          f"already shipped, {len(new)} new")
    if rejected:
        print(f"  rejected    {len(rejected)}: {', '.join(rejected)}")
    for kind in ("admitted", "empty", "not_found", "unverified"):
        print(f"  {kind:<11} {len(by_kind.get(kind, []))}")
    for kind in ("not_found", "unverified"):
        if by_kind.get(kind):
            print(f"  {kind}: {', '.join(sorted(h for h, _, _ in by_kind[kind]))}")
    if admitted:
        print(f"  open postings on admitted tenants: {sum(n for _, n in admitted)}")
        for row, n in admitted:
            print(f"    {row['token']:<28} {n:>6}  {row['label']}  ({row['base']}, {row['domain']})")
    if args.write and admitted:
        added = merge(path, [r for r, _ in admitted])
        print(f"  wrote {added} to {path.name}")

    status = 0
    if failed:
        status = 1
        print(f"\n{len(failed)} index request(s) failed; rerun to fetch just those:")
        for line in failed:
            print(f"  {line}")
    if not args.write:
        print("\nDry run: nothing written. Pass --write to merge the admitted tenants.")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--crawls", type=int, default=3,
                        help="newest N Common Crawl crawls to read; 0 checks --extra only")
    parser.add_argument("--extra", action="append", default=[], metavar="PATH",
                        help="a JSON list of hosts or URLs to check as well; repeatable")
    parser.add_argument("--write", action="store_true",
                        help=f"merge admitted tenants into config/{tenants.FILENAME}")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--pause", type=float, default=5.0,
                        help="seconds between index requests; the index blocks hard users")
    parser.add_argument("--cdx-attempts", type=int, default=6)
    parser.add_argument("--cache-dir", default=str(ds.DEFAULT_CACHE))
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
