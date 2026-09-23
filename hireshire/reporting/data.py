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
    ("core_skills_score", "core_skills_rationale", "Core skills", 40),
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
    # Written by the applier, not the matcher: the posting page stated a location
    # outside `scraper.location_filter`. The job keeps its LLM score — it was judged,
    # then found to be somewhere the user will not work.
    "location_mismatch": "Outside your search locations",
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
        "scrape_done": PHASE_SCRAPE in phases,
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

    Returns `(shortlisted, filtered, seen)`; the Jobs Applied and Needs Attention
    sections are built from the `applied` table instead, split on its status, because
    it carries the timestamp, status and reason that no `matches` row has.

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


def _pct(done: int, total: int) -> float:
    """0–100, clamped, and never a division by zero."""
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * done / total))


def progress_bars(progress: dict | None, run: dict[str, Any]) -> list[dict]:
    """The overview's three bars, from `Database.run_progress` and a `run_snapshot`.

    Returns `[]` for a run with no progress row: one made before the bars existed,
    or a phase run standalone. The page then simply has no progress block.

    Each bar's `state` is `waiting` (nothing to count yet), `running`, `done`, or
    `off` (the applier only). Once the pipeline's own run row exists every bar is
    `done` whatever its fill — a sweep that died part-way keeps the fill where it
    stopped, and that shortfall is the fact worth showing.

    The applier bar is stacked, because "handled" is not "applied": an excluded
    company or a deferral finishes a job without an application, and a bar that only
    counted submissions could never reach the end on a sweep that had one.
    """
    if not progress:
        return []
    finished = not run.get("in_progress", True)

    def state(total: int, done_when: bool) -> str:
        if finished or done_when:
            return "done"
        return "running" if total > 0 else "waiting"

    # -- scraper
    c_total = int(progress.get("companies_total") or 0)
    c_done = min(int(progress.get("companies_done") or 0), c_total) if c_total else 0
    s_note = f"{int(run.get('jobs') or 0):,} jobs in scope"
    if run.get("scrape_errors"):
        s_note += f" · {int(run['scrape_errors']):,} boards failed"
    scraper = {
        "key": "scraper", "label": "Scraper", "unit": "companies",
        "done": c_done, "total": c_total,
        "state": state(c_total, bool(run.get("scrape_done"))),
        "note": s_note if c_total else "Loading the company lists",
    }

    # -- matcher. Not done until the scrape is: the denominator is still growing.
    j_total = int(progress.get("jobs_in_scope") or 0)
    j_done = min(int(progress.get("jobs_processed") or 0), j_total)
    if j_total:
        m_note = (f"{int(run.get('scored') or 0):,} scored by the LLM · "
                  f"{int(run.get('shortlisted') or 0):,} shortlisted")
    elif run.get("scrape_done") or finished:
        m_note = "No jobs in scope this sweep"
    else:
        m_note = "Waiting for the scraper"
    matcher = {
        "key": "matcher", "label": "Matcher", "unit": "jobs",
        "done": j_done, "total": j_total,
        "state": state(j_total, bool(run.get("matching_done"))),
        "note": m_note,
    }

    # -- applier
    if not progress.get("apply_enabled"):
        applier = {
            "key": "applier", "label": "Applier", "unit": "jobs",
            "done": 0, "total": 0, "state": "off", "note": "Auto-apply is off",
        }
    else:
        a_total = int(progress.get("apply_queued") or 0)
        handled = min(int(progress.get("apply_handled") or 0), a_total)
        submitted = min(int(progress.get("submitted") or 0), handled)
        attention = min(int(progress.get("attention") or 0), handled - submitted)
        skipped = handled - submitted - attention
        if a_total:
            parts = [f"{submitted:,} applied"]
            if attention:
                parts.append(f"{attention:,} need attention")
            if skipped:
                parts.append(f"{skipped:,} skipped")
            a_note = " · ".join(parts)
        else:
            a_note = ("Nothing shortlisted this sweep" if finished
                      else "Waiting for shortlisted jobs")
        applier = {
            "key": "applier", "label": "Applier", "unit": "shortlisted",
            "done": handled, "total": a_total,
            "state": state(a_total, False),
            "note": a_note,
            "segments": [
                ("ok", _pct(submitted, a_total)),
                ("warn", _pct(attention, a_total)),
                ("skip", _pct(skipped, a_total)),
            ],
        }

    bars = [scraper, matcher, applier]
    for bar in bars:
        bar["pct"] = _pct(bar["done"], bar["total"])
    return bars


