"""
Sweeps the ATS board APIs for every company in the enabled slug lists.

Slugs are loaded from config/*_companies.json in the plugin install dir. Which
boards run is set by `enabled_platforms` in scraper.yaml — a disabled board's
slug file is never even opened. Slugs that 404 are recorded as a delta under the
plugin data dir and skipped before any HTTP call on later runs.

    python scraper.py
"""

import asyncio
import json
import logging
import time

import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from hireshire import paths
from hireshire.config import load_config
from hireshire.http_client import build_client
from hireshire.scrapers.ashby import AshbyScraper
from hireshire.scrapers.bamboohr import BambooHRScraper
from hireshire.scrapers.direct import DirectScraper
from hireshire.scrapers.exceptions import BoardBlockedError, SlugNotFoundError
from hireshire.scrapers.greenhouse import GreenhouseScraper
from hireshire.scrapers.lever import LeverScraper
from hireshire.scrapers.workday import WorkdayScraper
from hireshire.storage.db import get_db
from hireshire.storage.json_store import RunStore

logger = logging.getLogger(__name__)
console = Console()

_PLATFORMS = ("ashby", "greenhouse", "lever", "bamboohr", "workday", "direct")

# Dead-slug bookkeeping is split three ways because ${CLAUDE_PLUGIN_ROOT} is
# replaced wholesale on every plugin update, so nothing written at runtime can
# live there:
#
#   SEED   (ROOT, read-only)  the curated list shipped with each release
#   USER_BAD  (DATA, written)  slugs this install discovered are dead
#   USER_RECOVERED (DATA)      slugs verify_bad_slugs.py --prune found alive again
#
# Effective set = seed ∪ user_bad − user_recovered. A release can therefore add
# newly-dead slugs without erasing local learning, and a board that came back
# online for this user stays enabled even if the shipped seed still calls it dead.
SEED_BAD_SLUGS_PATH = paths.SHIPPED_CONFIG / "bad_slugs.json"
USER_BAD_SLUGS_PATH = paths.DATA / "user_bad_slugs.json"
USER_RECOVERED_PATH = paths.DATA / "user_recovered_slugs.json"


def _read_slug_map(path: Path) -> dict[str, set[str]]:
    """Read a {platform: [slug, ...]} file, tolerating absence."""
    if not path.exists():
        return {p: set() for p in _PLATFORMS}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {p: set(raw.get(p, [])) for p in _PLATFORMS}


