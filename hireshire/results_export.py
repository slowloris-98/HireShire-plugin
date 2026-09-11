"""The results CSV: every posting that reached the funnel, best first.

One file, because two were worse than one. There used to be a shortlist CSV — a
worklist of what to apply to — and a separate all-jobs CSV recording the run's
reasoning. Their columns overlapped heavily and neither answered the question a
user actually asks: *show me everything, best first, and tell me what I have
already applied to.*

What it holds is every row in `matches`: scored, below-cutoff, capped,
years-of-experience-dropped, and cluster siblings. Not the title-gate rejections —
those never get a `matches` row, there can be tens of thousands of them in a sweep,
and every score column would be blank. They are on the overview page's
`Total Jobs Seen` section instead, which reads the `jobs` table for exactly that
reason.

Two score columns, never merged into one. `llm_score` is a 0-100 percentage from an
LLM judge and `cross_score` is a cross-encoder logit on an unbounded scale that is
personal to the user's own profile. Averaging them or sorting on a blend would be
meaningless, and presenting them in one column would invite exactly that.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RESULTS_SUFFIX = "_results.csv"

FIELDS = [
    # When the employer posted it, from `jobs.updated_at` — not when we processed
    # it. They are different questions and this is the one a reader asks of a job.
    "posted_at",
    "company",
    "job_title",
    "link",
    "llm_score",
    "cross_score",
    "applied",
    "shortlisted",
]


def results_name(stamp: str) -> str:
    return f"{stamp}{RESULTS_SUFFIX}"


def _never_scored(record: dict) -> bool:
    """True when no LLM verdict stands behind this row's relevance_score.

    Rows that inherited a verdict from a cluster representative ARE scored — the
    call was made, just once for the whole cluster — so they keep their number.

    Same rule as `hireshire.reporting.data._never_scored`, and the two must stay
    the same: there is one definition of "judged" and both a CSV cell and a page
    cell are rendered from it.
    """
    if record.get("cluster_representative"):
        return False
    return bool(record.get("skipped")) or record.get("relevance_score") is None


def _llm_score(record: dict):
    """The judge's score, or None when no judge ever read this posting.

    Blank rather than 0 in the file. A budget drop carries `relevance_score=0` from
    the shared `filtered_result` builder, and printing that as a score reads as
    "the model judged this worthless" — the exact misreading that hid a broken
    reranker for a whole run.
    """
    return None if _never_scored(record) else record.get("relevance_score")


def _num(value) -> str | float:
    return "" if value is None else round(float(value), 4)


def _yes(value) -> str:
    return "yes" if value else "no"


def _sort_key(record: dict) -> tuple:
    """Best first: LLM score, then the cross-encoder logit, blanks last in both.

    Sorted here rather than in SQL, and that is not duplication.
    `Database.load_all_matches` orders by `relevance_score DESC`, which reads a
    never-scored row's placeholder 0 as a real score and files it among the genuine
    zeroes instead of at the bottom. `_never_scored` is the only thing that knows
    the difference, and it is Python.
    """
    llm = _llm_score(record)
    cross = record.get("rerank_score")
    return (
        llm is None, -(llm or 0),
        cross is None, -(float(cross) if cross is not None else 0.0),
    )


def _row(record: dict, applied_ids: set[str]) -> dict:
    llm = _llm_score(record)
    return {
        "posted_at": record.get("posted_at") or "",
        # `board_token` is the employer's board slug and is the closest thing to a
        # company name the funnel ever has — nothing upstream resolves a display
        # name, so this is the honest value rather than a prettified guess.
        "company": record.get("board_token") or "",
        "job_title": record.get("title") or "",
        "link": record.get("absolute_url") or "",
        "llm_score": "" if llm is None else llm,
        "cross_score": _num(record.get("rerank_score")),
        # `applied` has no `run_id` — an application is a fact about a job, not
        # about the sweep that surfaced it — so this is "have I ever applied to
        # this", which is the only question the table can answer.
        "applied": _yes(record.get("job_id") in applied_ids),
        "shortlisted": _yes(record.get("shortlisted")),
    }


def write_results_csv(records: list[dict], path: Path,
                      applied_ids: set[str] | None = None) -> Path | None:
    """Write the results CSV. Returns the path, or None if it could not be written.

    `applied_ids` comes from `Database.applied_ids()`. The caller passes it because
    no match record carries it: the `applied` table is keyed on the job alone and is
    read once for the whole file rather than per row.

    Never raises: losing this file must not take down a run whose database rows are
    already safe — the same trade `hireshire.reporting.refresh` makes. It is also
    called from a `finally`, so a sweep that died half-scored still writes what it
    judged, and an exception here would replace the real traceback with its own.
    """
    ids = applied_ids or set()
    rows = sorted(records, key=_sort_key)
    try:
        # utf-8-sig so Excel reads the em-dashes in job titles correctly.
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(_row(r, ids) for r in rows)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        return None
    logger.info("Wrote %d rows to %s", len(rows), path)
    return path
