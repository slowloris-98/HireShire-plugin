from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)


class ApplyRecord(BaseModel):
    job_id: str
    board_token: str
    title: str
    absolute_url: str
    applied_at: datetime
    status: str  # "dry_run" | "submitted" | "error" | "skipped"
    dry_run: bool
    screenshot: Optional[str] = None
    error: Optional[str] = None


class AppliedStore:
    """Applier records backed by the `applied` table. Screenshots remain on disk
    (referenced by path); `base_dir` is retained only so callers can locate the
    screenshots directory."""

    def __init__(self, base_dir: Path | None = None, db: Optional[Database] = None) -> None:
        if base_dir is not None:
            base_dir.mkdir(parents=True, exist_ok=True)
        self._db = db or get_db()
        self._records: list[ApplyRecord] = [ApplyRecord(**r) for r in self._db.load_applied()]
        self._applied_ids: set[str] = {r.job_id for r in self._records}

    def is_applied(self, job_id: str) -> bool:
        return job_id in self._applied_ids

    def append(self, record: ApplyRecord) -> None:
        self._records.append(record)
        self._applied_ids.add(record.job_id)
        self._db.record_applied(
            record.job_id, record.board_token, record.title, record.absolute_url,
            record.applied_at.isoformat(), record.status, record.dry_run,
            record.screenshot, record.error,
        )
        logger.info("Saved apply record for %s (status=%s)", record.job_id, record.status)

    @property
    def records(self) -> list[ApplyRecord]:
        return list(self._records)
