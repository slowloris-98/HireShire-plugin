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
from urllib.parse import quote

from bs4 import BeautifulSoup
from pydantic import ValidationError

from hireshire.direct.locations import normalize_location
from hireshire.direct.scope import EVERYWHERE, Scope
from hireshire.direct.staging import SOURCE, make_job_id
from hireshire.models.job import Job, Location

logger = logging.getLogger(__name__)

TOKEN = "google"
SCOPE_COLUMN = "google"
BASE = "https://www.google.com/about/careers/applications"
DETAIL_URL = BASE + "/jobs/results/{native_id}-{slug}"
PAGE_SIZE = 20

_RESULT = re.compile(r"jobs/results/(\d+)-([a-z0-9-]+)")


def list_url(scope: Optional[Scope], page: int) -> str:
    """One `location=` per country in the scope; unscoped, none at all."""
    names = scope.for_portal(SCOPE_COLUMN) if scope else None
    location = "".join(f"location={quote(n)}&" for n in names or ())
    return f"{BASE}/jobs/results?{location}sort_by=date&page={page}"


def search_scope(scope: Optional[Scope]) -> str:
    """The location a list-only job carries until `fetch_detail` finds the real one.

    The list gives no per-job location, but `list_url` constrains the search
    server-side, so every returned job IS somewhere in the scope. It is derived
    from the same scope as the URL so the two cannot drift, and the job is marked
    `location_is_placeholder` so `scraper.py`'s location filter lets it through:
    a filter of city or state terms alone never contains a country name, and
    would otherwise drop the entire board before the funnel could hydrate it.
    """
    return (scope or EVERYWHERE).placeholder(SCOPE_COLUMN)


def _title_from_slug(slug: str) -> str:
    """'software-engineer-iii-performance' -> 'Software Engineer Iii Performance'.

    Only used until the detail fetch supplies the real title. Good enough for the
    matcher's keyword title gate, which is what runs before hydration — the slug's
    hyphens become spaces, so whole-word keywords still match.
    """
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def _parse_job(native_id: str, slug: str, scraped_at: datetime,
               scope: Optional[Scope] = None) -> Optional[Job]:
    try:
        return Job(
            source=SOURCE,
            board_token=TOKEN,
            job_id=make_job_id(TOKEN, native_id),
            title=_title_from_slug(slug),
            # Refined to the precise city by fetch_detail during funnel hydration.
            location=Location(name=search_scope(scope)),
            location_is_placeholder=True,
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
        response = await ctx.get(list_url(ctx.scope, page))
        pairs = list(dict.fromkeys(_RESULT.findall(response.text)))
        if not pairs:
            break

        new = 0
        for native_id, slug in pairs:
            job_id = make_job_id(TOKEN, native_id)
            if job_id in seen:
                continue
            seen.add(job_id)
            job = _parse_job(native_id, slug, scraped_at, ctx.scope)
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
        placeholder = job.location_is_placeholder
        lm = re.search(r"place\s+(.+?)\s+(?:bar_chart|Minimum qualifications)", text)
        if lm:
            location = normalize_location(lm.group(1).strip())
            placeholder = False

        if not text or len(text) < 300:
            raise ValueError("detail page yielded no usable text")
    except Exception as exc:
        logger.warning("Detail hydrate failed for Google job %s: %s", job.job_id, exc)
        return job.model_validate({**job.model_dump(), "detail_fetch_failed": True})

    return job.model_validate({
        **job.model_dump(),
        "title": title,
        "location": {"name": location},
        "location_is_placeholder": placeholder,
        "content_text": text,
        "detail_fetch_failed": False,
    })
