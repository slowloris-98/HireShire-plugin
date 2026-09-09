"""Every database read the two reports need, in one place.

Kept separate from the renderers so that the expensive question — "how much does
it cost to refresh this page?" — has a single answer. Both reports are rebuilt
repeatedly *during* a sweep, so the shape here matters:

* `run_snapshot` is all counts. It stays cheap for the whole run.
* `scored_jobs` / `unscored_jobs` load rows, and only ever return anything after
  the matcher's sentinel has fired — `matches` is empty until then, because top-K
  is a global decision and the budget is spent at the end. So the expensive path
  costs nothing for the ~18 minutes where it would be called most often.

Nothing here imports the matcher or funnel config modules. Those pull in pydantic
and the phase loaders, and this runs on the pipeline's progress callbacks; the two
settings the reports actually want are read straight out of the YAML, exactly as
`paths.workspace_dir()` does and for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from hireshire import paths
from hireshire.storage.db import PHASE_MATCH, PHASE_PIPELINE, PHASE_SCRAPE, Database

logger = logging.getLogger(__name__)

# The rubric the scoring prompt actually imposes, from hireshire/matcher/prompts.py.
# Duplicated here rather than imported because prompts.py is a prompt module, and a
# renderer importing it would make the prompt text a report dependency.
RUBRIC = (
    ("core_skills_score", "core_skills_rationale", "Core technical skills", 40),
    ("experience_score", "experience_rationale", "Relevant experience", 40),
    ("education_bonus_score", "education_rationale", "Education & nice-to-haves", 20),
)

# Skip reasons that mean "judged, just not by its own LLM call" or "judged and
# lost", as opposed to "the scorer broke". Only the first group is worth naming in
# a report; the rest are surfaced verbatim.
REASON_LABELS = {
    "": "Scored by the LLM",
    "rerank_below_cutoff": "Below the relevance cutoff — the cross-encoder read it and said no",
    "llm_call_cap_reached": "Reached the run's call cap — still eligible next sweep",
    # Written by runs made before selection became a cutoff. Nothing produces it any
    # more, but the reports render historical rows and an unlabelled reason shows up
    # verbatim, which reads as a bug.
    "rerank_below_top_k": "Over budget — lost the top-K race",
    "duplicate_of_cluster": "Duplicate requisition — verdict copied from its cluster",
    "no_content_text": "No description text to score",
    "api_error": "Scoring call failed",
}


def _matcher_settings() -> dict[str, Any]:
    """`threshold` and `top_k`, read directly from the user's YAML.

    Never raises: a report that cannot find the threshold still renders, it just
    stops claiming to know where the cutoff was.
    """
    out: dict[str, Any] = {"threshold": None, "top_k": None}
    try:
        raw = yaml.safe_load(
            paths.config_file("matcher.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("Could not read matcher.yaml for the report: %s", exc)
        return out
    out["threshold"] = (raw.get("settings") or {}).get("threshold")
    out["top_k"] = (raw.get("funnel") or {}).get("top_k")
    return out


def run_snapshot(db: Database, run_id: str) -> dict[str, Any]:
    """Counts for one run — the whole funnel, safe to call every few seconds.

    `in_progress` keys off the pipeline phase's `runs` row, which is written by
    `_finalise_pipeline` after every output file is on disk. That makes it the one
    honest signal for "should this page keep refreshing".
    """
    scrape = db.scrape_counts(run_id)
    match = db.match_counts(run_id)
    phases = db.run_phase_stats(run_id)
    settings = _matcher_settings()

    candidates = match["rows_total"]
    jobs = scrape["jobs"]
    # Only computable once the matcher has finished: until the sentinel fires,
    # `candidates` is 0 because nothing is persisted, and jobs - 0 would report
    # every job as gated out.
    matching_done = PHASE_MATCH in phases
    gated_out = max(0, jobs - candidates) if matching_done else None

    return {
        "run_id": run_id,
        "in_progress": PHASE_PIPELINE not in phases,
        "matching_done": matching_done,
        "started_at": (phases.get(PHASE_PIPELINE) or phases.get(PHASE_SCRAPE) or {}).get("started_at"),
        "finished_at": (phases.get(PHASE_PIPELINE) or {}).get("finished_at"),
        "companies": scrape["companies"],
        "companies_with_jobs": scrape["companies_with_jobs"],
        "scrape_errors": scrape["errors"],
        "jobs": jobs,
        "gated_out": gated_out,
        "candidates": candidates,
        # Everything that cleared the title gates and still got no LLM call. Both
        # current reasons plus the pre-cutoff one, so a dashboard spanning old and
        # new runs counts the same thing in every row.
        "over_budget": sum(
            match["by_reason"].get(r, 0)
            for r in ("rerank_below_cutoff", "llm_call_cap_reached", "rerank_below_top_k")
        ),
        # ...and the two apart, because they mean opposite things to the user. A
        # cutoff drop is a verdict and the job is retired; a cap drop is a deferral
        # and it comes back next sweep. Telling someone a retired job is "still
        # eligible" is worse than saying nothing.
        "below_cutoff": match["by_reason"].get("rerank_below_cutoff", 0),
        "cap_reached": sum(
            match["by_reason"].get(r, 0)
            for r in ("llm_call_cap_reached", "rerank_below_top_k")
        ),
        "duplicates": match["by_reason"].get("duplicate_of_cluster", 0),
        "scored": match["scored"],
        "shortlisted": match["shortlisted"],
        "top_score": match["top_score"],
        "by_reason": match["by_reason"],
        "threshold": settings["threshold"] or (phases.get(PHASE_MATCH) or {}).get("threshold"),
        "top_k": settings["top_k"],
        "model": (phases.get(PHASE_MATCH) or {}).get("model"),
        # What the scoring drew: calls, tokens, cache reads and a cost estimate.
        # None until the matcher finalises — like `model` and unlike the counts, it
        # is written once at the end — so a live page simply shows no cost figure
        # rather than a figure that climbs and then stops meaning anything.
        "usage": (phases.get(PHASE_MATCH) or {}).get("usage"),
    }


def _never_scored(record: dict) -> bool:
    """True when no LLM verdict stands behind this row's relevance_score.

    Same rule as `hireshire.results_export._never_scored`, and it must stay the
    same: a row that inherited a verdict from its cluster representative *was*
    judged, just once for the whole cluster, so it keeps its number. Getting this
    wrong prints a budget drop's placeholder 0 as if the model had returned it.
    """
    if record.get("cluster_representative"):
        return False
    return bool(record.get("skipped")) or record.get("relevance_score") is None


def split_matches(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition a run's match rows into (scored, not scored).

    `records` arrives from `Database.load_all_matches`, already ordered best-first
    by LLM score, then the cross-encoder logit, then the old wide-pass column —
    three keys applied in sequence rather than merged, because on rows old enough
    to carry both rerank columns they came from different models.
    """
    scored = [r for r in records if not _never_scored(r)]
    unscored = [r for r in records if _never_scored(r)]
    return scored, unscored


