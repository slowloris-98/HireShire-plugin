from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import ValidationError

from hireshire.http_client import make_retry_decorator
from hireshire.models.job import ApplicationQuestion, Department, Job, Location
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.base import AbstractScraper
from hireshire.scrapers.exceptions import SlugNotFoundError

logger = logging.getLogger(__name__)

# Public (unauthenticated) BambooHR careers endpoints. Not the api.bamboohr.com
# gateway — these are the same JSON feeds the hosted careers page consumes.
LIST_URL = "https://{slug}.bamboohr.com/careers/list"
DETAIL_URL = "https://{slug}.bamboohr.com/careers/{job_id}/detail"

# Some BambooHR tenants soft-block the default HireShire UA; send a browser one.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}


def _parse_location(raw) -> Location:
    if isinstance(raw, dict):
        city = raw.get("city") or ""
        state = raw.get("state") or ""
        name = ", ".join(p for p in (city, state) if p)
        return Location(name=name or "Not specified")
    return Location(name=str(raw) if raw else "Not specified")


def _parse_questions(form_fields) -> list[ApplicationQuestion]:
    """BambooHR formFields is a dict keyed by field name; each value carries
    label/isRequired and optional {id,text} options."""
    result: list[ApplicationQuestion] = []
    if not isinstance(form_fields, dict):
        return result
    for field_name, spec in form_fields.items():
        if not isinstance(spec, dict):
            continue
        try:
            result.append(ApplicationQuestion(
                label=spec.get("label") or field_name,
                required=bool(spec.get("isRequired", False)),
                field_type=field_name,
                values=[o["text"] for o in spec.get("options", []) if isinstance(o, dict) and "text" in o],
            ))
        except (KeyError, ValidationError):
            pass
    return result


def _parse_job(
    slug: str,
    list_entry: dict,
    detail: Optional[dict],
    scraped_at: datetime,
    deferred: bool = False,
) -> Optional[Job]:
    try:
        job_id = str(list_entry["id"])
        opening = (detail or {}).get("jobOpening") or {}

        content_html = opening.get("description")
        location = _parse_location(opening.get("location") or list_entry.get("location") or {})

        dept_label = opening.get("departmentLabel") or list_entry.get("departmentLabel")
        departments = [Department(id=0, name=dept_label)] if dept_label else []

        updated_at = opening.get("datePosted") or scraped_at
        absolute_url = opening.get("jobOpeningShareUrl") or f"https://{slug}.bamboohr.com/careers/{job_id}"
        questions = _parse_questions((detail or {}).get("formFields"))

        return Job(
            source="bamboohr",
            board_token=slug,
            job_id=job_id,
            title=list_entry.get("jobOpeningName") or opening.get("jobOpeningName") or "",
            location=location,
            departments=departments,
            absolute_url=absolute_url,
            updated_at=updated_at,
            content_text=content_html,
            questions=questions,
            # detail is None both when a fetch failed and in list-only mode where it
            # was never attempted (`deferred=True`); only the former is a failure.
            detail_fetch_failed=(detail is None and not deferred),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, AttributeError) as exc:
        logger.warning("Failed to parse BambooHR job %s from %s: %s", list_entry.get("id"), slug, exc)
        return None


