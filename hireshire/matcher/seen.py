from __future__ import annotations

import logging
from typing import Optional

from hireshire.matcher.scorer import SCORING_ERROR_SKIP_REASONS
from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)


class SeenStore:
    """Persistent set of job IDs already scored by the matcher, stored in the
    `seen_jobs` table of the shared SQLite database.

    Loads the existing set once, buffers newly-seen IDs in memory, and flushes
    them on `save()` (INSERT OR IGNORE — atomic, no growing JSON file)."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db or get_db()
        # Release jobs retired by a scoring failure before snapshotting the set —
        # afterwards would silently no-op for this run. Installs that ran while the
        # claude_code backend was broken have jobs stuck here that no fix could
        # otherwise reach.
        freed = self._db.forget_seen_scoring_errors(SCORING_ERROR_SKIP_REASONS)
        if freed:
            logger.info("SeenStore: %d job IDs released after earlier scoring errors", freed)
        self._ids: set[str] = self._db.seen_ids()
        self._new: set[str] = set()
        logger.info("SeenStore: %d previously scored job IDs loaded", len(self._ids))

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._ids

    def add(self, job_id: str) -> None:
        if job_id not in self._ids:
            self._ids.add(job_id)
            self._new.add(job_id)

    def save(self) -> None:
        if self._new:
            self._db.mark_seen(self._new)
            logger.info("SeenStore: %d new job IDs persisted", len(self._new))
            self._new.clear()
