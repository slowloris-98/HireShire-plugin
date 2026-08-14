"""The all-jobs CSV: every scored, dropped and duplicated posting in one file.

Separate from the shortlist CSV because they answer different questions. The
shortlist is a worklist — what to apply to, one row per requisition, consumed by
the apply skill. This file is a record of the run's reasoning: why did a job the
user would have liked not reach them?

That question was unanswerable before. A sweep that shortlisted nothing left the
user with an empty CSV and no way to tell a bad resume from a bad threshold from a
broken reranker — the last of which turned out to be the actual fault.

Four score columns, never merged into one. They are a bi-encoder cosine, two logits
from two *different* cross-encoders, and an LLM percentage. Averaging them or
sorting on a blend would be meaningless, and presenting them in one column would
invite exactly that.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ALL_JOBS_SUFFIX = "_results_all_jobs.csv"

FIELDS = [
    "processed_at",
    "company",
    "job_title",
    "location",
    "llm_score",
    "cross_score_refined",
    "cross_score_wide",
    "bi_score",
    "rerank_stage",
    "status",
    "recommend",
    "cluster_size",
    "cluster_representative",
    "posted_at",
    "link",
    "job_id",
]


def all_jobs_name(stamp: str) -> str:
    return f"{stamp}{ALL_JOBS_SUFFIX}"


def _status(record: dict) -> str:
    """One word for what happened to this job, from the row's own fields."""
    if record.get("skip_reason"):
        return str(record["skip_reason"])
    if record.get("skipped"):
        return "skipped"
    if record.get("shortlisted"):
        return "shortlisted"
    return "scored_below_threshold"


def _row(record: dict) -> dict:
    return {
        "processed_at": record.get("scored_at") or "",
        "company": record.get("board_token") or "",
        "job_title": record.get("title") or "",
        "location": record.get("location") or "",
        # Blank rather than 0 for jobs the LLM never saw. A budget drop carries
        # relevance_score=0 from the shared `filtered_result` builder, and printing
        # that as a score reads as "the model judged this worthless" — the exact
        # misreading that hid the reranker fault for a whole run.
        "llm_score": "" if _never_scored(record) else record.get("relevance_score"),
        "cross_score_refined": _num(record.get("rerank_score")),
        "cross_score_wide": _num(record.get("rerank_score_wide")),
        "bi_score": _num(record.get("encoder_score")),
        "rerank_stage": record.get("rerank_stage") or "",
        "status": _status(record),
        "recommend": "yes" if record.get("recommend") else "no",
        "cluster_size": record.get("cluster_size") or 1,
        "cluster_representative": record.get("cluster_representative") or "",
        "posted_at": record.get("posted_at") or "",
        "link": record.get("absolute_url") or "",
        "job_id": record.get("job_id") or "",
    }


def _never_scored(record: dict) -> bool:
    """True when no LLM verdict stands behind this row's relevance_score.

    Rows that inherited a verdict from a cluster representative ARE scored — the
    call was made, just once for the whole cluster — so they keep their number.
    """
    if record.get("cluster_representative"):
        return False
    return bool(record.get("skipped")) or record.get("relevance_score") is None


def _num(value) -> str | float:
    return "" if value is None else round(float(value), 4)


def write_all_jobs_csv(records: list[dict], path: Path) -> Path | None:
    """Write the all-jobs CSV. Returns the path, or None if it could not be written.

    Never raises: this file is a diagnostic, and losing it must not take down a run
    whose real output — the database rows and the shortlist — is already safe.
    """
    try:
        # utf-8-sig so Excel reads the em-dashes in job titles correctly. The
        # shortlist CSV predates this and is left alone to avoid changing a file
        # the apply skill already parses.
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(_row(r) for r in records)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        return None
    logger.info("Wrote %d rows to %s", len(records), path)
    return path
