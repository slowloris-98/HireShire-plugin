from __future__ import annotations

import logging
from typing import Optional

from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)


class SeenStore:
    """Persistent set of job IDs already scored by the matcher, stored in the
    `seen_jobs` table of the shared SQLite database.

    Loads the existing set once, buffers newly-seen IDs in memory, and flushes
    them on `save()` (INSERT OR IGNORE — atomic, no growing JSON file)."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db or get_db()
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
