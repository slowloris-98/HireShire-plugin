"""Eightfold AI career sites — one platform, several single-tenant portals.

Microsoft, Lumen and Qualcomm all run Eightfold, and all three answer the same
two endpoints over plain HTTP with no session:

- `/api/pcsx/search` lists positions. It is a different endpoint from
  `/api/apply/v2/jobs`, which answers `403 {"message": "Not authorized for
  PCSX"}` on every tenant probed — that 403 is why Microsoft was once scraped
  through a browser.
- `/api/pcsx/position_details` returns one position's description.

Each tenant module (`microsoft.py`, `lumen.py`, `qualcomm.py`) binds a `Tenant`
and exposes the ordinary handler interface. Two platform facts shape the walk:

- **The page size is fixed at 10.** A `num=` parameter is ignored, so the walk
  takes twice `direct_max_pages` pages to cover about as many jobs as Apple or
  Google do with 20 per page.
- **`location=` takes one country.** A second `location=` is silently ignored,
  and so is the `India OR United States` syntax the sites' own search pages put
  in their URLs: measured on Lumen, `United States` → 113, `India` → 14, and
  `India OR United States` → 113. A scope of several countries is therefore
  walked as one series per country, deduped.

`location=` is free text geocoded by the platform, not by the tenant, so every
tenant shares one scope column (`Country.eightfold`).

The list carries the posting time but not the description; `fetch_detail` reads
it from `position_details` when the matcher funnel asks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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

SCOPE_COLUMN = "eightfold"
PAGE_SIZE = 10
# Pages walked per country for each page of `direct_max_pages`, which is sized
# for the 20-job pages of Apple and Google.
PAGES_PER_UNIT = 2


@dataclass(frozen=True)
class Tenant:
    token: str     # board_token, and the prefix of every job_id
    label: str     # for log lines
    base: str      # https://<host>, no trailing slash
    domain: str    # the `domain=` every request carries

    @property
    def search_url(self) -> str:
        return self.base + "/api/pcsx/search"

    @property
    def detail_url(self) -> str:
        return self.base + "/api/pcsx/position_details"

    def job_url(self, native_id: str) -> str:
        return f"{self.base}/careers/job/{native_id}"


def list_url(tenant: Tenant, country: Optional[str], page: int) -> str:
    params = {
        "domain": tenant.domain,
        "query": "",
        "location": country or "",
        "start": PAGE_SIZE * (page - 1),
        "sort_by": "timestamp",
    }
    return f"{tenant.search_url}?{urlencode(params)}"


def countries(scope: Optional[Scope]) -> list[Optional[str]]:
    """The `location=` values to walk; `[None]` searches unscoped."""
    names = scope.for_portal(SCOPE_COLUMN) if scope else None
    return list(names) if names else [None]


def _posted(entry: dict, scraped_at: datetime) -> datetime:
    ts = entry.get("postedTs") or entry.get("creationTs")
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return scraped_at


def parse_job(tenant: Tenant, entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry["id"])
        # `locations` is the long form ("United States, Washington, Redmond",
        # "Remote, India"), which names the country. `standardizedLocations`
        # shortens it to "Redmond, WA, US", sometimes to a bare "US" no country
        # inference can read — and on Lumen it renders "Remote" as Oregon
        # ("Remote, OR, US"). So it is only a fallback.
        raw = " | ".join(entry.get("locations") or entry.get("standardizedLocations") or [])

        departments: list[Department] = []
        if entry.get("department"):
            departments = [Department(id=0, name=entry["department"])]

        return Job(
            source=SOURCE,
            board_token=tenant.token,
            job_id=make_job_id(tenant.token, native_id),
            title=(entry.get("name") or "").strip(),
            location=Location(name=normalize_location(raw) or "N/A"),
            departments=departments,
            absolute_url=tenant.job_url(native_id),
            updated_at=_posted(entry, scraped_at),
            requisition_id=entry.get("displayJobId") or entry.get("atsJobId"),
            detail_path=native_id,
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse %s job %s: %s", tenant.label, entry.get("id"), exc)
        return None


def _positions(response) -> Optional[list[dict]]:
    """The page's positions, or None when the answer was not the search JSON."""
    try:
        return response.json()["data"]["positions"]
    except (ValueError, KeyError, TypeError):
        return None


async def fetch_list(tenant: Tenant, ctx) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for country in countries(ctx.scope):
        for page in range(1, ctx.max_pages * PAGES_PER_UNIT + 1):
            response = await ctx.get(list_url(tenant, country, page))
            positions = _positions(response)
            if positions is None:
                logger.warning("%s page %d (%s) was not search JSON",
                               tenant.label, page, country or "everywhere")
                break
            page_jobs = [j for j in (parse_job(tenant, e, scraped_at) for e in positions) if j]
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


async def fetch_detail(tenant: Tenant, ctx, job: Job) -> Job:
    native_id = job.detail_path or job.job_id.rsplit(":", 1)[-1]
    try:
        params = {"position_id": native_id, "domain": tenant.domain, "hl": "en"}
        response = await ctx.get(f"{tenant.detail_url}?{urlencode(params)}")
        html = (response.json().get("data") or {}).get("jobDescription") or ""
        # Job.content_text strips the HTML; measure the text, not the markup.
        if len(BeautifulSoup(html, "lxml").get_text(" ", strip=True)) < 300:
            raise ValueError("position_details yielded no usable description")
    except Exception as exc:
        logger.warning("Detail hydrate failed for %s job %s: %s", tenant.label, job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "content_text": html,
        "detail_fetch_failed": False,
    })
