"""Every database read the overview page needs, in one place.

Kept separate from the renderer so that the expensive question — "how much does
it cost to refresh this page?" — has a single answer. The page is rebuilt
repeatedly *during* a sweep, so the shape here matters:

* `run_snapshot` is all counts. It stays cheap for the whole run.
* `overview_snapshot` loads rows, and the rows are real work for most of a sweep —
  `matches` fills from the first employer on, because selection is a streaming
  per-job cutoff rather than a global top-K resolved at the sentinel. It therefore
  takes the caller's already-loaded `run` and `records` rather than re-deriving
  them, and `reporting.refresh` passes both. Do not assume an early call is free:
  that was true only under top-K, and `reporting/__init__.py` documents the same
  correction for the same reason.

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
    "yoe_below_requirement": "Asks for more years of experience than the resume shows",
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
        # current reasons plus the pre-cutoff one, so a page spanning old and new
        # runs counts the same thing in every row.
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


# The two LLM-free verdicts. A row carrying either was killed by a gate that costs
# nothing to run, which is what separates the overview page's last section from the
# one above it. Cluster siblings inherit their representative's reason, so this
# catches them too — and that is right: nothing judged that cluster.
_FREE_GATE_VERDICTS = ("rerank_below_cutoff", "yoe_below_requirement")


def partition_jobs(
    records: list[dict], applied_ids: set[str]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split match rows into the overview page's last three sections, in page order.

    Returns `(shortlisted, filtered, seen)`; the applied section is built from the
    `applied` table instead, because it carries the timestamp and status that no
    `matches` row has.

    The sections are **disjoint** — every job renders exactly once. That is the whole
    point of the page: it is the only list the user has of what is left to do, and a
    job appearing in two of them would double it. Note the counts therefore do *not*
    match the tiles above, which are a cumulative funnel.

    Ordering comes free from the caller. `load_all_matches` already returns rows by
    LLM score, then cross-encoder logit, then the old wide-pass column, and a stable
    partition preserves that within each bucket.

    One asymmetry with the `relevant` tile, deliberately: the tile excludes cluster
    siblings, because they were grouped after the rerank and never competed for a
    slot. Here a sibling follows its verdict — it carries a real score copied from its
    representative and belongs beside it.
    """
    shortlisted: list[dict] = []
    filtered: list[dict] = []
    seen: list[dict] = []

    for record in records:
        if record.get("job_id") in applied_ids:
            continue
        if (record.get("skip_reason") or "") in _FREE_GATE_VERDICTS:
            seen.append(record)
        elif record.get("shortlisted"):
            shortlisted.append(record)
        else:
            # Everything else cleared both free gates: either the LLM judged it and
            # it missed the threshold, or it never got a call for a reason that says
            # nothing about relevance — the run's cap, an API error, no description.
            filtered.append(record)

    return shortlisted, filtered, seen


def reason_label(reason: str | None) -> str:
    """A human phrase for a `skip_reason`, falling back to the raw value.

    Unknown reasons are shown verbatim rather than bucketed into "other": a reason
    nobody has labelled yet is exactly the one worth reading.
    """
    key = reason or ""
    return REASON_LABELS.get(key, key.replace("_", " ").capitalize() or "—")


# Caps on what the overview page renders. A closed `<details>` still costs its full
# DOM, so an uncapped lifetime page on a mature install would be tens of megabytes
# reloading itself every fifteen seconds. The summaries print the true count either
# way, so a capped list says how much it is not showing.
MAX_JOB_ROWS = 300
MAX_TAIL_ROWS = 5000


def overview_snapshot(
    db: Database,
    run_id: str | None = None,
    live: bool | None = None,
    run: dict[str, Any] | None = None,
    records: list[dict] | None = None,
) -> dict[str, Any]:
    """Everything the overview page renders, at one scope or the other.

    `run_id` selects the scope: a run id gives the per-sweep page beside that run's
    CSVs, `None` gives the lifetime page at the results root. The two go through
    different loaders — one run's rows are already indexed and cheap, the whole
    install's need grouping and a cap — but come back in the same shape, so the
    renderer is written once.

    Applications are the one figure that cannot be scoped cleanly: `applied` has no
    `run_id`. At run scope it means "applications to jobs this sweep saw", which is
    the closest honest reading and is the same rule `overview_counts` applies.

    `run` lets a caller hand in a `run_snapshot` it already has, and `records` the
    rows from `load_all_matches`. `reporting.refresh` has both a few lines earlier and
    always passes them: the reports rebuild on a clock for the whole length of a
    sweep, so re-deriving either here would double that work ~300 times a run.
    """
    counts = db.overview_counts(run_id)
    applied = db.load_applied_matches(run_id)
    applied_ids = {r.get("job_id") for r in applied}

    if run_id:
        rows = db.load_all_matches(run_id) if records is None else records
    else:
        # Two calls because the lifetime loader ranks each half by the score that
        # half actually has. Concatenated in that order, the partition below inherits
        # "LLM score first, then the cross-encoder logit" for free.
        rows = db.load_lifetime_matches(
            judged=True, limit=MAX_JOB_ROWS * 2 + len(applied)
        ) + db.load_lifetime_matches(judged=False, limit=MAX_TAIL_ROWS)

    shortlisted, filtered, seen = partition_jobs(rows, applied_ids)

    # The title-gate rejections, which live only in `jobs` — nothing wrote them a
    # `matches` row. They carry no score, so appending them after the rows that do
    # keeps the blanks at the bottom of the last section.
    seen += [
        r for r in db.load_unmatched_jobs(run_id, MAX_TAIL_ROWS)
        if r.get("job_id") not in applied_ids
    ]

    snapshot: dict[str, Any] = {
        "run_id": run_id,
        "counts": counts,
        "applied": applied[:MAX_JOB_ROWS],
        "applied_total": len(applied),
        "shortlisted": shortlisted[:MAX_JOB_ROWS],
        "shortlisted_total": len(shortlisted),
        "filtered": filtered[:MAX_JOB_ROWS],
        "filtered_total": len(filtered),
        "seen": seen[:MAX_TAIL_ROWS],
        "seen_total": len(seen),
        "started_at": None,
        "finished_at": None,
        "usage": None,
    }

    if run_id:
        run = run if run is not None else run_snapshot(db, run_id)
        snapshot["started_at"] = run.get("started_at")
        snapshot["finished_at"] = run.get("finished_at")
        snapshot["usage"] = run.get("usage")
        snapshot["live"] = run.get("in_progress") if live is None else live
    else:
        # The lifetime page has no run of its own to ask about, and scanning every
        # run for one still in progress would count a crashed sweep as live forever.
        # Its caller knows — `reporting.refresh` is always driven by a live run — so
        # it passes the answer in, and the default is the safe one.
        snapshot["live"] = bool(live)
    return snapshot
