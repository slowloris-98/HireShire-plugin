"""Add new Greenhouse, Lever and Ashby slugs to the shipped lists, from Common Crawl.

A maintenance tool, never run by the engine. The `config/*_companies.json` lists
only grow when someone runs this before a release; users receive the result with
the plugin update, since those lists live in ROOT.

It does not guess slugs. Common Crawl's URL index (index.commoncrawl.org) holds
every URL its crawler fetched, sorted host-first, so a prefix query such as
`jobs.ashbyhq.com/*` returns every Ashby board page someone on the web linked to.
The first path segment is the slug. Coverage is therefore whatever the crawler
reached: good for Ashby and Greenhouse, nearly nothing for Lever, whose host the
crawler barely visits. `--extra` checks slugs from any other list (the aggregator's,
say) through the same gate, and is Lever's main source.

Two rules, both forced by there being no skip list (see `scraper.py`): every
shipped slug is requested on every sweep by every user, and a 404 is a deferral,
never a verdict.

- **Add only.** Nothing here removes a slug. Removal would be the 404-as-verdict
  the skip list was taken out for.
- **A slug is admitted only when its board answers with at least one posting**,
  on the same endpoint the scraper uses. A junk token (`robots.txt`, a GUID, a
  one-letter path) would otherwise become a permanent request per sweep. A board
  that fails for any other reason is reported as unverified and tried again on
  the next run.

The index server is unreliable in two ways. It often answers 502/504, and it can
cut a page off mid-response while still answering 200. So pages are small (one
index block), each is retried patiently, and a page is cached only once it ends
on a whole record: a rerun asks only for the pages that failed. A crawl never
changes, so the cache never goes stale.

    python scripts/discover_slugs.py                      # dry run: report only
    python scripts/discover_slugs.py --write              # merge into config/
    python scripts/discover_slugs.py --boards ashby --crawls 1
    python scripts/discover_slugs.py --extra lever=../job-board-aggregator/data/lever_companies.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire import paths  # noqa: E402
from hireshire.http_client import build_client, make_retry_decorator  # noqa: E402
from hireshire.scrapers import ashby, greenhouse, lever  # noqa: E402

INDEX = "https://index.commoncrawl.org"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "analysis" / "cache" / "commoncrawl"

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Path segments on the board hosts that are site furniture, not boards.
_RESERVED = frozenset({
    "api", "assets", "embed", "favicon.ico", "robots.txt", "static", "v0", "v1",
})

# Statuses worth another attempt. The index server's 504 is the common one.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Board:
    name: str
    filename: str
    # Hosts the scraper can read. The EU hosts are left out on purpose: each
    # scraper's BASE_URL is the US API, so an EU board would never answer it.
    hosts: tuple[str, ...]
    check_url: Callable[[str], str]


BOARDS: dict[str, Board] = {
    "greenhouse": Board(
        "greenhouse", "greenhouse_companies.json",
        ("boards.greenhouse.io", "job-boards.greenhouse.io"),
        lambda s: f"{greenhouse.BASE_URL}/{s}/jobs",
    ),
    "lever": Board(
        "lever", "lever_companies.json",
        ("jobs.lever.co",),
        lambda s: f"{lever.BASE_URL}/{s}?mode=json&limit=1",
    ),
    "ashby": Board(
        "ashby", "ashby_companies.json",
        ("jobs.ashbyhq.com",),
        lambda s: f"{ashby.BASE_URL}/{s}",
    ),
}


def normalise(raw: str) -> str | None:
    """A slug as the shipped lists spell it, or None when it cannot be one."""
    slug = unquote(raw).strip().lower()
    if slug in _RESERVED or slug.startswith("sitemap"):
        return None
    return slug if SLUG_RE.fullmatch(slug) else None


def slug_from_url(board: Board, url: str) -> str | None:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    if (parts.hostname or "").lower() not in board.hosts:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    if segments[0].lower() == "embed":
        # Greenhouse's embeds name the board in `for=`. `token=` is NOT a board:
        # on `embed/job_app` it is the job id.
        value = parse_qs(parts.query).get("for", [""])[0]
        return normalise(value) if value else None
    return normalise(segments[0])


def slugs_from_cdx(board: Board, lines: list[str]) -> set[str]:
    found: set[str] = set()
    for line in lines:
        try:
            url = json.loads(line).get("url")
        except (json.JSONDecodeError, AttributeError):
            continue
        if url and (slug := slug_from_url(board, url)):
            found.add(slug)
    return found


# --- Common Crawl ---------------------------------------------------------------


def _retrying(attempts: int):
    return make_retry_decorator(attempts)


async def _get(client: httpx.AsyncClient, url: str, params: dict, retry) -> httpx.Response:
    @retry
    async def once() -> httpx.Response:
        response = await client.get(url, params=params)
        if response.status_code in _RETRY_STATUSES:
            response.raise_for_status()
        return response

    return await once()


async def newest_crawls(client: httpx.AsyncClient, count: int, retry) -> list[str]:
    response = await _get(client, f"{INDEX}/collinfo.json", {}, retry)
    response.raise_for_status()
    return [c["id"] for c in response.json()[:count]]


def _cache_name(crawl: str, pattern: str, page_size: int, suffix: str) -> str:
    return f"{crawl}__{re.sub(r'[^a-z0-9.]+', '_', pattern.lower())}__ps{page_size}__{suffix}"


def page_is_complete(text: str) -> bool:
    """The index server can stop mid-page and still answer 200: measured, a page
    of 8,105 lines came back as 2,232, cut off inside a URL. Nothing in the status
    says so, and caching that page would silently shrink every later run. A whole
    page ends on a newline, and its last line is a whole record."""
    if not text.endswith("\n"):
        return False
    last = text.rstrip("\n").rsplit("\n", 1)[-1]
    try:
        return isinstance(json.loads(last), dict)
    except json.JSONDecodeError:
        return False


async def cdx_lines(
    client: httpx.AsyncClient, crawl: str, pattern: str, cache: Path, retry,
    failed: list[str], pause_s: float = 1.0, page_size: int = 1, attempts: int = 3,
) -> list[str]:
    """Every index line for one pattern in one crawl. A page that still fails
    after its retries is named in `failed` and skipped, never fatal.

    `page_size` is in index blocks of ~3,000 lines. The server's default of 5
    makes a page slow enough to be cut off; one block answers in a few seconds."""
    endpoint = f"{INDEX}/{crawl}-index"
    count_file = cache / _cache_name(crawl, pattern, page_size, "pages.json")
    if count_file.exists():
        pages = json.loads(count_file.read_text(encoding="utf-8"))["pages"]
    else:
        try:
            response = await _get(
                client, endpoint,
                {"url": pattern, "showNumPages": "true", "pageSize": str(page_size)}, retry,
            )
        except httpx.HTTPError as exc:
            failed.append(f"{crawl} {pattern} (page count: {exc})")
            return []
        await asyncio.sleep(pause_s)
        if response.status_code == 404:  # no captures for this pattern at all
            pages = 0
        elif response.status_code != 200:
            failed.append(f"{crawl} {pattern} (page count: HTTP {response.status_code})")
            return []
        else:
            pages = response.json()["pages"]
        count_file.write_text(json.dumps({"pages": pages}), encoding="utf-8")

    lines: list[str] = []
    for page in range(pages):
        page_file = cache / _cache_name(crawl, pattern, page_size, f"{page}.jsonl")
        if page_file.exists():
            lines += page_file.read_text(encoding="utf-8").splitlines()
            continue
        params = {"url": pattern, "output": "json", "fl": "url",
                  "pageSize": str(page_size), "page": str(page)}
        text, problem = None, ""
        for _ in range(attempts):
            try:
                response = await _get(client, endpoint, params, retry)
            except httpx.HTTPError as exc:
                problem = str(exc).splitlines()[0]
                break
            finally:
                await asyncio.sleep(pause_s)
            if response.status_code != 200:
                problem = f"HTTP {response.status_code}"
                break
            if page_is_complete(response.text):
                text = response.text
                break
            problem = "page cut off mid-response"
        if text is None:
            failed.append(f"{crawl} {pattern} page {page} ({problem})")
            continue
        # Written only once whole, so a rerun asks again for what failed.
        page_file.write_text(text, encoding="utf-8")
        lines += text.splitlines()
    return lines


# --- The live check -------------------------------------------------------------


async def check(client: httpx.AsyncClient, board: Board, slug: str, retry) -> tuple[str, int]:
    """(`admitted` | `empty` | `not_found` | `unverified`, open postings seen)."""
    try:
        response = await _get(client, board.check_url(slug), {}, retry)
    except httpx.HTTPError:
        return "unverified", 0
    if response.status_code == 404:
        return "not_found", 0
    if response.status_code != 200:
        return "unverified", 0
    try:
        data = response.json()
    except ValueError:
        return "unverified", 0
    postings = data if isinstance(data, list) else (data.get("jobs") or [])
    return ("admitted", len(postings)) if postings else ("empty", 0)


async def check_all(
    client: httpx.AsyncClient, board: Board, slugs: list[str], retry, concurrency: int,
) -> dict[str, tuple[str, int]]:
    gate = asyncio.Semaphore(concurrency)

    async def one(slug: str) -> tuple[str, tuple[str, int]]:
        async with gate:
            return slug, await check(client, board, slug, retry)

    return dict(await asyncio.gather(*(one(s) for s in slugs)))


# --- The shipped lists ----------------------------------------------------------


def read_list(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))


def merge(path: Path, admitted: set[str]) -> int:
    """Union `admitted` into the list at `path`, keeping its line endings and
    trailing newline. Never removes a slug. Returns how many were added."""
    raw = path.read_bytes().decode("utf-8")
    existing = json.loads(raw)
    merged = sorted(set(existing) | admitted)
    added = len(merged) - len(set(existing))
    if not added:
        return 0
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = json.dumps(merged, indent=2).replace("\n", newline)
    if raw.endswith(("\n", "\r\n")):
        text += newline
    path.write_bytes(text.encode("utf-8"))
    return added


# --- CLI ------------------------------------------------------------------------


def _parse_extra(values: list[str]) -> dict[str, list[Path]]:
    extra: dict[str, list[Path]] = {}
    for value in values:
        name, sep, file = value.partition("=")
        if not sep or name not in BOARDS:
            raise SystemExit(f"--extra takes BOARD=PATH with BOARD one of {', '.join(BOARDS)}: {value}")
        extra.setdefault(name, []).append(Path(file))
    return extra


async def run(args: argparse.Namespace) -> int:
    boards = [BOARDS[b] for b in args.boards]
    extra = _parse_extra(args.extra)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cdx_retry = _retrying(args.cdx_attempts)
    check_retry = _retrying(3)
    failed: list[str] = []

    candidates: dict[str, set[str]] = {b.name: set() for b in boards}
    if args.crawls:
        async with build_client(180) as client:
            crawls = await newest_crawls(client, args.crawls, cdx_retry)
            print(f"Crawls: {', '.join(crawls)}")
            for board in boards:
                for crawl in crawls:
                    for host in board.hosts:
                        lines = await cdx_lines(client, crawl, f"{host}/*", cache, cdx_retry, failed)
                        candidates[board.name] |= slugs_from_cdx(board, lines)
    for board in boards:
        for file in extra.get(board.name, []):
            candidates[board.name] |= {s for raw in read_list(file) if (s := normalise(raw))}

    status = 0
    async with build_client(30) as client:
        for board in boards:
            path = paths.SHIPPED_CONFIG / board.filename
            shipped = set(read_list(path))
            new = sorted(candidates[board.name] - shipped)
            results = await check_all(client, board, new, check_retry, args.concurrency)
            by_kind: dict[str, list[tuple[str, int]]] = {}
            for slug, (kind, n) in results.items():
                by_kind.setdefault(kind, []).append((slug, n))
            admitted = sorted(by_kind.get("admitted", []), key=lambda x: (-x[1], x[0]))

            print(f"\n{board.name}: {len(candidates[board.name])} candidates, "
                  f"{len(candidates[board.name] & shipped)} already shipped, {len(new)} new")
            for kind in ("admitted", "empty", "not_found", "unverified"):
                print(f"  {kind:<11} {len(by_kind.get(kind, []))}")
            if admitted:
                print(f"  open postings on admitted boards: {sum(n for _, n in admitted)}")
                print("  " + ", ".join(f"{s} ({n})" for s, n in admitted))
            if args.write and admitted:
                added = merge(path, {s for s, _ in admitted})
                print(f"  wrote {added} to {path.name}")

    if failed:
        status = 1
        print(f"\n{len(failed)} index request(s) failed; rerun to fetch just those:")
        for line in failed:
            print(f"  {line}")
    if not args.write:
        print("\nDry run: nothing written. Pass --write to merge the admitted slugs.")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--boards", nargs="+", choices=list(BOARDS), default=list(BOARDS))
    parser.add_argument("--crawls", type=int, default=3,
                        help="newest N Common Crawl crawls to read; 0 checks --extra only")
    parser.add_argument("--extra", action="append", default=[], metavar="BOARD=PATH",
                        help="a JSON list of slugs to check as well; repeatable")
    parser.add_argument("--write", action="store_true", help="merge admitted slugs into config/")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--cdx-attempts", type=int, default=6)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
