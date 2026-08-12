"""Staged-job schema shared by the /scrape-direct skill's two consumers.

The skill writes one JSON array per company to
`<staging_dir>/<run_id>/<company>.json`. Rather than make it emit the full `Job`
schema by hand (nested Location/Department objects, `HttpUrl`, `scraped_at`),
it emits the flat `StagedJob` shape below and this module promotes it to a real
`Job`.

Both `scripts/direct_cli.py ingest` (which writes the DB rows) and
`orchestrate._direct_scrape_stage` (which feeds the same jobs onto the matcher
queue) load through here, so the two can never disagree about what was scraped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from hireshire.models.job import Job, Location

logger = logging.getLogger(__name__)

SOURCE = "direct"


class StagedJob(BaseModel):
    """The flat record the skill writes. Only 4 fields are required."""

    native_id: str          # the portal's own id, unprefixed
    title: str
    url: str
    updated_at: datetime

    location: str = ""
    content_html: Optional[str] = None   # raw HTML or plain text; stripped on promotion
    department: Optional[str] = None
    requisition_id: Optional[str] = None
    detail_fetch_failed: bool = False


def make_job_id(company: str, native_id: str) -> str:
    """Namespace the portal's id.

    `seen_jobs` is keyed on `job_id` alone across every platform and every run
    (db.py), so a bare portal id like Intuit's `23208` would collide with a
    BambooHR job of the same number and be silently dropped as already-seen.
    """
    return f"{SOURCE}:{company}:{native_id}"


def to_job(record: dict, company: str, scraped_at: Optional[datetime] = None) -> Job:
    """Promote one staged record to a `Job`. Raises on invalid input."""
    staged = StagedJob(**record)
    return Job(
        source=SOURCE,
        board_token=company,
        job_id=make_job_id(company, staged.native_id),
        title=staged.title,
        location=Location(name=staged.location or "N/A"),
        absolute_url=staged.url,
        updated_at=staged.updated_at,
        requisition_id=staged.requisition_id,
        # Raw HTML is intentional — Job.strip_html (mode="before") converts it.
        content_text=staged.content_html,
        detail_fetch_failed=staged.detail_fetch_failed,
        scraped_at=scraped_at or datetime.now(timezone.utc),
    )


def staging_path(run_id: str, staging_dir: str | Path = "direct") -> Path:
    return Path(staging_dir) / run_id


def load_staged(
    run_id: str,
    staging_dir: str | Path = "direct",
) -> tuple[dict[str, list[Job]], dict[str, int]]:
    """Read every `<company>.json` staged for `run_id`.

    Returns `(jobs_by_company, malformed_by_company)`. A record that fails
    validation is counted, logged and skipped — one bad row never costs us the
    rest of the company, matching how the API scrapers' `_parse_job` behaves.
    """
    run_dir = staging_path(run_id, staging_dir)
    jobs_by_company: dict[str, list[Job]] = {}
    malformed: dict[str, int] = {}

    if not run_dir.is_dir():
        return jobs_by_company, malformed

    for path in sorted(run_dir.glob("*.json")):
        company = path.stem
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Unreadable staged file %s: %s", path, exc)
            malformed[company] = malformed.get(company, 0) + 1
            continue

        if not isinstance(records, list):
            logger.error("Staged file %s is not a JSON array", path)
            malformed[company] = malformed.get(company, 0) + 1
            continue

        jobs: list[Job] = []
        bad = 0
        for record in records:
            try:
                jobs.append(to_job(record, company))
            except Exception as exc:  # pydantic ValidationError, TypeError, ...
                bad += 1
                logger.warning("Dropping malformed %s record: %s", company, exc)

        # Dedupe within the company — a paginated scrape can re-emit a job when
        # the portal reorders between page fetches. (run_id, job_id) is the jobs
        # PK, so a dupe would be an INSERT OR REPLACE, but the queue would still
        # hand the matcher the same job twice.
        seen: set[str] = set()
        deduped = []
        for job in jobs:
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            deduped.append(job)

        jobs_by_company[company] = deduped
        if bad:
            malformed[company] = bad

    return jobs_by_company, malformed
