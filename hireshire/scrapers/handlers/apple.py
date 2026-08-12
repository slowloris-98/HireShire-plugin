"""Apple — jobs.apple.com.

Apple's search page is a React app, but the full result set for the page is
embedded in the served HTML as `window.__staticRouterHydrationData`, so plain
HTTP is enough — no browser. Each record already carries `jobSummary`, so Apple
never defers a detail fetch.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Department, Job, Location

logger = logging.getLogger(__name__)

TOKEN = "apple"
LIST_URL = (
    "https://jobs.apple.com/en-us/search"
    "?location=united-states-USA+india-INDC&sort=newest&page={page}"
)
DETAIL_URL = "https://jobs.apple.com/en-us/details/{native_id}/{slug}"
PAGE_SIZE = 20

_HYDRATION = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((".*?")\);', re.S
)


def _walk(obj: Any) -> Iterator[dict]:
    """Yield every dict carrying a positionId, wherever it sits in the tree."""
    if isinstance(obj, dict):
        if "positionId" in obj:
            yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _parse_posting_date(entry: dict, scraped_at: datetime) -> datetime:
    """Use `postingDate`, NOT `postDateInGMT`.

    `postDateInGMT` comes back with nanosecond precision equal to the moment of
    the request — it is the server's "now", not the posting time, so every job
    would look brand new. `postingDate` ("Aug 06, 2026") is the real one; it is
    date-only, so treat it as end-of-day UTC or same-window postings fall
    outside a 24h cutoff by a few hours.
    """
    raw = entry.get("postingDate")
    if not raw:
        return scraped_at
    try:
        day = datetime.strptime(raw, "%b %d, %Y").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return scraped_at
    return day + timedelta(hours=23, minutes=59)


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry["positionId"])
        slug = entry.get("transformedPostingTitle") or ""

        parts = []
        for loc in entry.get("locations") or []:
            bit = ", ".join(
                p for p in (loc.get("city"), loc.get("stateProvince"), loc.get("countryName")) if p
            )
            if bit:
                parts.append(bit)
        location = normalize_location(" | ".join(parts))

        departments: list[Department] = []
        team = entry.get("team") or {}
        if team.get("teamName"):
            departments = [Department(id=0, name=team["teamName"])]

        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=entry.get("postingTitle") or "",
            location=Location(name=location or "N/A"),
            departments=departments,
            absolute_url=DETAIL_URL.format(native_id=native_id, slug=slug),
            updated_at=_parse_posting_date(entry, scraped_at),
            requisition_id=entry.get("reqId"),
            # The list payload carries the description — nothing to defer.
            content_text=entry.get("jobSummary"),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Apple job %s: %s", entry.get("positionId"), exc)
        return None


def _extract(html: str, scraped_at: datetime) -> list[Job]:
    m = _HYDRATION.search(html)
    if not m:
        return []
    try:
        data = json.loads(json.loads(m.group(1)))
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Apple hydration blob did not parse: %s", exc)
        return []

    jobs, seen = [], set()
    for entry in _walk(data):
        native_id = str(entry.get("positionId") or "")
        if not native_id or native_id in seen:
            continue
        seen.add(native_id)
        job = _parse_job(entry, scraped_at)
        if job is not None:
            jobs.append(job)
    return jobs


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for page in range(1, ctx.max_pages + 1):
        response = await ctx.get(LIST_URL.format(page=page))
        page_jobs = _extract(response.text, scraped_at)
        if not page_jobs:
            break

        fresh = [j for j in page_jobs if j.job_id not in seen]
        seen.update(j.job_id for j in fresh)
        out.extend(fresh)

        # Sorted newest-first: once a whole page is past the cutoff, so is the rest.
        if ctx.cutoff and all(j.updated_at < ctx.cutoff for j in page_jobs):
            break

    return out
