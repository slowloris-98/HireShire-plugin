"""Add new Oracle Recruiting Cloud employers to `config/oracle_companies.json`, from Common Crawl.

A maintenance tool, never run by the engine; the sibling of `discover_slugs.py`
and `discover_eightfold.py`, whose index plumbing and row-merging it reuses. Every
rule in those docstrings holds here: add only, and an employer is admitted only
when its own API answers with jobs.

An employer is a **pod** — its Oracle HCM server, `<pod>.fa.<region>.oraclecloud.com`
— with one or more candidate sites, so the index query is per region
(`*.fa.us2.oraclecloud.com`) and the candidate is a `(pod, site)` pair read off the
Candidate Experience path, `/hcmUI/CandidateExperience/<lang>/sites/<site>/...`.

The one thing that makes this harder than Eightfold: **a pod answers an unknown
site number with its default site's jobs.** Measured on BDO, `CX`, `CX_xx` and
`jobsearch` — none of them real — returned the same 675 jobs as
`BDOExperiencedCareers`, while its real second site `CX_1` returned 618, 18 of
the first 200 not on the main one. So every crawled site "works", and the verify
step prunes rather than trusts: per pod, sites are taken busiest first (ties to
the one the crawl names most often, which is the real one), and a site whose
first-page requisition ids are all already covered is dropped. What is shipped is
the smallest set of sites that reaches every job the crawl could see.

The label is the site page's `<title>` (`BDO USA Experienced Career Site` ->
`BDO USA Experienced`), and the token a slug of it, because the dashboard prints
`board_token` as the company. A token that is another board's slug gets
`-oracle`, for the reason `discover_eightfold.unclash` documents.

An employer's own careers domain (`careers.americanexpress.com`) is only a front:
it answers the API with a redirect to a 404 page. `--extra` takes such URLs anyway
and reads the pod host out of the page, which names it a dozen times.

    python scripts/discover_oracle.py                    # dry run: report only
    python scripts/discover_oracle.py --write            # merge into config/
    python scripts/discover_oracle.py --crawls 1 --regions us2 us6
    python scripts/discover_oracle.py --crawls 0 --extra urls.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import html
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discover_eightfold as de  # noqa: E402
import discover_slugs as ds  # noqa: E402
from hireshire import oracle_tenants, paths  # noqa: E402
from hireshire.direct import tenants as eightfold_tenants  # noqa: E402
from hireshire.http_client import build_client  # noqa: E402
from hireshire.scrapers import oracle  # noqa: E402
from hireshire.scrapers.direct import _HANDLERS  # noqa: E402

# Regions read off the index (CC-MAIN-2026-43): these had pages, or timed out
# rather than answering zero. `sa1` and `uk1` answered zero pages and are left out.
REGIONS = ("us2", "us6", "us8", "em2", "em3", "em4", "em5", "ap1", "ap2", "ap3", "ap4",
           "ca2", "ca3", "me1", "jp1")
CHECK_LIMIT = 25
CLASH_SUFFIX = "-oracle"

POD_RE = re.compile(r"[a-z0-9]+\.fa\.[a-z0-9]+\.oraclecloud\.com")
_SITE_RE = re.compile(r"/hcmUI/CandidateExperience/[a-z]{2}(?:[-_][a-z]{2,4})?/sites/([A-Za-z0-9_]+)",
                      re.I)
_VANITY_SITE_RE = re.compile(r"/[a-z]{2}(?:[-_][a-z]{2,4})?/sites/([A-Za-z0-9_]+)", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]*?)\s*</title>", re.I | re.S)
_OG_RE = re.compile(r'property="og:site_name"\s+content="([^"]*)"', re.I)
_TITLE_NOISE = re.compile(
    # Leading invitations: `Careers at Pella`, `Jobs at Acosta`, `Check out Arqiva`,
    # `Trabaja en Interbank`, `the Myriad Genetics ...`.
    r"^\s*(?:welcome\s+to|careers?\s+(?:at|@)|jobs?\s+at|check\s+out|work\s+(?:at|for|with)"
    # A lowercase `the` only: `The Fedcap Group` is the employer's own name.
    r"|trabaja\s+en|(?-i:the))\s+"
    # Trailing template words, every one read off a real crawl's titles.
    r"|\s*[-|:]?\s*(?:external\s+)?(?:careers?|jobs?)(?:\s+(?:site|page|portal|search|section))?\s*$"
    r"|\s*[-|:]?\s*(?:candidate|career)\s+(?:experience|site|section)(?:\s+site)?\s*$"
    r"|\s*[-|:]?\s*(?:job|career)\s+opportunities\s*$"
    r"|\s*[-|:]?\s*(?:minimal|new\s+applicants|new)\s*$"
    r"|\s+(?:and|&)\s*$",
    re.I)
# What a site title says when nobody set one, read off a real crawl: Oracle's own
# product name, its template's page names, and an error page served with a 200.
# Never an employer.
_GENERIC_TITLES = frozenset({"candidate experience", "candidate experience site", "home page",
                             "home", "careers", "career", "career site", "jobs", "job search",
                             "welcome", "page not found", "generic", "global", "work with us"})
# Site numbers that name nothing: Oracle's generated ones and the common defaults.
_GENERIC_SITE = re.compile(r"cx(?:_\w+)?|jobsearch|careers?|jobs?|external|internal|corp", re.I)
_SITE_SUFFIX = re.compile(r"(?:career|careers|jobs?)(?:site)?$", re.I)


def pairs_from_cdx(lines: list[str]) -> dict[str, collections.Counter]:
    """pod host -> how often the crawl named each of its sites."""
    found: dict[str, collections.Counter] = {}
    for line in lines:
        try:
            url = json.loads(line)["url"]
        except (ValueError, KeyError, TypeError):
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not POD_RE.fullmatch(host):
            continue
        if (m := _SITE_RE.search(parts.path)):
            found.setdefault(host, collections.Counter())[m.group(1)] += 1
    return found


def clean_label(title: str) -> str | None:
    """An employer name from a site page title. Only for humans: the dashboard's
    company column and log lines. `BDO USA Experienced Career Site` -> `BDO USA
    Experienced`; `Careers at Pella` -> `Pella`."""
    # Each `|` segment on its own, so `Work with us | Copa Airlines - NEW` drops the
    # template half instead of stripping words across the boundary.
    segments = [_clean_segment(s) for s in html.unescape(title or "").split("|")]
    kept = [s for s in segments if s]
    return " | ".join(kept) or None


def _clean_segment(text: str) -> str | None:
    text = text.strip()
    previous = None
    while text and text != previous and text.lower() not in _GENERIC_TITLES:
        previous, text = text, _TITLE_NOISE.sub("", text).strip(" -:")
    return text if text and text.lower() not in _GENERIC_TITLES else None


def read_site_page(text: str) -> str | None:
    if (m := _OG_RE.search(text)) and (label := clean_label(m.group(1))):
        return label
    if (m := _TITLE_RE.search(text)):
        return clean_label(m.group(1))
    return None


def label_from_site(site: str) -> str | None:
    """A branded site number names its employer (`AperamCareerSite` -> `Aperam`);
    Oracle's generated ones (`CX_1001`) and the defaults name nothing."""
    if _GENERIC_SITE.fullmatch(site):
        return None
    return _SITE_SUFFIX.sub("", site).strip("_- ") or None


