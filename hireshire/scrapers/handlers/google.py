"""Google — google.com/about/careers.

The results page ships no usable `<a>` tags: the job anchors are created during
hydration, so a DOM parse of the served HTML finds nothing. The ids and slugs
*are* in the markup though, so we regex `jobs/results/<id>-<slug>` out of it.

The slug doubles as a serviceable title for the matcher's title gate; the real
title, location and description come from the detail page, which IS fully
server-rendered (qualifications, responsibilities and all). Detail is deferred
to the matcher funnel so only jobs that survive the gate cost a request.

The list exposes no posting date, so `updated_at` is the scrape time and the age
cutoff never fires — `seen_jobs` is what stops reprocessing across runs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Job, Location

logger = logging.getLogger(__name__)

TOKEN = "google"
BASE = "https://www.google.com/about/careers/applications"
LIST_URL = (
    BASE + "/jobs/results"
    "?location=United%20States&location=India&sort_by=date&page={page}"
)
DETAIL_URL = BASE + "/jobs/results/{native_id}-{slug}"
PAGE_SIZE = 20

_RESULT = re.compile(r"jobs/results/(\d+)-([a-z0-9-]+)")

# The list gives no per-job location, but LIST_URL constrains the search
# server-side, so every returned job IS in one of these. Without this,
# scraper.py's location filter sees "N/A" and drops the entire board before the
# funnel ever gets a chance to fetch the real location.
#
# COUPLED TO LIST_URL: if you change the `location=` parameters above, change
# this to match, or the client-side filter will disagree with the server.
SEARCH_SCOPE = "United States | India"


def _title_from_slug(slug: str) -> str:
    """'software-engineer-iii-performance' -> 'Software Engineer Iii Performance'.

    Only used until the detail fetch supplies the real title. Good enough for the
    matcher's substring title gate, which is what runs before hydration.
    """
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def _parse_job(native_id: str, slug: str, scraped_at: datetime) -> Optional[Job]:
    try:
        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=_title_from_slug(slug),
            # Refined to the precise city by fetch_detail during funnel hydration.
            location=Location(name=SEARCH_SCOPE),
            absolute_url=DETAIL_URL.format(native_id=native_id, slug=slug),
            updated_at=scraped_at,   # portal exposes no posting date
            # Deferred to the funnel; detail_path carries the slug so the URL
            # can be rebuilt without re-deriving it.
            detail_path=slug,
            scraped_at=scraped_at,
        )
    except (ValidationError, TypeError) as exc:
        logger.warning("Failed to build Google job %s: %s", native_id, exc)
        return None


async def fetch_list(ctx, token: str) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    out: list[Job] = []
    seen: set[str] = set()

    for page in range(1, ctx.max_pages + 1):
        response = await ctx.get(LIST_URL.format(page=page))
        pairs = list(dict.fromkeys(_RESULT.findall(response.text)))
        if not pairs:
            break

        new = 0
        for native_id, slug in pairs:
            job_id = make_job_id(TOKEN, native_id)
            if job_id in seen:
                continue
            seen.add(job_id)
            job = _parse_job(native_id, slug, scraped_at)
            if job is not None:
                out.append(job)
                new += 1

        if new == 0:  # pagination exhausted / looping on the same page
            break

    return out


async def fetch_detail(ctx, job: Job) -> Job:
    """Hydrate title, location and description from the server-rendered page."""
    try:
        response = await ctx.get(str(job.absolute_url))
        soup = BeautifulSoup(response.text, "lxml")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        body = soup.find("main") or soup
        text = body.get_text(" ", strip=True)

        # "<title>Software Engineer III, Performance — Google Careers</title>"
        title = job.title
        if soup.title and soup.title.string:
            cleaned = re.split(r"\s+[—-]\s+Google Careers", soup.title.string.strip())[0]
            if cleaned:
                title = cleaned

        location = job.location.name
        lm = re.search(r"place\s+(.+?)\s+(?:bar_chart|Minimum qualifications)", text)
        if lm:
            location = normalize_location(lm.group(1).strip())

        if not text or len(text) < 300:
            raise ValueError("detail page yielded no usable text")
    except Exception as exc:
        logger.warning("Detail hydrate failed for Google job %s: %s", job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "title": title,
        "location": {"name": location},
        "content_text": text,
        "detail_fetch_failed": False,
    })
