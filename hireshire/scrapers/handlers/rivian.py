"""Rivian — careers.rivian.com, a Jibe board (applications go to iCIMS).

`/api/jobs` answers plain HTTP, honours `limit=100` and `sortBy=posted_date`
newest-first, and carries the whole description in the list — the
responsibilities and qualifications are already inside `description` — so there
is no `fetch_detail`.

**Rivian has no scope column, deliberately.** Its `country=` filter answers a
value it has no openings for with zero jobs and a 200 (measured: `India` → 0),
which is the silent-empty hazard `portal_locations` exists to avoid, and the
facet only exists where Rivian currently hires, like Intuit's. The whole board
is ~1,000 jobs, so it is read newest-first unscoped, the walk stops at the
age cutoff, and `scraper._matches_location` narrows it afterwards — the Meta
pattern.

The location is built from the structured `city`/`state`/`country` fields,
never `full_location`, which drops the country ("Atlanta, Georgia" — the state
or the republic?). `additional_locations` carries the rest of a multi-site job
in the same shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Department, Job, Location

logger = logging.getLogger(__name__)

TOKEN = "rivian"
# None: the portal is not scoped; the scraper's location filter does the
# narrowing after fetch (see the module docstring).
SCOPE_COLUMN = None
BASE = "https://careers.rivian.com"
LIST_API = BASE + "/api/jobs"
JOB_URL = BASE + "/jobs/{slug}"
PAGE_SIZE = 100
# Pages walked for each page of `direct_max_pages`. At 100 jobs a page, the
# default 5 covers the whole board twice over; the cutoff ends it far sooner.
PAGES_PER_UNIT = 2


def list_url(page: int) -> str:
    params = {
        "page": page,
        "limit": PAGE_SIZE,
        "sortBy": "posted_date",
        "descending": "true",
    }
    return f"{LIST_API}?{urlencode(params)}"


def _place(loc: dict) -> str:
    return ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p)


def _location(entry: dict) -> str:
    places = [_place(entry)]
    places += [_place(loc) for loc in entry.get("additional_locations") or []
               if isinstance(loc, dict)]
    places = list(dict.fromkeys(p for p in places if p))   # dedupe, keep order
    if not places and entry.get("full_location"):
        places = [entry["full_location"]]
    return " | ".join(places)


def _posted(entry: dict, scraped_at: datetime) -> datetime:
    raw = entry.get("posted_date") or entry.get("create_date")
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return scraped_at


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry.get("req_id") or entry["slug"])
        slug = entry.get("slug") or native_id

        departments: list[Department] = []
        categories = entry.get("categories") or []
        if categories and (categories[0] or {}).get("name"):
            departments = [Department(id=0, name=categories[0]["name"].strip())]

        meta = entry.get("meta_data") or {}
        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=(entry.get("title") or "").strip(),
            location=Location(name=normalize_location(_location(entry)) or "N/A"),
            departments=departments,
            absolute_url=meta.get("canonical_url") or JOB_URL.format(slug=slug),
            updated_at=_posted(entry, scraped_at),
            requisition_id=entry.get("req_id"),
            # The list payload carries the description — nothing to defer.
            content_text=entry.get("description"),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Rivian job %s: %s", entry.get("req_id"), exc)
        return None


def _entries(response) -> Optional[list[dict]]:
    """The page's job entries, or None when the answer was not the list JSON."""
    try:
        return [item["data"] for item in response.json()["jobs"]]
    except (ValueError, KeyError, TypeError):
        return None


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for page in range(1, ctx.max_pages * PAGES_PER_UNIT + 1):
        entries = _entries(await ctx.get(list_url(page)))
        if entries is None:
            logger.warning("Rivian page %d was not list JSON", page)
            break
        page_jobs = [j for j in (_parse_job(e, scraped_at) for e in entries) if j]
        if not page_jobs:
            break

        fresh = [j for j in page_jobs if j.job_id not in seen]
        seen.update(j.job_id for j in fresh)
        out.extend(fresh)

        if len(entries) < PAGE_SIZE:
            break
        if ctx.cutoff and all(j.updated_at < ctx.cutoff for j in page_jobs):
            break

    return out