def slugify(label: str) -> str:
    """ASCII, lowercase, hyphenated: `Carrière` -> `carriere`, `Woman’s` -> `womans`."""
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    ascii_label = re.sub(r"['’]", "", ascii_label)
    return re.sub(r"[^a-z0-9]+", "-", ascii_label.lower()).strip("-")


def prune_sites(checked: dict[str, tuple[int, list[str]]],
                crawl_counts: collections.Counter) -> list[str]:
    """The smallest busiest-first set of sites whose first pages cover every id
    seen. `checked` maps site -> (TotalJobsCount, first-page ids)."""
    order = sorted((s for s, (n, _) in checked.items() if n > 0),
                   key=lambda s: (-checked[s][0], -crawl_counts.get(s, 0), s))
    kept: list[str] = []
    covered: set[str] = set()
    for site in order:
        ids = set(checked[site][1])
        if kept and ids <= covered:
            continue
        kept.append(site)
        covered |= ids
    return kept


# --- The live check -------------------------------------------------------------


async def check_site(client: httpx.AsyncClient, host: str, site: str, retry
                     ) -> tuple[str, int, list[str]]:
    """(`ok` | `not_found` | `unverified`, TotalJobsCount, first-page ids)."""
    try:
        response = await de._get_url(client, oracle.list_url(host, site, 0, CHECK_LIMIT), retry)
    except httpx.HTTPError:
        return "unverified", 0, []
    if response.status_code == 404:
        return "not_found", 0, []
    if response.status_code != 200:
        return "unverified", 0, []
    try:
        item = response.json()["items"][0]
        reqs = item.get("requisitionList") or []
        return "ok", int(item.get("TotalJobsCount") or len(reqs)), [str(r["Id"]) for r in reqs]
    except (ValueError, KeyError, TypeError, IndexError):
        return "unverified", 0, []