def lifetime_progress_bars(lp: dict | None, live: bool) -> list[dict]:
    """The lifetime page's bars, from `Database.lifetime_progress`. Shown always.

    Same shape as `progress_bars`, so one renderer draws both. Scraper and matcher
    are sums over every tracked sweep; while one is live its counters are inside
    those sums, so the two bars read `running` then and `done` otherwise. The
    applier is the all-time backlog of shortlisted jobs and has no running state of
    its own: its segments already say what is applied and what needs the user, and
    the empty rest of the track is what is still to do.

    Returns `[]` only for an install with no tracked sweep and no shortlist, so a
    fresh page carries no block of empty bars.
    """
    if not lp:
        return []
    sweeps = int(lp.get("sweeps") or 0)
    shortlisted = int(lp.get("shortlisted") or 0)
    if not sweeps and not shortlisted:
        return []
    stage = "running" if live else "done"
    across = (f"across {sweeps:,} sweep{'s' if sweeps != 1 else ''}" if sweeps
              else "No sweeps tracked yet")

    c_total = int(lp.get("companies_total") or 0)
    j_total = int(lp.get("jobs_in_scope") or 0)
    submitted = min(int(lp.get("submitted") or 0), shortlisted)
    attention = min(int(lp.get("attention") or 0), shortlisted - submitted)
    pending = shortlisted - submitted - attention

    parts = [f"{submitted:,} applied"]
    if attention:
        parts.append(f"{attention:,} need attention")
    parts.append(f"{pending:,} not yet applied")

    # The scraper bar prints unique jobs rather than companies: summed across
    # sweeps, a company count (190,416) says nothing a user can use. Unique jobs has
    # no "out of", so the fill still tracks companies checked — the only measure of
    # how far a live scrape has got — and `count` replaces only the printed figure.
    unique = int(lp.get("unique_jobs") or 0)
    bars = [
        {"key": "scraper", "label": "Scraper", "unit": "companies",
         "done": min(int(lp.get("companies_done") or 0), c_total), "total": c_total,
         "count": f"{unique:,} unique jobs",
         "state": stage if c_total else "waiting", "note": across},
        {"key": "matcher", "label": "Matcher", "unit": "jobs",
         "done": min(int(lp.get("jobs_processed") or 0), j_total), "total": j_total,
         "state": stage if j_total else "waiting", "note": across},
        {"key": "applier", "label": "Applier", "unit": "shortlisted",
         "done": submitted + attention, "total": shortlisted,
         "state": "done" if shortlisted else "waiting",
         "note": " · ".join(parts) if shortlisted else "Nothing shortlisted yet",
         "segments": [("ok", _pct(submitted, shortlisted)),
                      ("warn", _pct(attention, shortlisted))]},
    ]
    for bar in bars:
        bar["pct"] = _pct(bar["done"], bar["total"])
    return bars


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
    progress: dict | None = None,
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

    `progress` differs by scope. On the run page it is `Database.run_progress` and
    the bars are that sweep's, kept after it finishes as a record of where each stage
    ended. On the lifetime page it is `Database.lifetime_progress` and the bars are
    lifetime totals, shown always — live or not, because they describe the install
    rather than any one sweep.
    """
    counts = db.overview_counts(run_id)
    # The `applied` table feeds two sections. A submission goes under Jobs Applied;
    # every other status — `error`, `excluded`, or one nobody has named yet — is an
    # application that needs the user, so it goes under Needs Attention rather than
    # vanishing. `excluded` is the one no session produces: the applier wrote it
    # itself because the employer's portal needs an account login.
    # Both halves stay in `applied_ids`, so neither reappears under Shortlisted.
    attempts = db.load_applied_matches(run_id)
    applied = [r for r in attempts if r.get("applied_status") == "submitted"]
    attention = [r for r in attempts if r.get("applied_status") != "submitted"]
    applied_ids = {r.get("job_id") for r in attempts}

    if run_id:
        rows = db.load_all_matches(run_id) if records is None else records
    else:
        # Two calls because the lifetime loader ranks each half by the score that
        # half actually has. Concatenated in that order, the partition below inherits
        # "LLM score first, then the cross-encoder logit" for free.
        rows = db.load_lifetime_matches(
            judged=True, limit=MAX_JOB_ROWS * 2 + len(attempts)
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
        "attention": attention[:MAX_JOB_ROWS],
        "attention_total": len(attention),
        "shortlisted": shortlisted[:MAX_JOB_ROWS],
        "shortlisted_total": len(shortlisted),
        "filtered": filtered[:MAX_JOB_ROWS],
        "filtered_total": len(filtered),
        "seen": seen[:MAX_TAIL_ROWS],
        "seen_total": len(seen),
        "started_at": None,
        "finished_at": None,
        "usage": None,
        "progress": [],
    }

    if run_id:
        run = run if run is not None else run_snapshot(db, run_id)
        snapshot["progress"] = progress_bars(progress, run)
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
        snapshot["progress"] = lifetime_progress_bars(progress, snapshot["live"])
    return snapshot
