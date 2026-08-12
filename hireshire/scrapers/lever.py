from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import ValidationError

from hireshire.http_client import make_retry_decorator
from hireshire.models.job import Department, Job, Location, Office
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.base import AbstractScraper
from hireshire.scrapers.exceptions import SlugNotFoundError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.lever.co/v0/postings"
PAGE_SIZE = 100


def _parse_job(board_token: str, entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        cats = entry.get("categories") or {}

        location_name = cats.get("location") or ""
        all_locs: list[str] = cats.get("allLocations") or []
        offices = [Office(id=i, name=loc, location=loc) for i, loc in enumerate(all_locs)]

        departments: list[Department] = []
        team = cats.get("team") or cats.get("department")
        if team:
            departments = [Department(id=0, name=team)]

        content_html = (
            (entry.get("opening") or "")
            + (entry.get("description") or "")
            + (entry.get("additional") or "")
        ) or None

        created_ms: Optional[int] = entry.get("createdAt")
        updated_at = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
            if created_ms
            else scraped_at
        )

        return Job(
            source="lever",
            board_token=board_token,
            job_id=entry["id"],
            title=entry["text"],
            location=Location(name=location_name),
            departments=departments,
            offices=offices,
            absolute_url=entry["hostedUrl"],
            updated_at=updated_at,
            content_text=content_html,
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError) as exc:
        logger.warning("Failed to parse Lever job %s from %s: %s", entry.get("id"), board_token, exc)
        return None


class LeverScraper(AbstractScraper):
    source = "lever"

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
        cutoff: Optional[datetime] = None,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)
        self._cutoff = cutoff

    async def fetch_all(self, board_token: str) -> list[Job]:
        scraped_at = datetime.now(timezone.utc)
        entries = await self._fetch_all_pages(board_token)
        jobs = [_parse_job(board_token, e, scraped_at) for e in entries]
        return [j for j in jobs if j is not None]

    async def _fetch_all_pages(self, board_token: str) -> list[dict]:
        all_entries: list[dict] = []
        skip = 0

        while True:
            url = f"{BASE_URL}/{board_token}?mode=json&limit={PAGE_SIZE}&skip={skip}"
            try:
                response = await self._get(url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise SlugNotFoundError("lever", board_token) from exc
                raise

            data = response.json()

            # Lever returns {"ok": false, "error": "..."} for unknown companies
            if isinstance(data, dict) and not data.get("ok", True):
                raise SlugNotFoundError("lever", board_token)

            if not isinstance(data, list) or not data:
                break

            for entry in data:
                if self._cutoff:
                    created_ms = entry.get("createdAt")
                    if created_ms and datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) < self._cutoff:
                        continue
                all_entries.append(entry)

            if len(data) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        return all_entries

    async def _get(self, url: str) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url)
                response.raise_for_status()
                return response

        return await _do_get()