async def check_pod(client: httpx.AsyncClient, host: str, sites: collections.Counter, retry
                    ) -> tuple[str, dict | None, int]:
    """(`admitted` | `empty` | `not_found` | `unverified`, the row, open jobs)."""
    checked: dict[str, tuple[int, list[str]]] = {}
    kinds: set[str] = set()
    for site in sorted(sites, key=lambda s: (-sites[s], s)):
        kind, n, ids = await check_site(client, host, site, retry)
        kinds.add(kind)
        if kind == "ok":
            checked[site] = (n, ids)
    kept = prune_sites(checked, sites)
    if not kept:
        if "ok" in kinds:
            return "empty", None, 0
        return ("not_found" if kinds == {"not_found"} else "unverified"), None, 0

    # Each kept site's page in turn: a busy run timed out on a page that answers
    # moments later (Texas Woman's University, measured), and one failed fetch would
    # otherwise ship a pod id as the employer's name.
    label = None
    for site in kept:
        try:
            page = await ds._get(client, f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}",
                                 {}, retry)
        except httpx.HTTPError:
            continue
        if page.status_code == 200 and (label := read_site_page(page.text)):
            break
    label = label or next((n for s in kept if (n := label_from_site(s))), None)
    pod = host.split(".")[0]
    row = {"token": slugify(label) if label else pod, "label": label or pod, "host": host,
           "sites": kept}
    # A pod id is a label of last resort; `run` lists these for a human to name.
    return "admitted", row, max(checked[s][0] for s in kept)


def unlabelled(row: dict) -> bool:
    return row["label"] == row["host"].split(".")[0]


