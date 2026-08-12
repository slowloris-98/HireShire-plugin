from __future__ import annotations

import logging
from typing import Optional

from hireshire.models.job import Job
from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)


def load_jobs(run_id: str, db: Optional[Database] = None) -> list[Job]:
    """Load all jobs for a scraper run from the database (unique by job_id)."""
    jobs = (db or get_db()).load_jobs(run_id)
    logger.info("Loaded %d unique jobs from run %s", len(jobs), run_id)
    return jobs
