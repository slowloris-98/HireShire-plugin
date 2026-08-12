"""
Re-check every slug in config/bad_slugs.json by re-running the real scraper
fetch_all() logic against the live APIs.

A slug stays "bad" only if it still raises SlugNotFoundError (genuine 404, or
Lever {"ok": false}). If fetch_all() succeeds — even with zero jobs — the slug is
reachable and was wrongly/transiently listed, so it is reported as recoverable.
Any other error (timeout, 5xx, network) is reported as inconclusive and never pruned.

    python scripts/verify_bad_slugs.py                 # report only (no writes)
    python scripts/verify_bad_slugs.py --prune         # also remove recoverable slugs
    python scripts/verify_bad_slugs.py --platform greenhouse
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as `python scripts/verify_bad_slugs.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from hireshire.config import load_config
from hireshire.http_client import build_client
from hireshire.scrapers.ashby import AshbyScraper
from hireshire.scrapers.bamboohr import BambooHRScraper
from hireshire.scrapers.direct import DirectScraper
from hireshire.scrapers.exceptions import SlugNotFoundError
from hireshire.scrapers.greenhouse import GreenhouseScraper
from hireshire.scrapers.lever import LeverScraper
from hireshire.scrapers.workday import WorkdayScraper
from scraper import (
    _PLATFORMS,
    USER_RECOVERED_PATH,
    _load_bad_slugs,
    _read_slug_map,
    _write_slug_map,
)

console = Console()


async def _check_slug(scraper, token, sem):
    """Return ('bad'|'recoverable'|'inconclusive', detail) for one slug."""
    try:
        jobs = await scraper.fetch_all(token)
        return "recoverable", len(jobs)
    except SlugNotFoundError:
        return "bad", None
    except Exception as exc:  # timeout / 5xx / network — can't conclude
        return "inconclusive", f"{type(exc).__name__}: {exc}"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify slugs in config/bad_slugs.json.")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove recoverable slugs from config/bad_slugs.json (default: report only).",
    )
    parser.add_argument(
        "--platform",
        choices=_PLATFORMS,
        help="Limit the check to a single platform.",
    )
    args = parser.parse_args()

    config = load_config()
    settings = config.settings

    bad_slugs = _load_bad_slugs()
    platforms = [args.platform] if args.platform else list(_PLATFORMS)

    total = sum(len(bad_slugs[p]) for p in platforms)
    if total == 0:
        console.print("[yellow]No bad slugs to check.[/yellow]")
        return

    console.print(f"[bold]Verifying {total} bad slug(s)[/bold] across: {', '.join(platforms)}\n")

    sem = asyncio.Semaphore(settings.concurrency)
    recoverable: dict[str, list[tuple[str, int]]] = {p: [] for p in _PLATFORMS}
    still_bad: dict[str, int] = {p: 0 for p in _PLATFORMS}
    inconclusive: dict[str, list[tuple[str, str]]] = {p: [] for p in _PLATFORMS}

    async with build_client(settings.request_timeout_s) as client:
        scrapers = {
            "greenhouse": GreenhouseScraper(client, sem, settings.retry_attempts),
            "lever": LeverScraper(client, sem, settings.retry_attempts, cutoff=None),
            "ashby": AshbyScraper(client, sem, settings.retry_attempts),
            "bamboohr": BambooHRScraper(client, sem, settings.retry_attempts),
            "workday": WorkdayScraper(client, sem, settings.retry_attempts, cutoff=None),
            # Present so `--platform direct` doesn't KeyError. In practice this
            # stays empty: DirectScraper never raises SlugNotFoundError, so a
            # single-tenant portal can never be pruned into bad_slugs.json.
            "direct": DirectScraper(client, sem, settings.retry_attempts),
        }

        async def run_one(platform, token):
            status, detail = await _check_slug(scrapers[platform], token, sem)
            if status == "recoverable":
                recoverable[platform].append((token, detail))
            elif status == "bad":
                still_bad[platform] += 1
            else:
                inconclusive[platform].append((token, detail))

        tasks = [
            run_one(p, token)
            for p in platforms
            for token in sorted(bad_slugs[p])
        ]
        await asyncio.gather(*tasks)

    # Report
    total_recoverable = 0
    total_inconclusive = 0
    for p in platforms:
        n_rec = len(recoverable[p])
        n_inc = len(inconclusive[p])
        total_recoverable += n_rec
        total_inconclusive += n_inc
        console.print(
            f"[bold]{p}[/bold]: {still_bad[p]} still bad, "
            f"[green]{n_rec} recoverable[/green], "
            f"[yellow]{n_inc} inconclusive[/yellow]"
        )
        for token, count in sorted(recoverable[p]):
            console.print(f"    [green]+[/green] {token}  ({count} jobs)")
        for token, err in sorted(inconclusive[p]):
            console.print(f"    [yellow]?[/yellow] {token}  ({err})")
    console.print()

    if total_recoverable == 0:
        console.print("[green]All checked slugs are genuinely bad.[/green]")
    else:
        console.print(
            f"[bold green]{total_recoverable}[/bold green] recoverable slug(s) found."
            + ("" if args.prune else "  Re-run with [cyan]--prune[/cyan] to remove them.")
        )
    if total_inconclusive:
        console.print(
            f"[yellow]{total_inconclusive} slug(s) inconclusive[/yellow] — not pruned; re-run later."
        )

    if args.prune and total_recoverable:
        # Record recoveries as a delta rather than editing the shipped seed: the
        # seed lives in the read-only install dir and is replaced wholesale on
        # every plugin update, so an edit there would be silently reverted.
        # Writing to user_recovered keeps the slug enabled across releases.
        recovered = _read_slug_map(USER_RECOVERED_PATH)
        for p in platforms:
            for token, _ in recoverable[p]:
                recovered[p].add(token)
        _write_slug_map(USER_RECOVERED_PATH, recovered)
        console.print(
            f"\n[bold]Recovered {total_recoverable} slug(s)[/bold] → [cyan]{USER_RECOVERED_PATH}[/cyan]."
        )


if __name__ == "__main__":
    asyncio.run(main())
