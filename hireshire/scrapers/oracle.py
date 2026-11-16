"""Oracle Recruiting Cloud — the "Candidate Experience" career sites.

American Express, Oracle, BDO, Pella, Estes and many more run their careers on
Oracle HCM. Each employer has its own **pod**, `<pod>.fa.<region>.oraclecloud.com`,
and the pod carries one or more candidate sites. The employer's own careers domain
(`careers.americanexpress.com`) is only a front: it answers the API with a redirect
to a 404 page, so every request here goes to the pod. The rows come from
`config/oracle_companies.json` (`hireshire/oracle_tenants.py`).

Facts measured against live pods, each of which shapes the code:

- **The list is a public JSON resource**, `recruitingCEJobRequisitions` with a
  `findReqs` finder. No login, no session, no browser.
- **`limit` caps at 200.** Asking for 500 returns 200, so pages are 200.
- **`POSTING_DATES_DESC` is strictly newest-first** (zero out-of-order steps over
  200 jobs on two pods), so the walk stops on the first whole page dated before
  the cutoff, like Workday's.
- **`PostedDate` is a date with no time.** It is read as the END of that day
  (capped at the scrape), so the scraper's instant-based age gate can only err
  towards keeping a job — see `_posted` for the job a midnight reading would lose
  for good. The walk's stop compares days, never times, for the same reason. The
  detail call replaces it with the exact `ExternalPostedStartDate`.
- **The list carries no description** (`ShortDescriptionStr` is empty), so this is
  a list->detail board: `fetch_detail` reads `recruitingCEJobRequisitionDetails`,
  whose `ExternalDescriptionStr` is the posting. The detail also names the
  secondary locations the list omits; they are added to the location then.
- **An unknown site number is not an error.** The pod answers it with its default
  site's jobs — measured on BDO, `CX`, `CX_xx` and `jobsearch` all returned the
  same 675 — which is why a pod's sites are deduped by requisition id here and
  pruned to genuinely distinct ones by the discovery script.
- **Requisition ids are small integers** (`14715`) that repeat between employers,
  so `job_id` is namespaced by token, as the direct portals do.

There is no location scope: the list is read newest-first to the cutoff and the
scraper's location gate narrows it afterwards, as for Meta and Rivian. The gate
sees only the primary location at that point.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire import oracle_tenants
from hireshire.http_client import make_retry_decorator
from hireshire.models.job import Job, Location
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers.base import AbstractScraper
from hireshire.scrapers.exceptions import SlugNotFoundError

logger = logging.getLogger(__name__)

SOURCE = "oracle"
PAGE_SIZE = 200
# A ceiling on pages per site when there is no cutoff (a first sweep with no age
# limit): 25 x 200 covers every pod measured, the largest at 5,240 jobs.
MAX_PAGES = 25
_API = "/hcmRestApi/resources/latest"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def make_job_id(token: str, native_id: str) -> str:
    return f"{SOURCE}:{token}:{native_id}"


def list_url(host: str, site: str, offset: int, limit: int = PAGE_SIZE) -> str:
    finder = f"findReqs;siteNumber={site},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
    return (f"https://{host}{_API}/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList&finder={quote(finder, safe=';,=')}")


def detail_url(host: str, site: str, native_id: str) -> str:
    finder = f'ById;Id="{native_id}",siteNumber={site}'
    return (f"https://{host}{_API}/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder={quote(finder, safe=';,=')}")


def job_url(host: str, site: str, native_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{native_id}"


def _posted(value, scraped_at: datetime) -> datetime:
    """The END of the list's posting day, never later than the scrape.

    The list gives a date with no time, and `scraper.py` compares instants
    (`updated_at >= cutoff`). Read as midnight, a job posted at 22:00 UTC and first
    seen by the 01:00 sweep would sit before that sweep's cutoff and be dropped —
    by that sweep and every later one, so it would never be seen at all. Read as
    the day's end, the gate can only err towards keeping a job, by under a day.
    `fetch_detail` replaces this with the exact `ExternalPostedStartDate`."""
    try:
        day = datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return scraped_at
    return min(day + timedelta(days=1) - timedelta(seconds=1), scraped_at)


def _exact_posted(value) -> Optional[datetime]:
    try:
        posted = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)


def parse_job(token: str, host: str, site: str, entry: dict,
              scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry["Id"])
        return Job(
            source=SOURCE,
            board_token=token,
            job_id=make_job_id(token, native_id),
            title=(entry.get("Title") or "").strip(),
            location=Location(name=(entry.get("PrimaryLocation") or "").strip() or "N/A"),
            absolute_url=job_url(host, site, native_id),
            updated_at=_posted(entry.get("PostedDate"), scraped_at),
            requisition_id=native_id,
            detail_path=f"{host}|{site}|{native_id}",
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Oracle job %s from %s: %s", entry.get("Id"), token, exc)
        return None


def _requisitions(response) -> Optional[list[dict]]:
    """The page's requisitions, or None when the answer was not the list JSON."""
    try:
        items = response.json()["items"]
        return (items[0].get("requisitionList") or []) if items else []
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


