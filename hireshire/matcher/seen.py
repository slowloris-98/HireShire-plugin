from __future__ import annotations

import logging
from typing import Optional

from hireshire.matcher.scorer import SCORING_ERROR_SKIP_REASONS
from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)


class SeenStore:
    """Persistent set of postings the matcher has already reached a verdict on.

    Loads the existing set once, buffers newly-retired postings in memory, and
    flushes them on `save()`. Retirement lives in `postings.retired_at`; it used to
    be a `seen_jobs` table of its own.

    **Keyed on `(board_token, job_id)`, not the bare id**, which is the whole reason
    this class changed shape. `job_id` is only unique per board — Greenhouse and
    BambooHR both mint bare integers, and Workday falls back to the job *title* — so
    an id-keyed set let one employer's verdict retire another employer's posting
    permanently, and nothing could bring it back. `Job` objects and `MatchResult`s
    both carry `board_token`, so every caller has the pair to hand.

    Membership is tested with a `Job` or a `MatchResult` rather than with a key, so
    no caller has to build the tuple itself and get the order wrong:
    `if job in seen`, `seen.add(result)`.
    """

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db or get_db()
        # Release postings retired by a scoring failure before snapshotting the set —
        # afterwards would silently no-op for this run. Installs that ran while the
        # claude_code backend was broken have postings stuck here that no fix could
        # otherwise reach.
        freed = self._db.forget_seen_scoring_errors(SCORING_ERROR_SKIP_REASONS)
        if freed:
            logger.info("SeenStore: %d postings released after earlier scoring errors",
                        freed)
        self._ids: set[tuple[str, str]] = self._db.seen_ids()
        self._new: set[tuple[str, str]] = set()
        logger.info("SeenStore: %d previously scored postings loaded", len(self._ids))

    @staticmethod
    def key(item) -> tuple[str, str]:
        """`(board_token, job_id)` for a `Job` or a `MatchResult`.

        `board_token or ""` matches what `insert_jobs` stores, so a posting whose
        board token is somehow blank still round-trips instead of silently never
        being retired.
        """
        return (item.board_token or "", item.job_id)

    def __contains__(self, item) -> bool:
        return self.key(item) in self._ids

    def add(self, item) -> None:
        key = self.key(item)
        if key not in self._ids:
            self._ids.add(key)
            self._new.add(key)

    def save(self) -> None:
        if self._new:
            self._db.mark_seen(self._new)
            logger.info("SeenStore: %d newly retired postings persisted", len(self._new))
            self._new.clear()
