"""Amazon — amazon.jobs.

`/en/search.json` is the JSON the site's own search page reads, and it answers
plain HTTP. Each record already carries the description and both qualification
lists, so Amazon never defers a detail fetch — the same shape as Apple.

The search refuses an offset past 10,000 hits ("Cannot return more than 10000
results at once"), which `direct_max_pages` keeps it far below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.scope import Scope
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Department, Job, Location

logger = logging.getLogger(__name__)

TOKEN = "amazon"
SCOPE_COLUMN = "amazon"
BASE = "https://www.amazon.jobs"
SEARCH_URL = BASE + "/en/search.json"
PAGE_SIZE = 100


def list_url(scope: Optional[Scope], page: int) -> str:
    """One `normalized_country_code[]` per country in the scope; unscoped, none.

    An unknown code answers zero hits and a 200, which is why the codes come from
    the checked-in table and never from the user's own spelling.
    """
    params = [("result_limit", PAGE_SIZE), ("offset", PAGE_SIZE * (page - 1)),
              ("sort", "recent")]
    codes = scope.for_portal(SCOPE_COLUMN) if scope else None
    params += [("normalized_country_code[]", c) for c in codes or ()]
    return f"{SEARCH_URL}?{urlencode(params)}"


def _parse_posting_date(entry: dict, scraped_at: datetime) -> datetime:
    """`posted_date` ("September 23, 2026"), NOT `updated_time`.

    `updated_time` is a relative age ("13 minutes") that moves on every edit.
    `posted_date` is date-only, so it is read as end-of-day UTC for the same
    reason as Apple's: a same-day posting must not fall outside a 24h cutoff.
    """
    raw = entry.get("posted_date")
    if not raw:
        return scraped_at
    try:
        day = datetime.strptime(raw.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return scraped_at
    return day + timedelta(hours=23, minutes=59)


def _content(entry: dict) -> Optional[str]:
    """Description plus both qualification lists — the requirements the
    cross-encoder and the YoE gate need are in the qualifications, not the prose."""
    sections = [
        ("", entry.get("description")),
        ("Basic qualifications", entry.get("basic_qualifications")),
        ("Preferred qualifications", entry.get("preferred_qualifications")),
    ]
    parts = [f"<p>{h}</p>{body}" if h else body for h, body in sections if body]
    return "<br/>".join(parts) or None


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry.get("id_icims") or entry["id"])
        raw_location = entry.get("normalized_location") or entry.get("location") or ""

        departments: list[Department] = []
        team = entry.get("job_category") or entry.get("business_category")
        if team:
            departments = [Department(id=0, name=str(team))]

        path = entry.get("job_path") or f"/en/jobs/{native_id}"
        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=(entry.get("title") or "").strip(),
            location=Location(name=normalize_location(raw_location) or "N/A"),
            departments=departments,
            absolute_url=BASE + path if path.startswith("/") else path,
            updated_at=_parse_posting_date(entry, scraped_at),
            requisition_id=native_id,
            # The list payload carries the description — nothing to defer.
            content_text=_content(entry),
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Amazon job %s: %s", entry.get("id_icims"), exc)
        return None


def _extract(payload: dict, scraped_at: datetime) -> list[Job]:
    jobs = []
    for entry in payload.get("jobs") or []:
        job = _parse_job(entry, scraped_at)
        if job is not None:
            jobs.append(job)
    return jobs


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for page in range(1, ctx.max_pages + 1):
        response = await ctx.get(list_url(ctx.scope, page))
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Amazon page %d was not JSON", page)
            break
        if payload.get("error"):
            logger.warning("Amazon search refused page %d: %s", page, payload["error"])
            break

        page_jobs = _extract(payload, scraped_at)
        if not page_jobs:
            break

        fresh = [j for j in page_jobs if j.job_id not in seen]
        seen.update(j.job_id for j in fresh)
        out.extend(fresh)

        # Sorted newest-first: once a whole page is past the cutoff, so is the rest.
        if ctx.cutoff and all(j.updated_at < ctx.cutoff for j in page_jobs):
            break

    return out
