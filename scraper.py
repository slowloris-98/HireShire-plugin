"""
Sweeps the ATS board APIs for every company in the enabled slug lists.

Slugs are loaded from config/*_companies.json in the plugin install dir. Which
boards run is set by `enabled_platforms` in scraper.yaml — a disabled board's
slug file is never even opened. Every slug in an enabled board's list is tried on
every run: a slug that 404s is recorded as an error row for that run and changes
nothing for the next one.

    python scraper.py
"""

import asyncio
import logging
import time

import httpx
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from hireshire.config import load_config
from hireshire.http_client import build_client
from hireshire.scrapers.ashby import AshbyScraper
from hireshire.scrapers.bamboohr import BambooHRScraper
from hireshire.direct.scope import resolve_scope
from hireshire.scrapers.direct import DirectScraper
from hireshire.scrapers.exceptions import BoardBlockedError, SlugNotFoundError
from hireshire.scrapers.greenhouse import GreenhouseScraper
from hireshire.scrapers.lever import LeverScraper
from hireshire.scrapers.workday import WorkdayScraper
from hireshire.storage.db import get_db
from hireshire.storage.json_store import RunStore

logger = logging.getLogger(__name__)
console = Console()

# There is deliberately NO skip list. Every slug in an enabled board's file is
# tried on every run, whatever happened to it last time.
#
# This reverses a seed-plus-delta scheme (a curated `bad_slugs.json` in ROOT plus
# `user_bad_slugs.json` / `user_recovered_slugs.json` deltas in DATA) and the
# reversal should not be undone. That list could only ever grow: it was read once
# before the sweep and never re-checked during one, so a slug that 404'd through a
# transient outage — or a company that moved boards and came back — was skipped on
# every future sweep. The only road back was `verify_bad_slugs.py --prune`, a
# terminal command, in a plugin whose whole premise is that users never open a
# terminal. So the cost of being wrong was permanent and invisible, and it landed
# on exactly the employers a user would most want re-checked.
#
# What it bought was requests: ~6,066 of a default sweep's 15,871 companies were
# skipped. That is the price now paid, and it is paid in a phase that is already
# I/O-bound and rate-limited per board. A 404 is handled per company by
# `scrape_one` and recorded as an error row for that run only.


def _matches_location(job, terms: list[str]) -> bool:
    # A direct-portal job whose list entry named no place carries the search it
    # came from instead; the portal was already scoped to the user's countries.
    if getattr(job, "location_is_placeholder", False):
        return True
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

    # A board absent from settings.enabled_platforms has no companies loaded at
    # all — load_config() never opened its slug file — so these lists collapse to
    # empty and run_board returns immediately. Nothing else is filtered: see the
    # note above on why there is no skip list.
    ashby_companies = list(config.ashby_companies)
    greenhouse_companies = list(config.greenhouse_companies)
    lever_companies = list(config.lever_companies)
    bamboohr_companies = list(config.bamboohr_companies)
    workday_companies = list(config.workday_companies)
    direct_companies = list(config.direct_companies)

    if not (greenhouse_companies or lever_companies or ashby_companies
            or bamboohr_companies or workday_companies or direct_companies):
        if not quiet:
            console.print("[yellow]No companies to scrape. Check `enabled_platforms` in scraper.yaml.[/yellow]")
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
        console.print(f"Fetching from {' + '.join(sources)}")
        console.print()

    lock = asyncio.Lock()

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
                scope=resolve_scope(settings.location_filter) if direct_companies else None,
            )

            total_companies = (
                len(greenhouse_companies)
                + len(lever_companies)
                + len(ashby_companies)
                + len(bamboohr_companies)
                + len(workday_companies)
                + len(direct_companies)
            )
            await store.set_companies_total(total_companies)
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
                    except SlugNotFoundError:
                        # No board behind this slug. Contained to the one company:
                        # recorded under its own status so it stays separable from a
                        # real failure, counted for the summary line, and tried again
                        # next run like everything else. It is much the commonest
                        # outcome on a full sweep, so it logs at debug — a warning per
                        # dead slug would bury every other line in the log.
                        elapsed = time.monotonic() - t0
                        logger.debug("No board for %s (%s) — retried next run", company.name, token)
                        await store.record_error(
                            token, "not_found", "no board for this slug",
                            platform=platform, fetch_time_s=elapsed,
                        )
                        async with lock:
                            counters["not_found"] += 1
                    except BoardBlockedError as exc:
                        # Access refused (WAF/edge). Retried next run.
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
                        await store.company_done()
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
            console.print(f"  [dim]·[/dim] {counters['not_found']} companies: no board for this slug")
        if counters["blocked"]:
            console.print(f"  [yellow]⊘[/yellow] {counters['blocked']} blocked (WAF/edge) — retried next run")
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