def _write_slug_map(path: Path, data: dict[str, set[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({p: sorted(data.get(p, ())) for p in _PLATFORMS}, indent=2),
        encoding="utf-8",
    )


def _load_bad_slugs() -> dict[str, set[str]]:
    seed = _read_slug_map(SEED_BAD_SLUGS_PATH)
    user_bad = _read_slug_map(USER_BAD_SLUGS_PATH)
    recovered = _read_slug_map(USER_RECOVERED_PATH)
    return {p: (seed[p] | user_bad[p]) - recovered[p] for p in _PLATFORMS}


def _save_bad_slugs(bad: dict[str, set[str]]) -> None:
    """Persist only what this install discovered — never the shipped seed, which
    is read-only and would be lost on the next update anyway. A slug that is
    newly bad also stops being 'recovered'."""
    seed = _read_slug_map(SEED_BAD_SLUGS_PATH)
    recovered = _read_slug_map(USER_RECOVERED_PATH)
    user_bad = {p: bad.get(p, set()) - seed[p] for p in _PLATFORMS}
    _write_slug_map(USER_BAD_SLUGS_PATH, user_bad)

    still_recovered = {p: recovered[p] - bad.get(p, set()) for p in _PLATFORMS}
    if still_recovered != recovered:
        _write_slug_map(USER_RECOVERED_PATH, still_recovered)


def _matches_location(job, terms: list[str]) -> bool:
    haystack = [job.location.name.lower()]
    haystack += [o.location.lower() for o in job.offices if o.location]
    return any(term in loc for term in terms for loc in haystack)


class _NoopProgress:
    def update(self, *a, **kw): pass
    def advance(self, *a, **kw): pass
    def add_task(self, *a, **kw): return 0
    def __enter__(self): return self
    def __exit__(self, *a): pass


async def main(
    out_queue: asyncio.Queue | None = None,
    quiet: bool = False,
    run_id: str | None = None,
    on_company_start=None,
) -> None:
    if not quiet:
        logging.basicConfig(
            level=logging.WARNING,
            handlers=[RichHandler(show_path=False, rich_tracebacks=True)],
        )

    config = load_config()
    settings = config.settings

    bad_slugs = _load_bad_slugs()
    total_skipped = sum(len(v) for v in bad_slugs.values())

    # A board absent from settings.enabled_platforms has no companies loaded at
    # all — load_config() never opened its slug file — so these comprehensions
    # collapse to empty and run_board returns immediately.
    ashby_companies = [c for c in config.ashby_companies if c.ashby_token not in bad_slugs["ashby"]]
    greenhouse_companies = [c for c in config.greenhouse_companies if c.greenhouse_token not in bad_slugs["greenhouse"]]
    lever_companies = [c for c in config.lever_companies if c.lever_token not in bad_slugs["lever"]]
    bamboohr_companies = [c for c in config.bamboohr_companies if c.bamboohr_token not in bad_slugs["bamboohr"]]
    workday_companies = [c for c in config.workday_companies if c.workday_token not in bad_slugs["workday"]]

    # Single-tenant portals: no slug can be "wrong", and DirectScraper never
    # raises SlugNotFoundError, so there is nothing to filter out here.
    direct_companies = list(config.direct_companies)

    if not (greenhouse_companies or lever_companies or ashby_companies
            or bamboohr_companies or workday_companies or direct_companies):
        if not quiet:
            console.print("[yellow]No companies to scrape. All slugs may be in the bad-slugs list.[/yellow]")
        return

    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    store = RunStore(run_id=run_id, db=get_db(settings.db_path))
    started_at = datetime.now(timezone.utc)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.max_age_hours)
        if settings.max_age_hours
        else None
    )
    location_terms = [t.lower() for t in settings.location_filter]

    if not quiet:
        console.print(f"[bold]HireShire Scraper[/bold] — run [cyan]{run_id}[/cyan]")
        if cutoff:
            console.print(
                f"Filtering jobs updated after [cyan]{cutoff.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]"
                f" (last {settings.max_age_hours}h)"
            )
        if location_terms:
            console.print(f"Filtering by location: [cyan]{', '.join(settings.location_filter)}[/cyan]")
        sources = []
        if greenhouse_companies:
            sources.append(f"[bold]{len(greenhouse_companies)}[/bold] via Greenhouse")
        if lever_companies:
            sources.append(f"[bold]{len(lever_companies)}[/bold] via Lever")
        if ashby_companies:
            sources.append(f"[bold]{len(ashby_companies)}[/bold] via Ashby")
        if bamboohr_companies:
            sources.append(f"[bold]{len(bamboohr_companies)}[/bold] via BambooHR")
        if workday_companies:
            sources.append(f"[bold]{len(workday_companies)}[/bold] via Workday")
        if direct_companies:
            sources.append(f"[bold]{len(direct_companies)}[/bold] via direct portals")
        console.print(f"Fetching from {' + '.join(sources)}", end="")
        if total_skipped:
            console.print(f"  [dim]({total_skipped} known-bad slugs skipped)[/dim]")
        else:
            console.print()
        console.print()

    lock = asyncio.Lock()
    newly_bad: dict[str, list[str]] = {p: [] for p in _PLATFORMS}

    # Counters shared across tasks (mutated under lock)
    counters = {"jobs": 0, "with_jobs": 0, "errors": 0, "not_found": 0, "blocked": 0, "done": 0}
    errors_detail: list[tuple[str, str]] = []  # (name, error message)

    try:
        async with build_client(settings.request_timeout_s) as client:
            greenhouse_scraper = GreenhouseScraper(
                client, settings.make_limiter("greenhouse"), settings.retry_attempts,
                detail_concurrency=settings.detail_concurrency, detail_jitter_s=settings.detail_jitter_s,
                fetch_questions=settings.greenhouse_fetch_questions,
            )
            lever_scraper = LeverScraper(client, settings.make_limiter("lever"), settings.retry_attempts, cutoff=cutoff)
            ashby_scraper = AshbyScraper(client, settings.make_limiter("ashby"), settings.retry_attempts)
            bamboohr_scraper = BambooHRScraper(
                client, settings.make_limiter("bamboohr"), settings.retry_attempts,
                detail_concurrency=settings.detail_concurrency, detail_jitter_s=settings.detail_jitter_s,
                fetch_detail=settings.scrape_details,
            )
            workday_scraper = WorkdayScraper(
                client, settings.make_limiter("workday"), settings.retry_attempts, cutoff=cutoff,
                detail_concurrency=settings.detail_concurrency, detail_jitter_s=settings.detail_jitter_s,
                fetch_detail=settings.scrape_details,
            )
            direct_scraper = DirectScraper(
                client, settings.make_limiter("direct"), settings.retry_attempts,
                max_pages=settings.direct_max_pages, cutoff=cutoff,
            )

            total_companies = (
                len(greenhouse_companies)
                + len(lever_companies)
                + len(ashby_companies)
                + len(bamboohr_companies)
                + len(workday_companies)
                + len(direct_companies)
            )
            prog_ctx = (
                Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    console=console,
                )
                if not quiet
                else _NoopProgress()
            )

            with prog_ctx as progress:
                task = progress.add_task("Scraping companies...", total=total_companies)

                async def scrape_one(company, scraper_instance, token, platform, timeout):
                    t0 = time.monotonic()
                    try:
                        jobs = await asyncio.wait_for(
                            scraper_instance.fetch_all(token),
                            timeout=timeout,
                        )
                        elapsed = time.monotonic() - t0
                        if cutoff:
                            jobs = [j for j in jobs if j.updated_at >= cutoff]
                        if location_terms:
                            jobs = [j for j in jobs if _matches_location(j, location_terms)]
                        await store.save_company(token, jobs, platform=platform, fetch_time_s=elapsed)
                        if out_queue is not None and jobs:
                            await out_queue.put((token, jobs))
                        async with lock:
                            counters["jobs"] += len(jobs)
                            if jobs:
                                counters["with_jobs"] += 1
                    except SlugNotFoundError as exc:
                        async with lock:
                            newly_bad[exc.platform].append(exc.token)
                            counters["not_found"] += 1
                    except BoardBlockedError as exc:
                        # Access refused (WAF/edge). Not pruned — retried next run.
                        elapsed = time.monotonic() - t0
                        msg = f"blocked (HTTP {exc.status_code})"
                        logger.warning("Blocked by %s — skipping (retried next run)", company.name)
                        await store.record_error(token, "blocked", msg, platform=platform, fetch_time_s=elapsed)
                        async with lock:
                            counters["blocked"] += 1
                    except asyncio.TimeoutError:
                        elapsed = time.monotonic() - t0
                        msg = f"timeout after {timeout}s"
                        logger.warning("Scrape timed out: %s — skipping", company.name)
                        await store.record_error(token, "timeout", msg, platform=platform, fetch_time_s=elapsed)
                        async with lock:
                            counters["errors"] += 1
                            errors_detail.append((company.name, msg))
                    except httpx.HTTPStatusError as exc:
                        # Expected-ish HTTP failure (e.g. 5xx after retries). Log
                        # concisely — a full traceback here is noise at scale.
                        elapsed = time.monotonic() - t0
                        msg = f"HTTP {exc.response.status_code}"
                        await store.record_error(token, "error", msg, platform=platform, fetch_time_s=elapsed)
                        logger.warning("Failed to scrape %s: %s", company.name, msg)
                        async with lock:
                            counters["errors"] += 1
                            errors_detail.append((company.name, msg))
                    except Exception as exc:
                        elapsed = time.monotonic() - t0
                        msg = str(exc)
                        await store.record_error(token, "error", msg, platform=platform, fetch_time_s=elapsed)
                        logger.exception("Failed to scrape %s", company.name)
                        async with lock:
                            counters["errors"] += 1
                            errors_detail.append((company.name, msg))
                    finally:
                        progress.advance(task)
                        if on_company_start:
                            async with lock:
                                counters["done"] += 1
                                done = counters["done"]
                            on_company_start(company.name, platform, done, total_companies)

                async def run_board(companies, scraper_instance, token_attr, platform):
                    """Drain one board's companies through a fixed pool of workers.

                    Companies wait for a worker in an UNTIMED asyncio.Queue — the
                    per-company timeout (a generous safety-net) only starts once a
                    worker actually picks the company up, so queue-wait never counts
                    against a company's budget. Concurrency per board is capped at
                    `company_workers(platform)`, decoupled from the per-call limiter.
                    """
                    if not companies:
                        return
                    workers = max(1, settings.company_workers(platform))
                    timeout = settings.company_timeout_s
                    queue: asyncio.Queue = asyncio.Queue()
                    for company in companies:
                        queue.put_nowait(company)
                    for _ in range(workers):
                        queue.put_nowait(None)  # one shutdown sentinel per worker

                    async def worker():
                        while True:
                            company = await queue.get()
                            if company is None:
                                return
                            token = getattr(company, token_attr)
                            await scrape_one(company, scraper_instance, token, platform, timeout)

                    await asyncio.gather(*(worker() for _ in range(workers)))

                await asyncio.gather(
                    run_board(greenhouse_companies, greenhouse_scraper, "greenhouse_token", "greenhouse"),
                    run_board(lever_companies, lever_scraper, "lever_token", "lever"),
                    run_board(ashby_companies, ashby_scraper, "ashby_token", "ashby"),
                    run_board(bamboohr_companies, bamboohr_scraper, "bamboohr_token", "bamboohr"),
                    run_board(workday_companies, workday_scraper, "workday_token", "workday"),
                    run_board(direct_companies, direct_scraper, "direct_token", "direct"),
                )

        await store.finalise_run(started_at, stats=dict(counters))
    finally:
        if out_queue is not None:
            await out_queue.put(None)

    # Persist newly discovered bad slugs
    new_count = sum(len(v) for v in newly_bad.values())
    if new_count:
        for platform, tokens in newly_bad.items():
            bad_slugs[platform].update(tokens)
        _save_bad_slugs(bad_slugs)

    if not quiet:
        console.print("\n[bold]Results[/bold]")
        zero_jobs = (
            total_companies
            - counters["with_jobs"]
            - counters["errors"]
            - counters["not_found"]
            - counters["blocked"]
        )
        console.print(f"  [green]✓[/green] {counters['with_jobs']} companies had jobs  ({counters['jobs']} total jobs)")
        console.print(f"  [dim]·[/dim] {zero_jobs} companies: 0 jobs")
        if counters["not_found"]:
            console.print(f"  [dim]+[/dim] {counters['not_found']} new bad slugs → [cyan]{USER_BAD_SLUGS_PATH}[/cyan]")
        if counters["blocked"]:
            console.print(f"  [yellow]⊘[/yellow] {counters['blocked']} blocked (WAF/edge) — not pruned, retried next run")
        if counters["errors"]:
            console.print(f"  [red]✗[/red] {counters['errors']} errors")
            for name, msg in errors_detail:
                console.print(f"      [red]{name}[/red]: {msg}")
        console.print(
            f"\n[bold green]{counters['jobs']} total jobs[/bold green] saved to the database "
            f"(run [cyan]{run_id}[/cyan])"
        )


if __name__ == "__main__":
    asyncio.run(main())
