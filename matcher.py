"""
Scores the latest scrape run's jobs against the resume.

Cheap title gates and detail hydration run first (see hireshire/funnel/), then a
cross-encoder reads each survivor's full description and only those reaching
`funnel.rerank.min_score` are sent to the LLM. `funnel.top_k` caps how many calls one
run may make, so a mis-set cutoff costs friction rather than the user's whole
allowance.

Every stage is a per-job decision, which is what lets the orchestrator judge a batch
the moment it is scraped instead of waiting for the sweep to finish.

    python matcher.py
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from hireshire import paths
from hireshire.funnel import cluster, experience
from hireshire.funnel.config import ExperienceConfig
from hireshire.funnel.detail_fetcher import DETAIL_SOURCES
from hireshire.funnel.funnel import Funnel
from hireshire.funnel.rerank import RERANK_STAGE, Reranker
from hireshire.matcher.config import load_matcher_config
from hireshire.matcher.loader import load_jobs
from hireshire.matcher.resume import extract_resume_text
from hireshire.matcher.scorer import (
    SCORING_ERROR_SKIP_REASONS,
    JobScorer,
    MatchResult,
    make_backend,
)
from hireshire.matcher.seen import SeenStore
from hireshire.matcher.store import MatchStore, is_shortlisted
from hireshire.matcher.title_filter import apply_title_filter, filtered_result
from hireshire.models.job import Job
from hireshire.storage.db import get_db
from hireshire.storage.json_store import RunStore

load_dotenv()

logger = logging.getLogger(__name__)
console = Console()

# Jobs that cleared the title gates but did not reach `rerank.min_score`. A VERDICT,
# not a deferral: the cross-encoder read the whole description and said no. Nothing
# about the next sweep changes that answer — same profile, same model, same text —
# so unlike a budget drop this one retires the job. Retrying it would re-run the
# same deterministic computation ~6 times per 24h to reach the same conclusion.
CUTOFF_SKIP_REASON = "rerank_below_cutoff"

# Jobs above the cutoff that arrived after the run had spent its `top_k` LLM calls.
# THIS one is a deferral, and the reasoning that used to attach to `rerank_below_top_k`
# now lives here: the job may be perfectly good and simply late in a busy sweep, so it
# has to stay eligible. Distinct from the cutoff reason because the two answer
# different questions — "is this job good enough" versus "was there budget left".
CAP_SKIP_REASON = "llm_call_cap_reached"

# The pre-cutoff budget drop. Nothing writes it any more; it is kept so the reports
# can still label rows from runs made before the funnel became a streaming cutoff.
BUDGET_SKIP_REASON = "rerank_below_top_k"

# The posting asks for meaningfully more experience than the candidate has, read
# straight out of the description by `funnel/experience.py`.
#
# A VERDICT, like the cutoff above, and deliberately absent from
# _RETRYABLE_SKIP_REASONS for exactly the same reason: the same description and the
# same `candidate_years` produce the same answer every time, so retrying it would
# re-run a deterministic computation ~6 times a day to reach the identical result.
#
# This reverses the guidance in analysis/results/extraction_prefilter.md, which
# required extraction drops to be RETRYABLE. That was written about an LLM extractor,
# where a misparse is a transient failure of the same class as the `--json-schema`
# bug that once retired 100 jobs permanently. A regex has no transient failure mode.
YOE_SKIP_REASON = "yoe_below_requirement"

# A repeat posting of a requisition whose representative WAS scored. Distinct from a
# budget drop because it is a verdict, not a deferral: the job has been judged, just
# by proxy, and it carries the representative's score. It must therefore stay OUT of
# _RETRYABLE_SKIP_REASONS below — re-queuing it next sweep would spend the budget
# re-deriving a score it already has.
DUPLICATE_SKIP_REASON = "duplicate_of_cluster"

# Reasons that must NOT retire a job_id into seen_jobs. A cap drop is the whole
# point here: max_age_hours=24 with a 4h poll means a job resurfaces in ~6 sweeps,
# and one that arrived after the budget ran out should get another shot in a quieter
# one. Marking it seen would retire it permanently on the strength of one busy run.
#
# The general rule: a job may be retired on a *verdict*, never on an *error* or a
# *deferral*. The scoring-failure reasons below are here because they once weren't —
# a broken `--json-schema` argument failed all 100 scoring calls in a run and
# permanently retired every one of them, so fixing the flag would not have brought
# them back. `no_content_text` and `llm_skipped` stay retiring: those are facts about
# the job.
#
# `CUTOFF_SKIP_REASON` is deliberately absent. It is the one drop that IS a verdict —
# see its definition above. `BUDGET_SKIP_REASON` stays listed so rows written by
# older runs, which are deferrals, are not retired now that nothing writes it.
_RETRYABLE_SKIP_REASONS = (
    {CAP_SKIP_REASON, BUDGET_SKIP_REASON} | set(SCORING_ERROR_SKIP_REASONS)
)


# Consecutive scoring failures after which the run stops calling the backend.
_BREAKER_LIMIT = 5


class _ScoringBreaker:
    """Stops a run once the scoring backend is clearly broken rather than dead.

    A misused `--json-schema` argument once failed all 100 scoring calls in a sweep;
    the run still reported "0 new matches", which reads as "nothing was good enough".
    Twenty minutes of scraping produced nothing and said nothing.

    It counts *results*, not exceptions: `JobScorer.score` converts a failed backend
    call into an `api_error` result and never raises. Once tripped it makes the
    remaining jobs return immediately instead of raising, which needs no asyncio
    cancellation, spends none of the remaining budget, and — because both
    `api_error` and `backend_unavailable` are in `_RETRYABLE_SKIP_REASONS` — leaves
    every unscored job eligible for the next run.
    """

    def __init__(self, limit: int = _BREAKER_LIMIT) -> None:
        self._limit = limit
        self._consecutive = 0
        self.tripped = False
        self.last_error: str | None = None

    # `backend_unavailable` is excluded: it is this class's own output, not evidence.
    _FAILURE_REASONS = {"api_error", "unexpected_error"}

    def record(self, result: MatchResult) -> None:
        if result.skip_reason in self._FAILURE_REASONS:
            self._consecutive += 1
            if self._consecutive >= self._limit and not self.tripped:
                self.tripped = True
                logger.error(
                    "Scoring backend failed %d times in a row — aborting scoring for "
                    "this run. Last error: %s", self._limit, self.last_error,
                )
        elif not result.skipped:
            self._consecutive = 0

    def summary(self) -> str:
        return (
            f"Scoring aborted after {self._limit} consecutive backend failures. "
            f"Last error: {self.last_error or 'unknown'}. "
            "No jobs were retired — they will be rescored on the next run."
        )


class _NoopProgress:
    def update(self, *a, **kw): pass
    def advance(self, *a, **kw): pass
    def add_task(self, *a, **kw): return 0
    def __enter__(self): return self
    def __exit__(self, *a): pass


async def _persist_hydrated_details(db, run_id: str, to_score) -> None:
    """Upsert funnel-hydrated Workday/BambooHR descriptions back to the jobs table.

    List-only rows are scraped with content_text=NULL and hydrated by the funnel
    in-memory only, so without this DB-backed readers (standalone tuner/apply,
    re-runs, pipeline/jobs exports) never see the description. `insert_jobs` is an
    INSERT OR REPLACE on (run_id, job_id) — it upserts the scrape-time row in
    place, updating content_text and detail_fetch_failed (carried in raw_json).
    Only detail-board survivors are touched; other boards already carry content."""
    changed = [
        j for j in to_score
        if j.source in DETAIL_SOURCES and (j.content_text or j.detail_fetch_failed)
    ]
    if changed:
        await asyncio.to_thread(db.insert_jobs, run_id, changed)


def _passthrough_result(job, run_id: str, score=None) -> MatchResult:
    result = MatchResult(
        job_id=job.job_id,
        board_token=job.board_token,
        title=job.title,
        location=job.location.name,
        absolute_url=str(job.absolute_url),
        relevance_score=None,
        match_reasons=["LLM scoring skipped"],
        disqualifiers=[],
        recommend=True,
        skip_reason="llm_skipped",
        scored_at=datetime.now(timezone.utc),
        source_run_id=run_id,
    )
    return _apply_rerank_scores(result, score)


def _apply_rerank_scores(
    result: MatchResult,
    score: float | None,
    cluster_size: int = 1,
    yoe_required: float | None = None,
) -> MatchResult:
    """Copy a cross-encoder logit onto a result row.

    One model, one scale, so this is a single number now. `rerank_score_wide` is
    left alone: the column still exists because runs made under the old two-stage
    cascade wrote to it and the reports render those rows, but nothing writes it
    any more."""
    if score is not None:
        result.rerank_score = score
        result.rerank_stage = RERANK_STAGE
    if yoe_required is not None:
        result.yoe_required = yoe_required
    result.cluster_size = cluster_size
    return result


def _yoe_required(job: Job) -> float | None:
    """What this posting asks for in years, or None if it says nothing.

    Recorded on EVERY reranked row regardless of whether the gate is switched on.
    Reading the number is free, and a column that only appears once the gate is
    enabled would leave a user with no way to find out what enabling it would cost
    them — the same reason budget drops are persisted individually rather than
    rolled into `finalise`'s summary stats.
    """
    req = experience.parse_requirement(job.content_text)
    return req.min_years if req else None


def _log_usage(scorer, quiet: bool) -> None:
    """Report what the run drew on the user's Claude allowance.

    Scoring shares a rolling 5-hour window and a weekly one with the user's own
    Claude chat, so "what did that sweep cost me" is a fair question that had no
    answer anywhere in the product. Only backends that can read their own meters
    expose a tally; the rest report nothing rather than print zeros as if they were
    measurements.

    Silent when no call was made, so a `--no-llm` run does not claim to have spent
    anything."""
    usage = getattr(scorer, "usage", None)
    if usage is None or usage.empty:
        return
    logger.info(usage.summary())
    if not quiet:
        console.print(f"[dim]{usage.summary()}[/dim]")
    if usage.calls > 1 and usage.cache_read == 0:
        # The resume and rubric are identical on every call, so this should never
        # happen. When it does, the run is paying full price to re-read the same
        # resume once per job — which is invisible without saying so.
        logger.warning(
            "No tokens were served from cache across %d scoring calls. The cached "
            "prefix is not byte-identical between jobs — check that nothing "
            "per-job has leaked into the system prompt.", usage.calls,
        )


def _usage_stats(scorer) -> dict | None:
    """The run's meters as JSON for `runs.stats_json`, or None when unmeasured.

    Same rule as `_log_usage` directly above, for the same reason: only backends
    that can read their own meters expose a tally, and a run without one records
    nothing rather than a row of zeros that would read as "this sweep was free".
    """
    usage = getattr(scorer, "usage", None)
    if usage is None or usage.empty:
        return None
    return usage.as_dict()


def _funnel_summary(stages: dict[str, int], budget: "_CallBudget") -> str:
    """One line naming what each stage passed.

    Exists because a fixed cutoff makes "nothing was shortlisted" ambiguous in a way
    top-K never did: under a ranking, something was always scored, so an empty
    shortlist could only mean the jobs were weak. Under a cutoff it can equally mean
    `min_score` is set wrong for this resume, and the two are indistinguishable from
    the outside. A zero at "above cutoff" while "reranked" is large says which."""
    line = (
        f"Funnel: {stages['gated']} gated → {stages['reranked']} reranked → "
        f"{stages['above_cutoff']} above cutoff → {stages['judged']} judged"
    )
    if budget.refused:
        line += f" ({budget.refused} hit the {budget.spent}-call cap)"
    return line


class _CallBudget:
    """The run's LLM-call fuse.

    Not the selection mechanism — `rerank.min_score` decides which jobs deserve a
    call, and this only stops a mis-set cutoff from turning that into an unbounded
    number of them. Shared across every batch in the run, so it has to be an object
    rather than a per-batch count.
    """

    def __init__(self, limit: int) -> None:
        # 0 or negative disables the cap, matching what `top_k` has always meant.
        self._limit = limit if limit and limit > 0 else None
        self.spent = 0
        self.refused = 0

    @property
    def exhausted(self) -> bool:
        return self._limit is not None and self.spent >= self._limit

    def take(self) -> bool:
        """Claim one call. False once the run is out."""
        if self.exhausted:
            self.refused += 1
            return False
        self.spent += 1
        return True


async def _process_batch(
    candidates: list[Job],
    reranker: Reranker,
    min_score: float,
    budget: _CallBudget,
    run_id: str,
    dedupe: bool = True,
    max_word_diff: int = cluster.DEFAULT_MAX_WORD_DIFF,
    exp_cfg: ExperienceConfig | None = None,
) -> tuple[list[tuple[Job, float]], list[MatchResult], dict[str, list[tuple[Job, float]]]]:
    """Rerank one batch and decide, per job, whether it is worth an LLM call.

    Returns `(winners, dropped, siblings)`:
      - `winners` are (job, logit) pairs the caller should judge now,
      - `dropped` are skip rows for everything the LLM will not see,
      - `siblings` maps a winning representative's job_id -> the other postings in
        its cluster paired with their own scores, so the caller can copy the verdict
        across once it has one.

    **This runs per batch, and that is the whole point.** Selection used to be a
    global top-K over the pooled sweep, which meant no job could be judged until
    every job had been seen. A cutoff is a statement about one job against one
    profile, so it can be applied the moment the batch arrives — which is what makes
    the shortlist fill during the sweep instead of in its last two minutes.

    **Clustering survives the move, and it is load-bearing.** Postings group by
    employer and description, and the scraper emits one employer per queue item, so
    every member of a cluster is in this batch by construction. Grouping here still
    collapses 31 copies of one requisition into one LLM call; no global pass is
    needed for that. The thing top-K needed globally was *ranking* companies against
    each other, and nothing does that any more.

    **The experience gate runs after the cutoff, not before it.** Both are free of
    LLM cost, so the ordering is chosen for two other reasons. It keeps
    `stages["above_cutoff"]` meaning what the overview page says it means — a large
    "reached the reranker" with nothing above the cutoff is the tell for a mis-set
    `min_score`, and a second killer running ahead of it would deflate that signal
    silently. And it holds the blast radius of a wrong `candidate_years` down to jobs
    that were about to cost an LLM call anyway.

    **Rerank before cluster, not after.** `cluster.group` anchors each cluster on its
    best-scoring member, so it needs `by_id` already populated — which is also what
    lets the representative be chosen without a second pass. Reordering these two
    lines would silently make scrape order pick the representative.

    An unusable reranker (no profile, or reranking switched off) must not be gated:
    it scores everything 0.0, which is an absence of information rather than a
    verdict. Everything passes to the cap in that case, which is the old
    arbitrary-but-bounded behaviour.
    """
    if not candidates:
        return [], [], {}

    scores = await reranker.rank(candidates)
    by_id = {job.job_id: score for job, score in zip(candidates, scores)}

    # --- Group repeat requisitions so one employer cannot eat the budget --------
    if dedupe:
        clusters = cluster.group(candidates, by_id, max_word_diff=max_word_diff)
    else:
        clusters = [[job] for job in candidates]

    # `group` returns each cluster with its representative first — the anchor every
    # sibling was measured against — so there is nothing left to choose here.
    representatives: list[tuple[Job, list[Job]]] = [
        (members[0], members[1:]) for members in clusters
    ]

    # Best-first *within the batch*. This no longer decides anything — every
    # representative is measured against the cutoff on its own — but when the run is
    # close to its cap it decides who gets the last few calls, and the strongest
    # candidates should get them.
    gated = reranker.usable
    representatives.sort(key=lambda pair: by_id[pair[0].job_id], reverse=True)

    winners: list[tuple[Job, float]] = []
    dropped: list[MatchResult] = []
    siblings: dict[str, list[tuple[Job, float]]] = {}

    yoe_gated = bool(exp_cfg and exp_cfg.enabled and exp_cfg.candidate_years > 0)

    for rep, others in representatives:
        size = len(others) + 1
        score = by_id[rep.job_id]
        # Once per cluster, not once per posting: `cluster.group` keys on
        # (board_token, description), so every sibling parses to the same number.
        req = experience.parse_requirement(rep.content_text)
        yoe = req.min_years if req else None

        if gated and score < min_score:
            reason = CUTOFF_SKIP_REASON
        elif yoe_gated and not experience.meets(
            req, exp_cfg.candidate_years, exp_cfg.tolerance_years
        ):
            reason = YOE_SKIP_REASON
        elif not budget.take():
            reason = CAP_SKIP_REASON
        else:
            winners.append((rep, score))
            if others:
                siblings[rep.job_id] = [(job, by_id[job.job_id]) for job in others]
            continue

        # A whole cluster shares its representative's fate. Every member gets its own
        # row so the results CSV can still show its location and link.
        for job in (rep, *others):
            dropped.append(
                _apply_rerank_scores(
                    filtered_result(job, reason, run_id), by_id[job.job_id], size, yoe
                )
            )

    return winners, dropped, siblings


def _sibling_result(job: Job, rep: MatchResult, run_id: str, score, cluster_size: int) -> MatchResult:
    """Build the row for a posting that inherits its representative's score.

    The whole judgement is copied — score, subscores, rationales, recommendation —
    because it is the same requisition; only the identity, link and location differ.
    `cluster_representative` records where the verdict came from so nothing in the
    export looks like an independent second opinion.

    When the representative was not actually judged — a backend error, a scoring
    crash — its OWN skip reason is inherited instead of `duplicate_of_cluster`. That
    keeps the general rule intact: a job may be retired on a verdict, never on an
    error. Stamping the duplicate reason here would retire the whole cluster because
    one call failed, and `duplicate_of_cluster` is deliberately not retryable.
    """
    inherited_reason = (
        rep.skip_reason
        if rep.skip_reason in _RETRYABLE_SKIP_REASONS
        else DUPLICATE_SKIP_REASON
    )
    result = rep.model_copy(
        update={
            "job_id": job.job_id,
            "board_token": job.board_token,
            "title": job.title,
            "location": job.location.name,
            "absolute_url": str(job.absolute_url),
            "cluster_representative": rep.job_id,
            "cluster_size": cluster_size,
            "source_run_id": run_id,
            # Marked skipped so `is_shortlisted` leaves it out of the apply queue:
            # 31 copies of one requisition must not become 31 applications. The
            # inherited relevance_score and rationales are kept regardless, so the
            # row still explains itself in the results CSV, and the user can
            # apply to a specific location by hand from the link it carries.
            "skipped": True,
            "skip_reason": inherited_reason,
        }
    )
    return _apply_rerank_scores(result, score, cluster_size)


def _load_search_profile(settings) -> str:
    """The expanded 'ideal candidate' profile written by /hireshire:setup.

    Used ONLY as the reranker query. It is never shown to the scorer: it states
    transferable and inferred framing ("React -> component-based UI development"),
    and a judge reading that would credit the candidate for skills the resume does
    not actually evidence.
    """
    if not settings.search_profile_path:
        return ""
    p = paths.resolve_data(settings.search_profile_path)
    if not p.exists():
        logger.warning("search_profile_path set but not found: %s", p)
        return ""
    return p.read_text(encoding="utf-8")


async def main(
    in_queue: asyncio.Queue | None = None,
    out_queue: asyncio.Queue | None = None,
    quiet: bool = False,
    run_id: str | None = None,
    skip_llm: bool = False,
    on_job_score=None,
) -> None:
    if not quiet:
        logging.basicConfig(
            level=logging.WARNING,
            handlers=[RichHandler(show_path=False, rich_tracebacks=True)],
        )

    config = load_matcher_config()
    settings = config.settings
    effective_skip_llm = skip_llm or settings.skip_llm
    db = get_db(settings.db_path)

    # --- Determine run_id ---
    if in_queue is not None:
        if run_id is None:
            raise ValueError("run_id is required when using in_queue (orchestrator mode)")
    else:
        run_id = RunStore.latest_run(db)
        if not run_id:
            if not quiet:
                console.print("[red]No scraper runs found in the database. Run python scraper.py first.[/red]")
            return

    if not quiet:
        console.print(f"[bold]HireShire Matcher[/bold] — scoring jobs from run [cyan]{run_id}[/cyan]")

    # --- Load resume (both modes) ---
    try:
        if not settings.resume_path:
            raise FileNotFoundError(
                "No resume configured. Run /hireshire:setup to point HireShire at your resume PDF."
            )
        resume_text = extract_resume_text(paths.resolve_data(settings.resume_path))
        if not quiet:
            console.print(f"Resume loaded: [green]{settings.resume_path}[/green] ({len(resume_text)} chars)")
    except (FileNotFoundError, ValueError) as exc:
        if not quiet:
            console.print(f"[red]{exc}[/red]")
        if out_queue is not None:
            await out_queue.put(None)
        return

    # --- Load optional projects context (both modes) ---
    projects_text = ""
    if settings.projects_path:
        p = paths.resolve_data(settings.projects_path)
        if p.exists():
            projects_text = p.read_text(encoding="utf-8")
            if not quiet:
                console.print(f"Projects loaded: [green]{settings.projects_path}[/green] ({len(projects_text)} chars)")
        elif not quiet:
            console.print(f"[yellow]projects_path set but file not found: {settings.projects_path}[/yellow]")

    # --- Set up scorer and store (both modes) ---
    started_at = datetime.now(timezone.utc)
    sem = asyncio.Semaphore(settings.concurrency)
    if not effective_skip_llm:
        backend = make_backend(settings, sem)
        scorer = JobScorer(settings=settings, backend=backend)
    store = MatchStore(run_id=run_id, threshold=settings.threshold, db=db)

    seen = SeenStore(db=db)
    breaker = _ScoringBreaker()

    results: list[MatchResult] = []

    reranker = Reranker(config.funnel.rerank, _load_search_profile(settings))
    budget = _CallBudget(config.funnel.top_k)
    min_score = config.funnel.rerank.min_score

    # Per-stage counts. With selection now a fixed cutoff rather than a ranking, a
    # run that scores nothing is ambiguous — a bad market and a mis-set `min_score`
    # look identical from the outside. These are what tell them apart, so they are
    # reported whether or not anything was shortlisted.
    stages = {"gated": 0, "reranked": 0, "above_cutoff": 0, "judged": 0}

    # job_id -> bi-encoder cosine, accumulated across every gated batch. Stamped onto
    # each result on its way to the database so the number that opened the funnel is
    # recoverable next to the ones that closed it.
    encoder_scores: dict[str, float] = {}

    async def persist(result: MatchResult) -> MatchResult:
        if result.encoder_score is None:
            result.encoder_score = encoder_scores.get(result.job_id)
        await store.append_result(result)
        return result

    async def score_one(job, score=None, cluster_size: int = 1) -> MatchResult:
        def _failed(reason: str) -> MatchResult:
            return MatchResult(
                job_id=job.job_id,
                board_token=job.board_token,
                title=job.title,
                location=job.location.name,
                absolute_url=str(job.absolute_url),
                relevance_score=0,
                match_reasons=[],
                disqualifiers=[],
                recommend=False,
                skipped=True,
                skip_reason=reason,
                scored_at=datetime.now(timezone.utc),
                source_run_id=run_id,
            )

        if breaker.tripped:
            # Don't call a backend already known to be failing. Recorded rather than
            # dropped so the user can see how much of the budget went unspent.
            result = _failed("backend_unavailable")
        else:
            if on_job_score:
                on_job_score(job.board_token, job.title)
            try:
                result = await scorer.score(job, resume_text, run_id, projects_text)
            except Exception as exc:
                logger.exception("Unexpected error scoring job %s/%s", job.board_token, job.job_id)
                breaker.last_error = str(exc)
                result = _failed("unexpected_error")
            else:
                breaker.last_error = scorer.last_error or breaker.last_error
            breaker.record(result)
        _apply_rerank_scores(result, score, cluster_size, _yoe_required(job))
        await persist(result)
        # In queue mode, forward shortlisted (result, job) pairs immediately
        if out_queue is not None and is_shortlisted(result, settings.threshold):
            await out_queue.put((result, job))
        return result

    async def score_cluster(job, score, siblings: dict[str, list[Job]]) -> list[MatchResult]:
        """Score a cluster representative, then copy its verdict to the rest.

        One LLM call covers every repeat of the requisition. Siblings are persisted
        so they appear in the results CSV with their own location and link, but
        are not forwarded to the apply queue — see `_sibling_result`."""
        others = siblings.get(job.job_id, [])
        size = len(others) + 1
        rep = await score_one(job, score, cluster_size=size)
        return [rep, *await _emit_siblings(rep, others, size)]

    async def _emit_siblings(rep: MatchResult, others, size: int) -> list[MatchResult]:
        """Persist the copies that inherit `rep`'s verdict.

        Every winning cluster's siblings must come through here. A sibling that is
        never emitted has no database row, never reaches the results CSV and is
        never marked seen — it simply disappears from the run, which is the one
        outcome clustering is supposed to make impossible."""
        out = []
        for sib, sib_score in others:
            r = _sibling_result(sib, rep, run_id, sib_score, size)
            await persist(r)
            out.append(r)
        return out

    async def passthrough_cluster(job, score, siblings) -> list[MatchResult]:
        """skip_llm equivalent of `score_cluster`.

        Without this the siblings of a winning cluster would be dropped on the floor
        whenever scoring is disabled: they are deliberately absent from
        `_process_batch`'s `dropped` list, because normally the scorer emits them."""
        others = siblings.get(job.job_id, [])
        size = len(others) + 1
        rep = _passthrough_result(job, run_id, score)
        rep.cluster_size = size
        await persist(rep)
        return [rep, *await _emit_siblings(rep, others, size)]

    # The funnel is the matcher-entry relevance gate (code filter + encoder + detail
    # hydration). When disabled, fall back to the plain code title filter. It owns an
    # http client for detail hydration, so keep it open across the whole run via the
    # AsyncExitStack below. `gate(jobs) -> (to_score, filtered_results)` is the drop-in
    # both modes call in place of apply_title_filter.
    funnel = Funnel(config.funnel, config.title_filter, run_id) if config.funnel.enabled else None

    async def gate(job_list):
        if funnel is not None:
            to_score, filtered, scores = await funnel.process(job_list)
            encoder_scores.update(scores)
        else:
            to_score, filtered = apply_title_filter(job_list, config.title_filter, run_id)
        await _persist_hydrated_details(db, run_id, to_score)
        return to_score, filtered

    async with AsyncExitStack() as _stack:
        if funnel is not None:
            await _stack.enter_async_context(funnel)

        # =========================================================
        # Queue mode: consume company batches from in_queue
        # =========================================================
        if in_queue is not None:
            # Every stage runs per batch now — gate, hydrate, rerank, judge — so a
            # job can be scraped and shortlisted without waiting for the sweep to
            # end. This used to pool candidates until the sentinel because top-K was
            # a decision across the whole run; a cutoff is a decision about one job,
            # so there is nothing left to wait for.
            try:
                while True:
                    item = await in_queue.get()
                    if item is None:
                        break
                    board_token, batch_jobs = item
                    logger.info("Gating batch: %s (%d jobs)", board_token, len(batch_jobs))
                    unseen = [j for j in batch_jobs if j.job_id not in seen]
                    if len(unseen) < len(batch_jobs):
                        logger.info(
                            "Dedup: skipping %d already-seen jobs from %s",
                            len(batch_jobs) - len(unseen), board_token,
                        )
                    to_score, title_filtered = await gate(unseen)
                    results.extend(title_filtered)
                    stages["gated"] += len(to_score)

                    winners, dropped, siblings = await _process_batch(
                        to_score, reranker, min_score, budget, run_id,
                        config.funnel.dedupe.enabled,
                        config.funnel.dedupe.max_word_diff,
                        config.funnel.experience,
                    )
                    stages["reranked"] += len(to_score)
                    # YoE drops cleared the cutoff — they are counted here for the
                    # same reason cap drops are, or the "above cutoff" number stops
                    # answering the question the report asks it.
                    stages["above_cutoff"] += len(winners) + sum(
                        1 for r in dropped
                        if r.skip_reason in (CAP_SKIP_REASON, YOE_SKIP_REASON)
                    )
                    stages["judged"] += len(winners)

                    results.extend(dropped)
                    # Persist cutoff and cap drops individually. `finalise` records
                    # only summary stats, so without this the user cannot see what
                    # the cutoff cost them — and "lower min_score to score them"
                    # would be unverifiable. Bounded by the candidate pool, unlike
                    # the title-gate rejections, which stay stats-only because there
                    # can be tens of thousands.
                    for r in dropped:
                        await persist(r)

                    if effective_skip_llm:
                        for j, score in winners:
                            if on_job_score:
                                on_job_score(j.board_token, j.title)
                            group = await passthrough_cluster(j, score, siblings)
                            if out_queue is not None:
                                # Only the representative is queued for applying —
                                # the siblings are the same requisition elsewhere.
                                await out_queue.put((group[0], j))
                            results.extend(group)
                    else:
                        for group in await asyncio.gather(
                            *[score_cluster(j, s, siblings) for j, s in winners]
                        ):
                            results.extend(group)
            except Exception:
                logger.exception("Matcher queue loop failed")
            finally:
                for r in results:
                    if r.skip_reason not in _RETRYABLE_SKIP_REASONS:
                        seen.add(r.job_id)
                seen.save()
                shortlisted = [r for r in results if is_shortlisted(r, settings.threshold)]
                rejected = [r for r in results if not is_shortlisted(r, settings.threshold)]
                shortlisted.sort(key=lambda r: (r.relevance_score or 0), reverse=True)
                store.finalise(shortlisted, rejected, started_at, settings.threshold,
                               settings.model, len(results),
                               _usage_stats(scorer if not effective_skip_llm else None))
                if breaker.tripped:
                    # Queue mode is what the monitor runs, where a "0 shortlisted"
                    # line would otherwise be the only trace of a dead backend.
                    logger.error("Matcher: %s", breaker.summary())
                    if not quiet:
                        console.print(f"[red]{breaker.summary()}[/red]")
                logger.info(_funnel_summary(stages, budget))
                _log_usage(scorer if not effective_skip_llm else None, quiet)
                logger.info(
                    "Matcher done: %d shortlisted, %d rejected (run %s)",
                    len(shortlisted), len(rejected), run_id,
                )
                if out_queue is not None:
                    await out_queue.put(None)  # sentinel — always sent
            return

        # =========================================================
        # Standalone mode: load jobs from the database (existing behaviour)
        # =========================================================
        jobs = load_jobs(run_id, db=db)
        if not jobs:
            if not quiet:
                console.print("[yellow]No jobs found in the latest run. Run python scraper.py first.[/yellow]")
            return

        provider = settings.provider or os.environ.get("LLM_PROVIDER", "claude_code")
        if not quiet:
            console.print(
                f"Scoring [bold]{len(jobs)}[/bold] jobs with [bold]{provider}/{settings.model}[/bold] "
                f"(threshold: {settings.threshold}/100)\n"
            )

        prior_results = store.load_progress()
        scored_ids = {r.job_id for r in prior_results}
        if prior_results and not quiet:
            console.print(
                f"[yellow]Resuming partial run — {len(prior_results)} already scored, "
                f"{len(jobs) - len(scored_ids)} remaining.[/yellow]\n"
            )

        not_in_run = [j for j in jobs if j.job_id not in scored_ids]
        unscored = [j for j in not_in_run if j.job_id not in seen]
        dedup_skipped = len(not_in_run) - len(unscored)
        if dedup_skipped > 0 and not quiet:
            console.print(f"[yellow]Dedup: {dedup_skipped} jobs skipped (already scored in a previous run)[/yellow]\n")
        gated, title_filtered = await gate(unscored)
        # One batch covering the whole run. Standalone mode reads a finished scrape
        # out of the database, so there is nothing to overlap with and no reason to
        # chunk it — but it goes through the same helper as the streaming path so the
        # two cannot drift apart in what they gate, cluster or retire.
        winners, cut_dropped, siblings = await _process_batch(
            gated, reranker, min_score, budget, run_id, config.funnel.dedupe.enabled,
            config.funnel.dedupe.max_word_diff, config.funnel.experience,
        )
        stages["gated"] = stages["reranked"] = len(gated)
        # See the queue-mode counter: a YoE drop happened above the cutoff.
        stages["above_cutoff"] = len(winners) + budget.refused + sum(
            1 for r in cut_dropped if r.skip_reason == YOE_SKIP_REASON
        )
        stages["judged"] = len(winners)
        jobs_to_score = winners
        for r in cut_dropped:
            await persist(r)
        if not quiet:
            console.print(
                f"Funnel: [yellow]{len(title_filtered)} filtered out[/yellow], "
                f"[green]{len(winners)} sent to LLM scoring[/green]"
                + (
                    f", [yellow]{len(cut_dropped)} below the {min_score} cutoff or "
                    f"over the {config.funnel.top_k}-call cap[/yellow]"
                    if cut_dropped else ""
                )
                + "\n"
            )

        results = list(prior_results) + title_filtered + cut_dropped

        prog_ctx = (
            Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                console=console,
            )
            if not quiet
            else _NoopProgress()
        )

        with prog_ctx as progress:
            task = progress.add_task("Scoring jobs...", total=len(jobs_to_score))

            if effective_skip_llm:
                for j, score in jobs_to_score:
                    results.extend(await passthrough_cluster(j, score, siblings))
                    progress.advance(task)
            else:
                async def score_cluster_p(job, score):
                    try:
                        return await score_cluster(job, score, siblings)
                    finally:
                        progress.advance(task)

                for group in await asyncio.gather(
                    *[score_cluster_p(j, s) for j, s in jobs_to_score]
                ):
                    results += group

        shortlisted = [r for r in results if is_shortlisted(r, settings.threshold)]
        rejected = [r for r in results if not is_shortlisted(r, settings.threshold)]
        shortlisted.sort(key=lambda r: (r.relevance_score or 0), reverse=True)
        store.finalise(shortlisted, rejected, started_at, settings.threshold,
                       settings.model, len(jobs),
                       _usage_stats(scorer if not effective_skip_llm else None))
        for r in results:
            # Budget drops stay eligible for a later run — see _RETRYABLE_SKIP_REASONS.
            if r.skip_reason not in _RETRYABLE_SKIP_REASONS:
                seen.add(r.job_id)
        seen.save()

        if breaker.tripped:
            logger.error("Matcher: %s", breaker.summary())

        if not quiet:
            console.print()
            if breaker.tripped:
                console.print(f"[red]{breaker.summary()}[/red]\n")
            if shortlisted:
                table = Table(title=f"Shortlisted Jobs (score >= {settings.threshold})", show_lines=True)
                table.add_column("Score", style="bold green", width=7)
                table.add_column("Title", style="bold")
                table.add_column("Company", style="cyan")
                table.add_column("Location")
                table.add_column("Recommend", width=10)
                for r in shortlisted:
                    table.add_row(
                        "—" if r.relevance_score is None else str(r.relevance_score),
                        r.title,
                        r.board_token,
                        r.location,
                        "[green]Yes[/green]" if r.recommend else "[yellow]Maybe[/yellow]",
                    )
                console.print(table)
            elif not breaker.tripped:
                # Never say "nothing met the threshold" when nothing was scored.
                console.print("[yellow]No jobs met the threshold. Try lowering it in config/matcher.yaml.[/yellow]")

            _FUNNEL_REASONS = ("title_excluded", "title_no_include_match", "title_low_relevance")
            # Both rerank drops, plus the pre-cutoff reason so a resumed run that
            # loaded older rows still counts them here rather than as "skipped".
            _RERANK_REASONS = (CUTOFF_SKIP_REASON, CAP_SKIP_REASON, BUDGET_SKIP_REASON)
            title_filtered_count = sum(1 for r in results if r.skip_reason in _FUNNEL_REASONS)
            cutoff_count = sum(1 for r in results if r.skip_reason == CUTOFF_SKIP_REASON)
            cap_count = sum(1 for r in results if r.skip_reason in (CAP_SKIP_REASON, BUDGET_SKIP_REASON))
            rerank_count = cutoff_count + cap_count
            other_skipped_count = sum(
                1 for r in results
                if r.skipped and r.skip_reason not in _FUNNEL_REASONS
                and r.skip_reason not in _RERANK_REASONS
            )
            llm_skipped_count = sum(1 for r in results if r.skip_reason == "llm_skipped")
            console.print(
                f"\n[bold]{len(shortlisted)} shortlisted[/bold], "
                f"{len(rejected) - title_filtered_count - rerank_count - other_skipped_count} rejected by LLM, "
                f"{title_filtered_count} funnel-filtered, "
                + (f"{cutoff_count} below the cutoff (lower min_score to score them), " if cutoff_count else "")
                + (f"{cap_count} over the call cap (raise top_k to score them), " if cap_count else "")
                + (f"{llm_skipped_count} LLM-skipped (auto-shortlisted), " if llm_skipped_count else "")
                + f"{other_skipped_count} skipped"
            )
            console.print(_funnel_summary(stages, budget))
        _log_usage(scorer if not effective_skip_llm else None, quiet)


if __name__ == "__main__":
    asyncio.run(main())
