"""Direct career portals — one scraper, one handler per company.

Google, Apple and Intuit run their own portals rather than a multi-tenant ATS,
so there is no slug list: `config/direct_companies.json` holds the fixed set of
company names, and each maps to a handler module in `handlers/`.

Modelling all three as ONE platform (`source="direct"`, `board_token=<company>`)
rather than three keeps the DB rows identical to those the `/scrape-direct`
skill already wrote, so `seen_jobs` dedupe carries across the migration.

Microsoft and Meta are deliberately NOT here — both hard-block plain HTTP
(Eightfold 403 "Not authorized for PCSX"; Meta 400 on every request) and stay
with the browser-driven `/scrape-direct` skill.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import httpx

from hireshire.http_client import make_retry_decorator
from hireshire.models.job import Job
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.base import AbstractScraper
from hireshire.scrapers.handlers import apple, google, intuit

logger = logging.getLogger(__name__)

SOURCE = "direct"

_HANDLERS = {
    apple.TOKEN: apple,
    google.TOKEN: google,
    intuit.TOKEN: intuit,
}

# These portals soft-block non-browser user agents, so present a browser one
# (same approach as workday.py / bamboohr.py).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class DirectScraper(AbstractScraper):
    source = SOURCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
        max_pages: int = 5,
        cutoff: Optional[datetime] = None,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)
        self.max_pages = max(1, max_pages)
        self.cutoff = cutoff

    async def fetch_all(self, board_token: str) -> list[Job]:
        handler = _HANDLERS.get(board_token)
        if handler is None:
            # Unknown company in direct_companies.json. Deliberately NOT
            # SlugNotFoundError — see the note on that in fetch_detail below.
            logger.warning("No direct handler for '%s' — skipping", board_token)
            return []
        return await handler.fetch_list(self, board_token)

    async def fetch_detail(self, job: Job) -> Job:
        """Hydrate a list-only job. Called by the matcher funnel.

        Apple's handler has no fetch_detail: its list payload already carries
        the description, so those jobs never reach here with empty content.
        """
        handler = _HANDLERS.get(job.board_token)
        fetch = getattr(handler, "fetch_detail", None) if handler else None
        if fetch is None:
            return job
        return await fetch(self, job)

    async def get(self, url: str, headers: Optional[dict] = None) -> httpx.Response:
        """Shared fetch with retry + rate limiting.

        The retry decorator wraps limiter acquisition so `request_timeout_s`
        measures network time only, never queue wait — same ordering as every
        other scraper's `_get`.

        NOTE: this never maps a 404 to SlugNotFoundError. A single-tenant portal
        has no "wrong slug", and SlugNotFoundError would permanently prune the
        company into config/bad_slugs.json — one transient layout change would
        silently disable Google forever. Failures propagate as ordinary HTTP
        errors so the run records an error row and retries next time.
        """
        merged = {**_HEADERS, **(headers or {})}

        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url, headers=merged)
                response.raise_for_status()
                return response

        return await _do_get()
