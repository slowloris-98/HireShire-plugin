"""Gem job boards (jobs.gem.com/<slug>).

Gem's public job-board API answers one GET with the whole board as a bare JSON list,
each posting carrying its description inline -- so, like Greenhouse, Ashby and Lever,
there is no detail fetch and full-text reranking is free. An unknown slug answers a
clean 404 ("Job board with vanity URL path ... not found").

Department and office ids are opaque base64 strings, while the shared models type them
`int`, so they are numbered by position the way Ashby's are. Nothing downstream joins on
them.
"""
from __future__ import annotations

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

BASE_URL = "https://api.gem.com/job_board/v0"


def board_url(board_token: str) -> str:
    return f"{BASE_URL}/{board_token}/job_posts/"


def _name(raw: object) -> str:
    if isinstance(raw, dict):
        return raw.get("name") or ""
    return raw if isinstance(raw, str) else ""


def _parse_date(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python < 3.11 cannot read a trailing `Z`.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_job(board_token: str, entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        offices: list[Office] = []
        for i, office in enumerate(entry.get("offices") or []):
            # An office's own `location` is where its jobs are ("United States -
            # Remote"); its `name` is the office ("Los Angeles"). Both feed the
            # location gate's haystack, so keep both.
            name = office.get("name") or _name(office.get("location"))
            if name:
                offices.append(Office(id=i, name=name, location=_name(office.get("location")) or None))

        departments = [
            Department(id=i, name=d["name"])
            for i, d in enumerate(entry.get("departments") or [])
            if d.get("name")
        ]

        # Publication date, not `updated_at`: an edit bumps the latter, and the age
        # cutoff would then re-admit an old posting as new. Same choice as Ashby's
        # `publishedAt`.
        updated_at = (
            _parse_date(entry.get("first_published_at"))
            or _parse_date(entry.get("created_at"))
            or _parse_date(entry.get("updated_at"))
            or scraped_at
        )

        return Job(
            source="gem",
            board_token=board_token,
            job_id=str(entry["id"]),
            internal_job_id=entry.get("internal_job_id") or None,
            title=entry["title"],
            location=Location(name=_name(entry.get("location"))),
            departments=departments,
            offices=offices,
            absolute_url=entry["absolute_url"],
            updated_at=updated_at,
            requisition_id=entry.get("requisition_id") or None,
            # Real HTML, not entity-escaped like Greenhouse's, so Job.strip_html's one
            # pass is enough. `content_plain` exists too but flattens list structure.
            content_text=entry.get("content") or entry.get("content_plain"),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Gem job %s from %s: %s", entry.get("id"), board_token, exc)
        return None


class GemScraper(AbstractScraper):
    source = "gem"

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
        try:
            response = await self._get(board_url(board_token))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SlugNotFoundError("gem", board_token) from exc
            raise

        data = response.json()
        entries: list[dict] = data if isinstance(data, list) else []
        jobs = [_parse_job(board_token, e, scraped_at) for e in entries if isinstance(e, dict)]
        return [j for j in jobs if j is not None]

    async def _get(self, url: str) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url)
                response.raise_for_status()
                return response

        return await _do_get()
