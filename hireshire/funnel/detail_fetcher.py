from __future__ import annotations

import asyncio
import logging
import random

from hireshire.funnel.config import DetailFetchConfig
from hireshire.http_client import build_client
from hireshire.models.job import Job
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.bamboohr import BambooHRScraper
from hireshire.scrapers.direct import DirectScraper
from hireshire.scrapers.workday import WorkdayScraper

logger = logging.getLogger(__name__)

# Boards whose description lives behind a separate per-job detail call.
# "direct" covers Google/Intuit (Apple's list payload already carries the
# description, so its jobs arrive with content_text set and are skipped below).
DETAIL_SOURCES = ("workday", "bamboohr", "direct")


class DetailFetcher:
    """Hydrates list-only Workday/BambooHR jobs by fetching their detail pages,
    reusing each scraper's `fetch_detail()`. Owns one shared httpx client for the
    funnel's lifetime; a semaphore caps concurrent hydrations."""

    def __init__(self, cfg: DetailFetchConfig):
        self._cfg = cfg
        self._client = None
        self._scrapers: dict[str, object] = {}
        self._sem: asyncio.Semaphore | None = None

    async def __aenter__(self) -> "DetailFetcher":
        self._client = build_client(self._cfg.timeout_s)
        await self._client.__aenter__()
        self._sem = asyncio.Semaphore(max(1, self._cfg.concurrency))
        # The semaphore is the real throttle; give the per-call limiter a matching
        # width and no spacing so it never becomes the bottleneck.
        limiter_width = max(1, self._cfg.concurrency)
        self._scrapers = {
            "workday": WorkdayScraper(self._client, RateLimiter(limiter_width, 0.0)),
            "bamboohr": BambooHRScraper(self._client, RateLimiter(limiter_width, 0.0)),
            "direct": DirectScraper(self._client, RateLimiter(limiter_width, 0.0)),
        }
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def hydrate(self, jobs: list[Job]) -> list[Job]:
        """Fill content_text for list-only Workday/BambooHR jobs. Jobs from other
        boards, or that already carry content, pass through untouched."""

        async def _one(job: Job) -> Job:
            scraper = self._scrapers.get(job.source)
            if scraper is None or job.content_text:
                return job
            async with self._sem:
                if self._cfg.jitter_s:
                    await asyncio.sleep(random.uniform(0, self._cfg.jitter_s))
                return await scraper.fetch_detail(job)

        if not jobs:
            return []

        needed = sum(1 for j in jobs if j.source in self._scrapers and not j.content_text)
        result = list(await asyncio.gather(*[_one(j) for j in jobs]))
        if needed:
            # A hydrated job with detail_fetch_failed set means the detail call errored
            # or returned no description. Everything else in `needed` was fetched OK.
            failed = sum(1 for j in result if j.source in self._scrapers and j.detail_fetch_failed)
            logger.info(
                "Detail API: hydrated %d/%d list-only jobs (%d failed)",
                needed - failed, needed, failed,
            )
        return result