class BambooHRScraper(AbstractScraper):
    source = "bamboohr"

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
        detail_concurrency: int = 4,
        detail_jitter_s: float = 0.3,
        fetch_detail: bool = True,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)
        self._detail_concurrency = max(1, detail_concurrency)
        self._detail_jitter_s = max(0.0, detail_jitter_s)
        self._fetch_detail = fetch_detail

    async def _fetch_list_entries(self, slug: str) -> list[dict]:
        """Fetch the raw careers list (openings) for a slug. Raises SlugNotFoundError
        when the board is dead (redirect to marketing site / 403 / 404 / 410 / non-JSON)."""
        # Fetch the list WITHOUT following redirects: a dead board 302-redirects to
        # the bamboohr.com marketing site, which is the clearest "slug not found" signal.
        try:
            response = await self._get(LIST_URL.format(slug=slug), follow_redirects=False)
        except httpx.HTTPStatusError as exc:
            # A dead board 302-redirects to the marketing site; 403/404/410 are also "no board".
            if exc.response.is_redirect or exc.response.status_code in (403, 404, 410):
                raise SlugNotFoundError("bamboohr", slug) from exc
            raise

        if response.status_code != 200:
            raise SlugNotFoundError("bamboohr", slug)

        # A 200 that isn't JSON also means there's no real board here.
        if "application/json" not in response.headers.get("content-type", ""):
            raise SlugNotFoundError("bamboohr", slug)

        return (response.json() or {}).get("result", [])

    async def fetch_listings(self, slug: str) -> list[tuple[str, Optional[datetime]]]:
        """List-only classification helper: return (title, None) per opening WITHOUT any
        detail fetch. BambooHR's list carries no posting date (it lives on the detail
        endpoint), but a careers list only exposes currently-open roles, so the caller
        treats these as within any recent window. Raises SlugNotFoundError like fetch_all."""
        entries = await self._fetch_list_entries(slug)
        return [((e.get("jobOpeningName") or ""), None) for e in entries]

    async def fetch_all(self, slug: str) -> list[Job]:
        entries = await self._fetch_list_entries(slug)
        if not entries:
            return []

        scraped_at = datetime.now(timezone.utc)

        # List-only mode: skip the per-job detail fetch and defer the description to
        # the matcher funnel, which hydrates only jobs that pass its relevance gate.
        if not self._fetch_detail:
            jobs = [_parse_job(slug, e, None, scraped_at, deferred=True) for e in entries]
            return [j for j in jobs if j is not None]

        # Per-tenant cap: a large board must not fire hundreds of detail fetches at once.
        detail_sem = asyncio.Semaphore(self._detail_concurrency)
        tasks = [self._fetch_detail_and_parse(slug, entry, scraped_at, detail_sem) for entry in entries]
        results = await asyncio.gather(*tasks)
        return [j for j in results if j is not None]

    async def fetch_detail(self, job: Job) -> Job:
        """Hydrate a list-only Job with its description via the careers detail endpoint.

        Rebuilds the detail URL from board_token (slug) + job_id. Returns a new Job
        (validated so content_text HTML->text stripping runs); on failure returns the
        job with `detail_fetch_failed=True`."""
        try:
            response = await self._get(DETAIL_URL.format(slug=job.board_token, job_id=job.job_id))
            opening = ((response.json() or {}).get("result") or {}).get("jobOpening") or {}
            raw_html = opening.get("description")
        except Exception as exc:
            logger.warning("Detail hydrate failed for BambooHR job %s/%s: %s", job.board_token, job.job_id, exc)
            return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

        return job.model_validate({
            **job.model_dump(),
            "content_text": raw_html,
            "detail_fetch_failed": raw_html is None,
        })

    async def _fetch_detail_and_parse(
        self, slug: str, list_entry: dict, scraped_at: datetime, detail_sem: asyncio.Semaphore
    ) -> Optional[Job]:
        job_id = list_entry.get("id")
        detail = None
        try:
            async with detail_sem:
                if self._detail_jitter_s:
                    await asyncio.sleep(random.uniform(0, self._detail_jitter_s))
                response = await self._get(DETAIL_URL.format(slug=slug, job_id=job_id))
                detail = (response.json() or {}).get("result")
        except Exception as exc:
            logger.warning("Detail fetch failed for BambooHR job %s/%s: %s", slug, job_id, exc)

        return _parse_job(slug, list_entry, detail, scraped_at)

    async def _get(self, url: str, follow_redirects: bool = True) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url, headers=_HEADERS, follow_redirects=follow_redirects)
                # 3xx (redirect) is not an error status, so raise_for_status won't fire on it —
                # callers that disable redirects inspect response.is_redirect themselves.
                response.raise_for_status()
                return response

        return await _do_get()
