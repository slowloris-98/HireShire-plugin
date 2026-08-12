from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from pydantic import ValidationError

from hireshire.http_client import make_retry_decorator
from hireshire.models.job import Job, Location
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.base import AbstractScraper
from hireshire.scrapers.exceptions import BoardBlockedError, SlugNotFoundError

logger = logging.getLogger(__name__)

PAGE_SIZE = 20

# Workday CXS tenants soft-block non-browser UAs; also expect a JSON POST.
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}


def _parse_posted_on(text) -> Optional[datetime]:
    """Convert Workday's relative 'Posted 2 Days Ago' string to a UTC datetime.
    Only used as a fallback when the detail endpoint's ISO startDate is missing."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().lower()
    now = datetime.now(timezone.utc)
    if "today" in t:
        return now
    for unit, delta in (("day", timedelta(days=1)), ("week", timedelta(weeks=1)), ("month", timedelta(days=30))):
        m = re.search(rf"(\d+)\s+{unit}", t)
        if m:
            return now - int(m.group(1)) * delta
    return None


class _WorkdayUrls:
    """Resolve the base/cxs/site URLs from a 'company|wd#|site_id' token."""

    def __init__(self, token: str):
        parts = token.split("|")
        if len(parts) != 3:
            raise SlugNotFoundError("workday", token)
        self.company, wd, self.site = parts
        wd_num = wd.replace("wd", "")
        self.base = f"https://{self.company}.wd{wd_num}.myworkdayjobs.com"
        self.cxs = f"{self.base}/wday/cxs/{self.company}/{self.site}"


def _parse_job(
    token: str,
    urls: _WorkdayUrls,
    list_entry: dict,
    detail: Optional[dict],
    scraped_at: datetime,
    deferred: bool = False,
) -> Optional[Job]:
    try:
        info = (detail or {}).get("jobPostingInfo") or {}
        external_path = list_entry.get("externalPath") or ""

        bullet = list_entry.get("bulletFields") or []
        req_id = info.get("jobReqId") or (bullet[0] if bullet else None)
        job_id = req_id or external_path or list_entry.get("title", "")

        location_name = info.get("location") or list_entry.get("locationsText") or "Not specified"
        content_html = info.get("jobDescription")

        start_date = info.get("startDate")
        updated_at = start_date or _parse_posted_on(list_entry.get("postedOn")) or scraped_at

        absolute_url = info.get("externalUrl") or f"{urls.base}/{urls.site}{external_path}"

        return Job(
            source="workday",
            board_token=token,
            job_id=str(job_id),
            title=info.get("title") or list_entry.get("title") or "",
            location=Location(name=location_name),
            absolute_url=absolute_url,
            updated_at=updated_at,
            requisition_id=str(req_id) if req_id else None,
            content_text=content_html,
            detail_path=external_path or None,
            questions=[],
            # detail is None here in two distinct cases: a fetch that failed, and
            # list-only mode where it was never attempted. `deferred=True` (set by
            # fetch_all in list-only mode) marks the latter so a missing detail is
            # not reported as a failure — the funnel will hydrate it later.
            detail_fetch_failed=(detail is None and not deferred),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, AttributeError) as exc:
        logger.warning("Failed to parse Workday job %s from %s: %s", list_entry.get("externalPath"), token, exc)
        return None


class WorkdayScraper(AbstractScraper):
    source = "workday"

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
        cutoff: Optional[datetime] = None,
        detail_concurrency: int = 4,
        detail_jitter_s: float = 0.3,
        fetch_detail: bool = True,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)
        self._cutoff = cutoff
        self._detail_concurrency = max(1, detail_concurrency)
        self._detail_jitter_s = max(0.0, detail_jitter_s)
        self._fetch_detail = fetch_detail

    async def fetch_all(self, token: str) -> list[Job]:
        urls = _WorkdayUrls(token)  # raises SlugNotFoundError on a malformed token
        list_entries = await self._fetch_all_pages(token, urls)
        if not list_entries:
            return []

        scraped_at = datetime.now(timezone.utc)

        # List-only mode: build jobs from the cheap list payload alone and defer the
        # per-job detail fetch (the description) to the matcher funnel, which only
        # hydrates jobs that survive its relevance gate.
        if not self._fetch_detail:
            jobs = [_parse_job(token, urls, e, None, scraped_at, deferred=True) for e in list_entries]
            return [j for j in jobs if j is not None]

        # Per-tenant cap so a big board (e.g. 1,200+ jobs) can't flood its own host
        # with concurrent detail fetches and trip Workday's per-tenant 429 throttle.
        detail_sem = asyncio.Semaphore(self._detail_concurrency)
        tasks = [self._fetch_detail_and_parse(token, urls, entry, scraped_at, detail_sem) for entry in list_entries]
        results = await asyncio.gather(*tasks)
        return [j for j in results if j is not None]

    async def fetch_detail(self, job: Job) -> Job:
        """Hydrate a list-only Job with its description by fetching the detail page.

        Reconstructs the CXS detail URL from the token + stored `detail_path`
        (externalPath). Returns a new Job (rebuilt through validation so the
        content_text HTML->text stripping runs); on failure returns the job with
        `detail_fetch_failed=True` and content_text left as-is (None)."""
        try:
            urls = _WorkdayUrls(job.board_token)
            external_path = job.detail_path or ""
            response = await self._get(f"{urls.cxs}{external_path}")
            detail = response.json()
            info = (detail or {}).get("jobPostingInfo") or {}
            raw_html = info.get("jobDescription")
        except Exception as exc:
            logger.warning("Detail hydrate failed for Workday job %s%s: %s", job.board_token, job.detail_path, exc)
            return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

        return job.model_validate({
            **job.model_dump(),
            "content_text": raw_html,
            "detail_fetch_failed": raw_html is None,
        })

    async def fetch_listings(self, token: str) -> list[tuple[str, Optional[datetime]]]:
        """List-only classification helper: return (title, posted_date) per posting
        WITHOUT any detail fetch. `posted_date` comes from the list-level relative
        'postedOn' string and is None when absent/unparseable. Raises
        SlugNotFoundError / BoardBlockedError exactly like `fetch_all`.

        Instantiate the scraper with `cutoff=None` so `_fetch_all_pages` returns the
        full board (the caller applies its own age window)."""
        urls = _WorkdayUrls(token)
        entries = await self._fetch_all_pages(token, urls)
        return [((e.get("title") or ""), _parse_posted_on(e.get("postedOn"))) for e in entries]

    async def _fetch_all_pages(self, token: str, urls: _WorkdayUrls) -> list[dict]:
        entries: list[dict] = []
        offset = 0
        while True:
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
            try:
                response = await self._post(f"{urls.cxs}/jobs", payload)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # 404/410 = gone; 422 = Workday's generic "this tenant/site path is
                # not a valid board" (empty-message HTTP_422). All mean no board here.
                if status in (404, 410, 422):
                    raise SlugNotFoundError("workday", token) from exc
                # 401/403 = the tenant's WAF/edge is refusing us (IP reputation, bot
                # detection). Usually transient/IP-based, so surface as a blocked
                # outcome rather than pruning the slug or dumping a traceback.
                if status in (401, 403):
                    raise BoardBlockedError("workday", token, status) from exc
                raise

            data = response.json()
            postings = data.get("jobPostings") or []
            total = data.get("total", 0)
            if not postings:
                break

            for entry in postings:
                # Age pre-filter on the cheap list-level 'postedOn' so we don't fetch
                # a detail page for every stale job. Unparseable dates are kept — the
                # precise startDate filter downstream still catches them.
                if self._cutoff:
                    posted = _parse_posted_on(entry.get("postedOn"))
                    if posted is not None and posted < self._cutoff:
                        continue
                entries.append(entry)

            offset += PAGE_SIZE
            if offset >= total:
                break

        return entries

    async def _fetch_detail_and_parse(
        self, token: str, urls: _WorkdayUrls, list_entry: dict, scraped_at: datetime, detail_sem: asyncio.Semaphore
    ) -> Optional[Job]:
        external_path = list_entry.get("externalPath") or ""
        detail = None
        try:
            async with detail_sem:
                if self._detail_jitter_s:
                    await asyncio.sleep(random.uniform(0, self._detail_jitter_s))
                # externalPath already begins with '/job/...'; append directly to cxs.
                response = await self._get(f"{urls.cxs}{external_path}")
                detail = response.json()
        except Exception as exc:
            logger.warning("Detail fetch failed for Workday job %s%s: %s", token, external_path, exc)

        return _parse_job(token, urls, list_entry, detail, scraped_at)

    async def _post(self, url: str, payload: dict) -> httpx.Response:
        @self._retry
        async def _do_post():
            async with self._limiter:
                response = await self._client.post(url, json=payload, headers=_HEADERS)
                response.raise_for_status()
                return response

        return await _do_post()

    async def _get(self, url: str) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url, headers=_HEADERS)
                response.raise_for_status()
                return response

        return await _do_get()
