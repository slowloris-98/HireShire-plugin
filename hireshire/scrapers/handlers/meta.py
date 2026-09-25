"""Meta — metacareers.com.

The job list is one GraphQL query, `CareersJobSearchResultsV2DataQuery`, and it
returns **the whole board in one response** (~1,000 jobs) — no pagination. It
answers plain HTTP once two conditions hold, and failing either is what made Meta
look browser-only ("400 on every request"):

- the request carries a browser's fetch metadata (`Sec-Fetch-*`, `Origin`,
  `Referer`); without it both the page and the API answer 400;
- it carries the page's `lsd` token, in the form and as `X-FB-LSD`, so the
  search page is fetched first to read it.

**`DOC_ID` is checked in, never discovered.** It names the persisted query and
is not in the search page or its eagerly loaded bundles, so a sweep has nowhere
to read it from. Meta rotates it on deploys. A stale id answers **404**, which
`raise_for_status` turns into an ordinary error row — loud, not an empty board.
To refresh it: open https://www.metacareers.com/jobsearch/ in a browser, find
the `graphql` request whose `fb_api_req_friendly_name` is
`CareersJobSearchResultsV2DataQuery`, and copy its `doc_id`.

**Not scoped server-side** (`SCOPE_COLUMN = None`). The whole board is one
response, so a scope would save nothing, and the query's `offices` filter takes
exact office names — a wrong one returns an empty list with a 200, the same
silent-empty hazard the portal location table exists to avoid. `scraper.py`'s
location filter does the work instead; Meta's "Menlo Park, CA" and
"Bengaluru, India" both resolve through `normalize_location`.

The list has no date and no description. `fetch_detail` reads both from the job
page's JSON-LD; `updated_at` is the scrape time until then, as for Google.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Department, Job, Location

logger = logging.getLogger(__name__)

TOKEN = "meta"
SCOPE_COLUMN = None
BASE = "https://www.metacareers.com"
SEARCH_PAGE = BASE + "/jobsearch/?sort_by_new=true"
GRAPHQL = BASE + "/graphql"
JOB_URL = BASE + "/jobs/{native_id}/"

QUERY_NAME = "CareersJobSearchResultsV2DataQuery"
DOC_ID = "27129360303422352"   # verified 2026-09-23; see the module docstring

_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')

_NAVIGATE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
_FETCH = {
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": BASE,
    "Referer": SEARCH_PAGE,
    "X-FB-Friendly-Name": QUERY_NAME,
}

_SEARCH_INPUT = {
    "q": None, "divisions": [], "offices": [], "roles": [], "leadership_levels": [],
    "saved_jobs": [], "saved_searches": [], "sub_teams": [], "teams": [],
    "is_leadership": False, "is_remote_only": False, "sort_by_new": True,
    "results_per_page": None,
}


class MetaSearchError(RuntimeError):
    """The search answered, but not with a job list. Raised, never swallowed:
    an empty return would read as "Meta has no jobs today"."""


def lsd_token(html: str) -> Optional[str]:
    m = _LSD.search(html)
    return m.group(1) if m else None


def search_form(lsd: str) -> dict[str, str]:
    variables = {"search_input": _SEARCH_INPUT, "viewasUserID": None, "isLoggedIn": False}
    return {
        "av": "0", "__user": "0", "__a": "1", "__comet_req": "31",
        "lsd": lsd,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": QUERY_NAME,
        "variables": json.dumps(variables, separators=(",", ":")),
        "server_timestamps": "true",
        "doc_id": DOC_ID,
    }


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    try:
        native_id = str(entry["id"])
        # Titles arrive with a hard line break: "Research Scientist,\nMachine Learning".
        title = " ".join((entry.get("title") or "").split())
        raw = " | ".join(entry.get("locations") or [])
        teams = entry.get("teams") or []
        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=title,
            location=Location(name=normalize_location(raw) or "N/A"),
            departments=[Department(id=0, name=teams[0])] if teams else [],
            absolute_url=JOB_URL.format(native_id=native_id),
            updated_at=scraped_at,   # the list exposes no posting date
            detail_path=native_id,
            scraped_at=scraped_at,
        )
    except (KeyError, ValidationError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse Meta job %s: %s", entry.get("id"), exc)
        return None


def extract(payload: dict, scraped_at: datetime) -> list[Job]:
    """Every job in the response, featured ones included, once each."""
    try:
        result = payload["data"]["job_search_with_featured_jobs_v2"]
        entries = list(result["all_jobs"]) + list(result.get("featured_jobs") or [])
    except (KeyError, TypeError) as exc:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        raise MetaSearchError(f"no job list in the search response: {errors or exc}") from exc

    jobs, seen = [], set()
    for entry in entries:
        job = _parse_job(entry, scraped_at)
        if job is not None and job.job_id not in seen:
            seen.add(job.job_id)
            jobs.append(job)
    return jobs


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    page = await ctx.get(SEARCH_PAGE, headers=_NAVIGATE)
    lsd = lsd_token(page.text)
    if not lsd:
        raise MetaSearchError("the search page carried no lsd token")

    response = await ctx.post(GRAPHQL, data=search_form(lsd),
                              headers={**_FETCH, "X-FB-LSD": lsd})
    try:
        payload = response.json()
    except ValueError as exc:
        raise MetaSearchError("the search response was not JSON") from exc
    return extract(payload, scraped_at)


def _job_posting(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


async def fetch_detail(ctx, job: Job) -> Job:
    """Description and posting date from the job page's JSON-LD `JobPosting`."""
    try:
        response = await ctx.get(str(job.absolute_url), headers=_NAVIGATE)
        posting = _job_posting(response.text) or {}
        # The requirements the YoE gate reads sit under "qualifications", so all
        # three sections go in, headed, not just the description.
        sections = (("", "description"), ("Responsibilities", "responsibilities"),
                    ("Qualifications", "qualifications"))
        html = "<br/>".join(
            f"<p>{head}</p>{posting[key]}" if head else posting[key]
            for head, key in sections if isinstance(posting.get(key), str) and posting[key]
        )
        if len(BeautifulSoup(html, "lxml").get_text(" ", strip=True)) < 300:
            raise ValueError("job page carried no usable JobPosting")
        updated_at = job.updated_at
        if posting.get("datePosted"):
            updated_at = datetime.fromisoformat(posting["datePosted"])
    except Exception as exc:
        logger.warning("Detail hydrate failed for Meta job %s: %s", job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "content_text": html,
        "updated_at": updated_at,
        "detail_fetch_failed": False,
    })