class OracleScraper(AbstractScraper):
    source = SOURCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        retry_attempts: int = 3,
        cutoff: Optional[datetime] = None,
        tenants: Optional[dict[str, dict]] = None,
    ):
        self._client = client
        self._limiter = limiter
        self._retry = make_retry_decorator(retry_attempts)
        self.cutoff = cutoff
        self._tenants = tenants if tenants is not None else oracle_tenants.by_token()

    async def fetch_all(self, board_token: str) -> list[Job]:
        tenant = self._tenants.get(board_token)
        if tenant is None:
            # Not in the shipped list. Deliberately not SlugNotFoundError: nothing
            # was asked, so there is no verdict about the employer to record.
            logger.warning("No Oracle employer '%s' in %s — skipping",
                           board_token, oracle_tenants.FILENAME)
            return []

        scraped_at = datetime.now(timezone.utc)
        jobs: list[Job] = []
        seen: set[str] = set()
        for site in tenant["sites"]:
            for page in range(MAX_PAGES):
                try:
                    response = await self._get(list_url(tenant["host"], site, page * PAGE_SIZE))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        raise SlugNotFoundError(SOURCE, board_token) from exc
                    raise
                entries = _requisitions(response)
                if entries is None:
                    logger.warning("Oracle %s site %s page %d was not list JSON",
                                   board_token, site, page)
                    break
                page_jobs = [j for j in (parse_job(board_token, tenant["host"], site, e, scraped_at)
                                         for e in entries if isinstance(e, dict)) if j]
                fresh = [j for j in page_jobs if j.job_id not in seen]
                seen.update(j.job_id for j in fresh)
                jobs.extend(fresh)
                if len(entries) < PAGE_SIZE:
                    break
                # Days, not times: PostedDate has no time, so comparing midnight to
                # the cutoff's instant would stop before the cutoff's own day.
                if self.cutoff and all(j.updated_at.date() < self.cutoff.date() for j in page_jobs):
                    break
        return jobs

    async def fetch_detail(self, job: Job) -> Job:
        try:
            host, site, native_id = (job.detail_path or "").split("|", 2)
            response = await self._get(detail_url(host, site, native_id))
            item = (response.json().get("items") or [{}])[0]
            parts = [item.get(k) or "" for k in
                     ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                      "ExternalQualificationsStr")]
            html = "\n".join(p for p in parts if p)
            # Job.content_text strips the HTML; measure the text, not the markup.
            if len(BeautifulSoup(html, "lxml").get_text(" ", strip=True)) < 300:
                raise ValueError("requisition details yielded no usable description")
        except Exception as exc:
            logger.warning("Detail hydrate failed for Oracle job %s: %s", job.job_id, exc)
            return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

        location = job.location.name
        extra = [s.get("Name") for s in (item.get("secondaryLocations") or [])
                 if isinstance(s, dict) and s.get("Name")]
        if extra:
            location = " | ".join(dict.fromkeys([location, *extra]))
        return job.model_validate({
            **job.model_dump(),
            "content_text": html,
            "location": {**job.location.model_dump(), "name": location},
            "updated_at": _exact_posted(item.get("ExternalPostedStartDate")) or job.updated_at,
            "detail_fetch_failed": False,
        })

    async def _get(self, url: str) -> httpx.Response:
        @self._retry
        async def _do_get():
            async with self._limiter:
                response = await self._client.get(url, headers=_HEADERS)
                response.raise_for_status()
                return response

        return await _do_get()