async def vanity_to_pod(client: httpx.AsyncClient, url: str, retry) -> tuple[str, str] | None:
    """(pod host, site) for an employer's own careers URL, read from its page."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    m = _VANITY_SITE_RE.search(parts.path)
    if not m:
        return None
    try:
        page = await ds._get(client, f"{parts.scheme}://{parts.netloc}{parts.path}", {}, retry)
    except httpx.HTTPError:
        return None
    pods = collections.Counter(POD_RE.findall(page.text.lower()))
    return (pods.most_common(1)[0][0], m.group(1)) if pods else None


async def check_all(client, pods: dict[str, collections.Counter], retry, concurrency: int):
    gate = asyncio.Semaphore(concurrency)

    async def one(host: str):
        async with gate:
            return host, await check_pod(client, host, pods[host], retry)

    return dict(await asyncio.gather(*(one(h) for h in sorted(pods))))


# --- The shipped list -----------------------------------------------------------


def merge(path: Path, admitted: list[dict]) -> int:
    """Add each admitted row whose host the file lacks, keeping its line endings
    and trailing newline. Never removes or edits a row. Returns how many were added."""
    raw = path.read_bytes().decode("utf-8") if path.exists() else "[]\n"
    existing = json.loads(raw)
    known = {str(r.get("host", "")).lower() for r in existing}
    fresh = [r for r in admitted if r["host"] not in known]
    if not fresh:
        return 0
    merged = sorted([*existing, *fresh], key=lambda r: str(r.get("token", "")).lower())
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = json.dumps(merged, indent=2).replace("\n", newline)
    if raw.endswith(("\n", "\r\n")):
        text += newline
    path.write_bytes(text.encode("utf-8"))
    return len(fresh)


def assign_tokens(rows: list[dict], taken: set[str], slugs: set[str]) -> None:
    """Make every token unique and clear of other boards' slugs, in place. A clash
    with another board gets `-oracle`; two pods with one label get the pod id."""
    for row in rows:
        token = row["token"]
        if token in slugs:
            token += CLASH_SUFFIX
        if token in taken:
            token = f"{token}-{row['host'].split('.')[0]}"
        row["token"] = token
        taken.add(token)


# --- CLI ------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cdx_retry = ds._retrying(args.cdx_attempts)
    check_retry = ds._retrying(3)
    failed: list[str] = []

    pods: dict[str, collections.Counter] = {}
    if args.crawls:
        async with build_client(180) as client:
            crawls = await ds.newest_crawls(client, args.crawls, cdx_retry)
            print(f"Crawls: {', '.join(crawls)}; regions: {', '.join(args.regions)}")
            for crawl in crawls:
                for region in args.regions:
                    lines = await ds.cdx_lines(client, crawl, f"*.fa.{region}.oraclecloud.com",
                                               cache, cdx_retry, failed, pause_s=args.pause)
                    for host, counter in pairs_from_cdx(lines).items():
                        pods.setdefault(host, collections.Counter()).update(counter)

    async with build_client(30) as client:
        client.headers.update(oracle._HEADERS)
        for file in args.extra:
            for url in json.loads(Path(file).read_text(encoding="utf-8")):
                url = str(url)
                host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
                if POD_RE.fullmatch(host):
                    pairs = pairs_from_cdx([json.dumps({"url": url})])
                    hit = next(((h, s) for h, c in pairs.items() for s in c), None)
                else:
                    hit = await vanity_to_pod(client, url, check_retry)
                if hit is None:
                    print(f"  --extra: no pod and site found for {url}")
                    continue
                pods.setdefault(hit[0], collections.Counter())[hit[1]] += 1

        path = paths.SHIPPED_CONFIG / oracle_tenants.FILENAME
        shipped = oracle_tenants.entries(path)
        shipped_hosts = {e["host"] for e in shipped}
        new = {h: c for h, c in pods.items() if h not in shipped_hosts}
        results = await check_all(client, new, check_retry, args.concurrency)

    by_kind: dict[str, list[tuple[str, dict | None, int]]] = {}
    for host, (kind, row, n) in results.items():
        by_kind.setdefault(kind, []).append((host, row, n))
    admitted = sorted(((row, n) for _, row, n in by_kind.get("admitted", []) if row),
                      key=lambda x: (-x[1], x[0]["token"]))
    taken = ({e["token"] for e in shipped} | set(eightfold_tenants.tokens()) | set(_HANDLERS))
    assign_tokens([r for r, _ in admitted], taken, de.other_board_slugs())

    print(f"\noracle: {len(pods)} candidate pods, {len(pods) - len(new)} already shipped, "
          f"{len(new)} new")
    for kind in ("admitted", "empty", "not_found", "unverified"):
        print(f"  {kind:<11} {len(by_kind.get(kind, []))}")
    for kind in ("not_found", "unverified"):
        if by_kind.get(kind):
            print(f"  {kind}: {', '.join(sorted(h for h, _, _ in by_kind[kind]))}")
    if admitted:
        print(f"  open jobs on admitted employers: {sum(n for _, n in admitted)}")
        for row, n in admitted:
            print(f"    {row['token']:<34} {n:>6}  {row['label']}  "
                  f"({row['host']}: {', '.join(row['sites'])})")
        if (nameless := [r["host"] for r, _ in admitted if unlabelled(r)]):
            print(f"  unlabelled  {len(nameless)} (no title or branded site; named by pod id, "
                  f"edit the label by hand): {', '.join(nameless)}")
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
        print("\nDry run: nothing written. Pass --write to merge the admitted employers.")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--crawls", type=int, default=3,
                        help="newest N Common Crawl crawls to read; 0 checks --extra only")
    parser.add_argument("--regions", nargs="+", default=list(REGIONS),
                        help="Oracle Cloud regions to read from the index")
    parser.add_argument("--extra", action="append", default=[], metavar="PATH",
                        help="a JSON list of pod or careers URLs to check as well; repeatable")
    parser.add_argument("--write", action="store_true",
                        help=f"merge admitted employers into config/{oracle_tenants.FILENAME}")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--pause", type=float, default=5.0,
                        help="seconds between index requests; the index blocks hard users")
    parser.add_argument("--cdx-attempts", type=int, default=6)
    parser.add_argument("--cache-dir", default=str(ds.DEFAULT_CACHE))
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
