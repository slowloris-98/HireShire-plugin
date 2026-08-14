"""
Scores the latest scrape run's jobs against the resume.

Cheap title gates and detail hydration run first (see hireshire/funnel/), then a
cross-encoder ranks the survivors and only the top `funnel.top_k` are sent to the
LLM — so scoring cost is bounded by a budget rather than by wherever a similarity
threshold lands.

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
from hireshire.funnel import cluster
from hireshire.funnel.detail_fetcher import DETAIL_SOURCES
from hireshire.funnel.funnel import Funnel
from hireshire.funnel.rerank import Reranker
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

# Jobs that cleared every gate but lost the top-K race. Distinct from the title
# gates because it is a *budget* outcome, not a relevance judgement — the job may
# be perfectly good and simply ranked 101st. That distinction matters in two
# places: the run summary, and the seen-jobs write below.
BUDGET_SKIP_REASON = "rerank_below_top_k"

# A repeat posting of a requisition whose representative WAS scored. Distinct from a
# budget drop because it is a verdict, not a deferral: the job has been judged, just
# by proxy, and it carries the representative's score. It must therefore stay OUT of
# _RETRYABLE_SKIP_REASONS below — re-queuing it next sweep would spend the budget
# re-deriving a score it already has.
DUPLICATE_SKIP_REASON = "duplicate_of_cluster"

# Reasons that must NOT retire a job_id into seen_jobs. A budget drop is the whole
# point here: max_age_hours=24 with a 4h poll means a job resurfaces in ~6 sweeps,
# and one that just missed the cut should get another shot when it is up against
# weaker competition. Marking it seen would retire it permanently on the strength
# of one crowded run.
#
# The general rule: a job may be retired on a *verdict*, never on an *error*. The
# scoring-failure reasons below are here because they once weren't — a broken
# `--json-schema` argument failed all 100 scoring calls in a run and permanently
# retired every one of them, so fixing the flag would not have brought them back.
# `no_content_text` and `llm_skipped` stay retiring: those are facts about the job.
_RETRYABLE_SKIP_REASONS = {BUDGET_SKIP_REASON} | set(SCORING_ERROR_SKIP_REASONS)


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


def _apply_rerank_scores(result: MatchResult, score, cluster_size: int = 1) -> MatchResult:
    """Copy a RerankScores onto a result row, keeping the two stages separate."""
    if score is not None:
        result.rerank_score_wide = score.wide
        result.rerank_score = score.refined
        result.rerank_stage = score.stage
    result.cluster_size = cluster_size
    return result


async def _spend_budget(
    candidates: list[Job],
    reranker: Reranker,
    top_k: int,
    run_id: str,
    dedupe: bool = True,
) -> tuple[list[tuple[Job, object]], list[MatchResult], dict[str, list[Job]]]:
    """Rank the whole candidate pool and hand the LLM only what the budget allows.

    Returns `(winners, dropped, siblings)`:
      - `winners` are (job, RerankScores) pairs in descending rank order,
      - `dropped` are skip rows for everything the LLM will not see,
      - `siblings` maps a winning representative's job_id -> the other postings in
        its cluster paired with their own rerank scores, so the caller can copy the
        verdict across once it has one.

    This is deliberately a *global* decision, which is why it runs once over the
    whole sweep rather than per company batch: a batch is one employer's postings,
    so ranking within it would compare a company against itself and spend the
    budget on whoever happened to be scraped first.

    Ordering uses `RerankScores.sort_key`, never the raw float — the wide and
    refined passes are different models on different logit scales and sorting them
    together would silently produce a wrong ranking.
    """
    if not candidates:
        return [], [], {}

    scores = await reranker.rank(candidates)
    by_id = {job.job_id: score for job, score in zip(candidates, scores)}

    # --- Group repeat requisitions so one employer cannot eat the budget --------
    if dedupe:
        clusters = list(cluster.group(candidates).values())
    else:
        clusters = [[job] for job in candidates]

    representatives: list[tuple[Job, list[Job]]] = []
    for members in clusters:
        rep = cluster.pick_representative(members, by_id) if len(members) > 1 else members[0]
        representatives.append((rep, [m for m in members if m.job_id != rep.job_id]))

    # A cluster competes on its representative's score, which is its best member's.
    representatives.sort(key=lambda pair: by_id[pair[0].job_id].sort_key, reverse=True)

    if top_k and top_k > 0 and len(representatives) > top_k:
        winning, losing = representatives[:top_k], representatives[top_k:]
    else:
        winning, losing = representatives, []

    dropped: list[MatchResult] = []
    siblings: dict[str, list[tuple[Job, object]]] = {}

    for rep, others in winning:
        if others:
            siblings[rep.job_id] = [(job, by_id[job.job_id]) for job in others]

    # Members of losing clusters are budget drops: retryable, so a job that lost one
    # crowded sweep is up for reconsideration in the next.
    for rep, others in losing:
        for job in [rep, *others]:
            dropped.append(
                _apply_rerank_scores(
                    filtered_result(job, BUDGET_SKIP_REASON, run_id),
                    by_id[job.job_id],
                    cluster_size=len(others) + 1,
                )
            )

    winners = [(rep, by_id[rep.job_id]) for rep, _ in winning]
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
            # row still explains itself in the all-jobs export, and the user can
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
    top_k = config.funnel.top_k

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
        _apply_rerank_scores(result, score, cluster_size)
        await persist(result)
        # In queue mode, forward shortlisted (result, job) pairs immediately
        if out_queue is not None and is_shortlisted(result, settings.threshold):
            await out_queue.put((result, job))
        return result

    async def score_cluster(job, score, siblings: dict[str, list[Job]]) -> list[MatchResult]:
        """Score a cluster representative, then copy its verdict to the rest.

        One LLM call covers every repeat of the requisition. Siblings are persisted
        so they appear in the all-jobs export with their own location and link, but
        are not forwarded to the apply queue — see `_sibling_result`."""
        others = siblings.get(job.job_id, [])
        size = len(others) + 1
        rep = await score_one(job, score, cluster_size=size)
        return [rep, *await _emit_siblings(rep, others, size)]

    async def _emit_siblings(rep: MatchResult, others, size: int) -> list[MatchResult]:
        """Persist the copies that inherit `rep`'s verdict.

        Every winning cluster's siblings must come through here. A sibling that is
        never emitted has no database row, never reaches the all-jobs export and is
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
        `_spend_budget`'s `dropped` list, because normally the scorer emits them."""
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
            # The cheap gates and detail hydration run per batch, overlapping with
            # the sweep. Scoring cannot: top-K is a global decision, so survivors
            # pool here and the budget is spent once the sentinel arrives. That
            # defers scoring to the end of the run, which costs little — the sweep
            # is rate-limit-bound at ~20 min while scoring top_k jobs is ~1 min.
            candidates: list[Job] = []
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
                    candidates.extend(to_score)

                winners, dropped, siblings = await _spend_budget(
                    candidates, reranker, top_k, run_id, config.funnel.dedupe.enabled
                )
                results.extend(dropped)
                # Persist budget drops individually. `finalise` only records summary
                # stats, so without this the user has no way to see what the budget
                # cost them — and "raise top_k to score them" would be unverifiable.
                # Bounded by the candidate pool, unlike the title-gate rejections,
                # which stay stats-only because there can be tens of thousands.
                for r in dropped:
                    await persist(r)
                logger.info(
                    "Budget: %d candidates → %d clusters scored, %d over budget",
                    len(candidates), len(winners), len(dropped),
                )

                if effective_skip_llm:
                    for j, score in winners:
                        if on_job_score:
                            on_job_score(j.board_token, j.title)
                        group = await passthrough_cluster(j, score, siblings)
                        if out_queue is not None:
                            # Only the representative is queued for applying — the
                            # siblings are the same requisition in another location.
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
                store.finalise(shortlisted, rejected, started_at, settings.threshold, settings.model, len(results))
                if breaker.tripped:
                    # Queue mode is what the monitor runs, where a "0 shortlisted"
                    # line would otherwise be the only trace of a dead backend.
                    logger.error("Matcher: %s", breaker.summary())
                    if not quiet:
                        console.print(f"[red]{breaker.summary()}[/red]")
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
        winners, budget_dropped, siblings = await _spend_budget(
            gated, reranker, top_k, run_id, config.funnel.dedupe.enabled
        )
        jobs_to_score = winners
        for r in budget_dropped:
            await persist(r)
        if not quiet:
            console.print(
                f"Funnel: [yellow]{len(title_filtered)} filtered out[/yellow], "
                f"[green]{len(winners)} sent to LLM scoring[/green]"
                + (
                    f", [yellow]{len(budget_dropped)} over the top-{top_k} budget[/yellow]"
                    if budget_dropped else ""
                )
                + "\n"
            )

        results = list(prior_results) + title_filtered + budget_dropped

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
        store.finalise(shortlisted, rejected, started_at, settings.threshold, settings.model, len(jobs))
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
            title_filtered_count = sum(1 for r in results if r.skip_reason in _FUNNEL_REASONS)
            budget_count = sum(1 for r in results if r.skip_reason == BUDGET_SKIP_REASON)
            other_skipped_count = sum(
                1 for r in results
                if r.skipped and r.skip_reason not in _FUNNEL_REASONS
                and r.skip_reason != BUDGET_SKIP_REASON
            )
            llm_skipped_count = sum(1 for r in results if r.skip_reason == "llm_skipped")
            console.print(
                f"\n[bold]{len(shortlisted)} shortlisted[/bold], "
                f"{len(rejected) - title_filtered_count - budget_count - other_skipped_count} rejected by LLM, "
                f"{title_filtered_count} funnel-filtered, "
                + (f"{budget_count} over budget (raise top_k to score them), " if budget_count else "")
                + (f"{llm_skipped_count} LLM-skipped (auto-shortlisted), " if llm_skipped_count else "")
                + f"{other_skipped_count} skipped"
            )


if __name__ == "__main__":
    asyncio.run(main())