def reason_label(reason: str | None) -> str:
    """A human phrase for a `skip_reason`, falling back to the raw value.

    Unknown reasons are shown verbatim rather than bucketed into "other": a reason
    nobody has labelled yet is exactly the one worth reading.
    """
    key = reason or ""
    return REASON_LABELS.get(key, key.replace("_", " ").capitalize() or "—")


def applied_summary(db: Database) -> dict[str, Any]:
    """Applications, cumulative.

    The `applied` table has no `run_id` — an application is a fact about a job, not
    about the sweep that surfaced it — so this is a lifetime total for the install
    and cannot be attributed to a run. The dashboard says so rather than implying
    the number belongs to the newest sweep.
    """
    rows = db.load_applied()
    submitted = [r for r in rows if r.get("status") == "submitted"]
    errors = [r for r in rows if r.get("status") == "error"]
    skipped = [r for r in rows if r.get("status") == "skipped"]
    return {
        "total": len(rows),
        "submitted": len(submitted),
        "errors": len(errors),
        "skipped": len(skipped),
        "recent": list(reversed(rows))[:15],
    }


def dashboard_snapshot(db: Database, limit: int = 30) -> dict[str, Any]:
    """The whole install at a glance: per-run counts plus lifetime totals."""
    runs = []
    for row in db.recent_runs(limit=limit):
        snap = run_snapshot(db, row["run_id"])
        snap["finished_at"] = snap["finished_at"] or row.get("finished_at")
        snap["started_at"] = snap["started_at"] or row.get("started_at")
        runs.append(snap)

    totals = {
        "runs": len(runs),
        "jobs": sum(r["jobs"] for r in runs),
        "candidates": sum(r["candidates"] for r in runs),
        "scored": sum(r["scored"] for r in runs),
        "shortlisted": sum(r["shortlisted"] for r in runs),
        # Runs that were never measured contribute nothing rather than breaking the
        # sum, so this is "what the measured sweeps cost", which is the most that can
        # honestly be said across an install that predates the tally.
        "cost_usd": sum((r.get("usage") or {}).get("cost_usd") or 0 for r in runs),
    }
    return {
        "runs": runs,
        "totals": totals,
        "applied": applied_summary(db),
        "live": next((r for r in runs if r["in_progress"]), None),
    }
