"""Intuit — jobs.intuit.com (Radancy TalentBrew).

`/search-jobs/results` returns JSON whose `results` key is a rendered HTML
fragment. It needs the FULL Radancy parameter set: a trimmed query returns
`{"results": "", "hasJobs": true}` — a silent empty result, not an error.

No posting date is exposed, so `updated_at` is the scrape time. Detail is
deferred to the matcher funnel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Job, Location

logger = logging.getLogger(__name__)

TOKEN = "intuit"
BASE = "https://jobs.intuit.com"

# Every parameter here is load-bearing. Dropping the module-name or pagination
# params yields an empty `results` string with a 200 status.
LIST_URL = (
    BASE + "/search-jobs/results"
    "?ActiveFacetID=0&CurrentPage={page}&RecordsPerPage=15"
    "&Distance=50&RadiusUnitType=0&Keywords=&Location=&ShowRadius=False"
    "&IsPagination=True&SearchResultsModuleName=Search+Results"
    "&SearchFiltersModuleName=Search+Filters"
    "&SortCriteria=1&SortDirection=1&SearchType=5&ResultsType=0"
)
PAGE_SIZE = 15


def _parse_job(anchor, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = anchor.get("data-job-id")
        href = anchor.get("href") or ""
        if not native_id or not href:
            return None

        loc_el = anchor.select_one("span.job-location")
        location = normalize_location(loc_el.get_text(strip=True) if loc_el else "")

        title = anchor.get("data-title") or anchor.get_text(strip=True)

        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=title,
            location=Location(name=location or "N/A"),
            absolute_url=BASE + href if href.startswith("/") else href,
            updated_at=scraped_at,   # portal exposes no posting date
            detail_path=href,
            scraped_at=scraped_at,
        )
    except (ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Intuit job: %s", exc)
        return None


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for page in range(1, ctx.max_pages + 1):
        response = await ctx.get(
            LIST_URL.format(page=page),
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Intuit page %d was not JSON", page)
            break

        soup = BeautifulSoup(payload.get("results") or "", "lxml")
        anchors = soup.select("a.sr-item[data-job-id]")
        if not anchors:
            break

        new = 0
        for anchor in anchors:
            job = _parse_job(anchor, scraped_at)
            if job is None or job.job_id in seen:
                continue
            seen.add(job.job_id)
            out.append(job)
            new += 1

        if new == 0:
            break

    return out


async def fetch_detail(ctx, job: Job) -> Job:
    try:
        response = await ctx.get(str(job.absolute_url))
        soup = BeautifulSoup(response.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body = soup.select_one(".job-description") or soup.find("main") or soup
        text = body.get_text(" ", strip=True)
        if not text or len(text) < 300:
            raise ValueError("detail page yielded no usable text")
    except Exception as exc:
        logger.warning("Detail hydrate failed for Intuit job %s: %s", job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "content_text": text,
        "detail_fetch_failed": False,
    })
