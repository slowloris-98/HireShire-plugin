"""Scraper persistence — writes scraped jobs into the shared SQLite database.

`RunStore` is a thin async facade over `hireshire.storage.db`: each company's
jobs are batch-inserted in one transaction, and every company (including
zero-job and errored ones) gets one cheap `run_companies` row. A company that
fetches successfully with zero jobs therefore produces a single metadata row and
NO `jobs` rows — no per-company file, no `[]` payload. Blocking DB writes are
offloaded with `asyncio.to_thread` so the scraper's event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from hireshire.models.job import Job
from hireshire.storage.db import PHASE_SCRAPE, Database, get_db

logger = logging.getLogger(__name__)


class RunStore:
    def __init__(self, run_id: str, db: Optional[Database] = None) -> None:
        self.run_id = run_id
        self._db = db or get_db()
        self._companies = 0
        self._companies_with_jobs = 0
        self._total_jobs = 0
        self._errors = 0

    async def save_company(
        self,
        board_token: str,
        jobs: list[Job],
        platform: Optional[str] = None,
        fetch_time_s: Optional[float] = None,
    ) -> None:
        self._companies += 1
        self._total_jobs += len(jobs)
        if jobs:
            self._companies_with_jobs += 1
        await asyncio.to_thread(
            self._db.record_company, self.run_id, board_token, platform,
            "ok", len(jobs), fetch_time_s, None,
        )
        if jobs:  # zero-job companies get a metadata row only — no job rows written
            await asyncio.to_thread(self._db.insert_jobs, self.run_id, jobs)
            logger.info("Saved %d jobs for %s", len(jobs), board_token)

    async def record_error(
        self,
        board_token: str,
        status: str,
        error: str,
        platform: Optional[str] = None,
        fetch_time_s: Optional[float] = None,
    ) -> None:
        self._companies += 1
        self._errors += 1
        await asyncio.to_thread(
            self._db.record_company, self.run_id, board_token, platform,
            status, 0, fetch_time_s, error,
        )

    async def finalise_run(self, started_at: datetime, stats: Optional[dict] = None) -> None:
        merged = {
            "total_jobs": self._total_jobs,
            "companies": self._companies,
            "companies_with_jobs": self._companies_with_jobs,
            "errors": self._errors,
        }
        if stats:
            merged.update(stats)
        await asyncio.to_thread(
            self._db.finalise_run, self.run_id, PHASE_SCRAPE, started_at.isoformat(), None, merged,
        )
        logger.info(
            "Scrape run finalised: %d jobs across %d companies",
            self._total_jobs, self._companies,
        )

    @staticmethod
    def latest_run(db: Optional[Database] = None) -> Optional[str]:
        return (db or get_db()).latest_run(PHASE_SCRAPE)
