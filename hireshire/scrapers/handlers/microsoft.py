"""Microsoft — apply.careers.microsoft.com (Eightfold AI).

`/api/pcsx/search` answers plain HTTP with no session. It is a different endpoint
from `/api/apply/v2/jobs`, which answers `403 {"message": "Not authorized for
PCSX"}` — that 403 is why Microsoft was once scraped through a browser.

Two things about the search that shape this module:

- **The page size is fixed at 10.** A `num=` parameter is ignored, so the walk
  takes twice `direct_max_pages` pages to cover about as many jobs as Apple or
  Google do with 20 per page.
- **`location=` takes one country.** A second `location=` is silently ignored, so
  a scope of several countries is walked as one series per country, deduped.

The list carries the posting time but not the description; `fetch_detail` reads
it from `/api/pcsx/position_details` when the matcher funnel asks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.scope import Scope
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Department, Job, Location

logger = logging.getLogger(__name__)

TOKEN = "microsoft"
SCOPE_COLUMN = "microsoft"
BASE = "https://apply.careers.microsoft.com"
SEARCH_URL = BASE + "/api/pcsx/search"
DETAIL_API = BASE + "/api/pcsx/position_details"
JOB_URL = BASE + "/careers/job/{native_id}"
PAGE_SIZE = 10
# Pages walked per country for each page of `direct_max_pages`, which is sized
# for the 20-job pages of Apple and Google.
PAGES_PER_UNIT = 2


def list_url(country: Optional[str], page: int) -> str:
    params = {
        "domain": "microsoft.com",
        "query": "",
        "location": country or "",
        "start": PAGE_SIZE * (page - 1),
        "sort_by": "timestamp",
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def _countries(scope: Optional[Scope]) -> list[Optional[str]]:
    """The `location=` values to walk; `[None]` searches unscoped."""
    names = scope.for_portal(SCOPE_COLUMN) if scope else None
    return list(names) if names else [None]


def _posted(entry: dict, scraped_at: datetime) -> datetime:
    ts = entry.get("postedTs") or entry.get("creationTs")
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return scraped_at


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry["id"])
        # `locations` is the long form ("United States, Washington, Redmond"), which
        # names the country. `standardizedLocations` shortens it to "Redmond, WA, US"
        # and sometimes to a bare "US", which no country inference can read.
        raw = " | ".join(entry.get("locations") or entry.get("standardizedLocations") or [])

        departments: list[Department] = []
        if entry.get("department"):
            departments = [Department(id=0, name=entry["department"])]

        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=(entry.get("name") or "").strip(),
            location=Location(name=normalize_location(raw) or "N/A"),
            departments=departments,
            absolute_url=JOB_URL.format(native_id=native_id),
            updated_at=_posted(entry, scraped_at),
            requisition_id=entry.get("displayJobId") or entry.get("atsJobId"),
            detail_path=native_id,
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Microsoft job %s: %s", entry.get("id"), exc)
        return None


def _positions(response) -> Optional[list[dict]]:
    """The page's positions, or None when the answer was not the search JSON."""
    try:
        return response.json()["data"]["positions"]
    except (ValueError, KeyError, TypeError):
        return None


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for country in _countries(ctx.scope):
        for page in range(1, ctx.max_pages * PAGES_PER_UNIT + 1):
            response = await ctx.get(list_url(country, page))
            positions = _positions(response)
            if positions is None:
                logger.warning("Microsoft page %d (%s) was not search JSON",
                               page, country or "everywhere")
                break
            page_jobs = [j for j in (_parse_job(e, scraped_at) for e in positions) if j]
            if not page_jobs:
                break

            fresh = [j for j in page_jobs if j.job_id not in seen]
            seen.update(j.job_id for j in fresh)
            out.extend(fresh)

            # `timestamp` order is only roughly newest-first, so stop on a whole
            # page past the cutoff, never on the first old job.
            if ctx.cutoff and all(j.updated_at < ctx.cutoff for j in page_jobs):
                break

    return out


async def fetch_detail(ctx, job: Job) -> Job:
    native_id = job.detail_path or job.job_id.rsplit(":", 1)[-1]
    try:
        params = {"position_id": native_id, "domain": "microsoft.com", "hl": "en"}
        response = await ctx.get(f"{DETAIL_API}?{urlencode(params)}")
        html = (response.json().get("data") or {}).get("jobDescription") or ""
        # Job.content_text strips the HTML; measure the text, not the markup.
        if len(BeautifulSoup(html, "lxml").get_text(" ", strip=True)) < 300:
            raise ValueError("position_details yielded no usable description")
    except Exception as exc:
        logger.warning("Detail hydrate failed for Microsoft job %s: %s", job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "content_text": html,
        "detail_fetch_failed": False,
    })
