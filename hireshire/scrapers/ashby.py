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

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


def _parse_job(board_token: str, entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        location_name = entry.get("location") or ""

        secondary: list[dict] = entry.get("secondaryLocations") or []
        offices = [
            Office(id=i, name=sec["location"], location=sec["location"])
            for i, sec in enumerate(secondary)
            if sec.get("location")
        ]

        departments: list[Department] = []
        dept = entry.get("department")
        if dept:
            departments = [Department(id=0, name=dept)]

        published = entry.get("publishedAt")
        try:
            updated_at = datetime.fromisoformat(published) if published else scraped_at
        except (ValueError, TypeError):
            updated_at = scraped_at

        return Job(
            source="ashby",
            board_token=board_token,
            job_id=entry["id"],
            title=entry["title"],
            location=Location(name=location_name),
            departments=departments,
            offices=offices,
            absolute_url=entry["jobUrl"],
            updated_at=updated_at,
            content_text=entry.get("descriptionPlain"),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError) as exc:
        logger.warning("Failed to parse Ashby job %s from %s: %s", entry.get("id"), board_token, exc)
        return None


class AshbyScraper(AbstractScraper):
    source = "ashby"

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)

    async def fetch_all(self, board_token: str) -> list[Job]:
        scraped_at = datetime.now(timezone.utc)
        url = f"{BASE_URL}/{board_token}"

        try:
            response = await self._get(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SlugNotFoundError("ashby", board_token) from exc
            raise

        data = response.json()
        entries: list[dict] = data.get("jobs") or []
        if not entries:
            return []

        jobs = [_parse_job(board_token, e, scraped_at) for e in entries]
        return [j for j in jobs if j is not None]

    async def _get(self, url: str) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url)
                response.raise_for_status()
                return response

        return await _do_get()
