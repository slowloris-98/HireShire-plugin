from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from hireshire.matcher.scorer import MatchResult
from hireshire.storage.db import PHASE_MATCH, Database, get_db

logger = logging.getLogger(__name__)


def is_shortlisted(result: MatchResult, threshold: int) -> bool:
    """A never-scored result (skip_llm) passes; a scored one must clear the threshold."""
    if result.skipped:
        return False
    return result.relevance_score is None or result.relevance_score >= threshold


class MatchStore:
    """Matcher persistence backed by the shared SQLite database.

    Each scored result is committed immediately to the `matches` table (crash-
    survivable — replaces the old progress.jsonl), tagged with `shortlisted` so
    the tuner/applier can query survivors directly. `finalise` records the run's
    summary stats.
    """

    def __init__(self, run_id: str, threshold: int, db: Optional[Database] = None) -> None:
        self.run_id = run_id
        self._threshold = threshold
        self._db = db or get_db()

    def load_progress(self) -> list[MatchResult]:
        """Return results from a previous partial run (matches present, run not finalised)."""
        if self._db.run_exists(self.run_id, PHASE_MATCH):
            return []  # run already completed — nothing to resume
        results: list[MatchResult] = []
        for raw in self._db.load_matches(self.run_id):
            try:
                results.append(MatchResult.model_validate(raw))
            except Exception:  # noqa: BLE001
                logger.warning("Skipping malformed match row during resume")
        return results

    async def append_result(self, result: MatchResult) -> None:
        """Commit one result immediately — survives a mid-run crash."""
        shortlisted = is_shortlisted(result, self._threshold)
        await asyncio.to_thread(
            self._db.upsert_match,
            self.run_id,
            result.job_id,
            result.board_token,
            result.title,
            result.relevance_score,
            shortlisted,
            result.skipped,
            result.skip_reason,
            result.source_run_id,
            result.scored_at.isoformat(),
            result.model_dump_json(),
        )

    def finalise(
        self,
        shortlisted: list[MatchResult],
        rejected: list[MatchResult],
        started_at: datetime,
        threshold: int,
        model: str,
        total_loaded: int,
    ) -> None:
        skipped = [r for r in rejected if r.skipped]
        stats = {
            "threshold": threshold,
            "model": model,
            "total_jobs_loaded": total_loaded,
            "total_jobs_scored": total_loaded - len(skipped),
            "total_jobs_skipped": len(skipped),
            "shortlisted_count": len(shortlisted),
            "rejected_count": len(rejected) - len(skipped),
        }
        self._db.finalise_run(self.run_id, PHASE_MATCH, started_at.isoformat(), None, stats)
        logger.info(
            "Saved %d shortlisted, %d rejected (run %s)",
            len(shortlisted), len(rejected), self.run_id,
        )
