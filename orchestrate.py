"""
HireShire pipeline orchestrator — runs Scraper → Matcher over asyncio queues,
then repeats on a schedule.

    python orchestrate.py              # wait 4h, then run; repeat every 4h
    python orchestrate.py --now        # run immediately, then every 4h
    python orchestrate.py --once       # run exactly once, no scheduling
    python orchestrate.py --interval 2 # every 2 hours instead of 4
    python orchestrate.py --no-matcher # scraper only (no scoring)
    python orchestrate.py --apply      # apply to each job as it is shortlisted
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

import matcher
import scraper
from hireshire import paths, reporting
from hireshire.results_export import results_name, write_results_csv
from hireshire.storage.db import PHASE_PIPELINE, get_db

if TYPE_CHECKING:
    from hireshire.applier.config import ApplierSettings

load_dotenv()

console = Console()
logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    log_dir = paths.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "orchestrate.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            RichHandler(console=console, show_path=False, show_time=False, rich_tracebacks=True),
            file_handler,
        ],
    )
    logging.getLogger("hireshire").setLevel(logging.INFO)
    # Suppress noisy third-party loggers
    for name in ("httpx", "httpcore", "playwright", "browser_use"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _collect_results(in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
    """Turn shortlisted (MatchResult, Job) pairs into flat result records.

    `location` and `posted_at` come off the Job, which is already in hand — the
    MatchResult carries neither in a form the CSV wants.
    """
    while True:
        item = await in_q.get()
        if item is None:
            break
        match_result, job = item
        await out_q.put({
            "job_id": job.job_id,
            "title": job.title,
            "company": job.board_token,
            "location": job.location.name,
            "posted_at": job.updated_at.isoformat(),
            "job_url": str(match_result.absolute_url),
            "relevance_score": match_result.relevance_score,
            "rerank_score": match_result.rerank_score,
            "rerank_score_wide": match_result.rerank_score_wide,
            "encoder_score": match_result.encoder_score,
            # >1 means this requisition was posted for several locations. Only this
            # representative is queued for applying; the rest are in the results
            # CSV with their own links.
            "cluster_size": match_result.cluster_size,
            "found_at": datetime.now(timezone.utc).isoformat(),
        })
    await out_q.put(None)


def _load_apply_inputs() -> "tuple[ApplierSettings, str] | None":
    """The applier's settings and the resume text, or None if either cannot be loaded.

    Loaded once per sweep, before the queues start. A failure turns applying off for
    this sweep rather than failing it: the scrape and the scoring are worth having
    whether or not anything gets applied to.
    """
    from hireshire.applier.config import load_applier_config
    from hireshire.matcher.resume import extract_resume_text

    try:
        settings = load_applier_config().settings
        resume_text = extract_resume_text(paths.resolve_data(settings.resume_path))
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Applier: could not load its settings or the resume — "
                         "not applying this sweep")
        return None
    return settings, resume_text


def _run_stamp(now: datetime | None = None) -> str:
    """The run's display stamp, used for the results folder and the files in it.

    LOCAL time, unlike `run_id`, because this one is read by a human browsing
    their own folder — a run at 14:30 filed under `090005` would be a bug report.
    It is never a key: `run_id` stays UTC so it is monotonic and cannot collide
    when the clock goes back an hour at the end of DST.
    """
    now = now or datetime.now(timezone.utc)
    return now.astimezone().strftime("%Y-%m-%d_%H%M%S")


def _json_name(stamp: str) -> str:
    return f"{stamp}_results.json"


async def _track_results(
    q: asyncio.Queue, results_dir: Path, run_id: str, stamp: str, quiet: bool = False,
    apply_q: asyncio.Queue | None = None,
) -> None:
    """Persist each pipeline result to the DB, O(1) per row, then hand it to the applier.

    `apply_q` is how applying streams: a shortlisted job reaches the apply worker the
    moment it is recorded, not after the sweep. It is recorded *first* so the job is
    in the database before a browser goes anywhere near it. Its `None` sentinel is
    sent in a `finally`, because a worker left waiting would hold the whole sweep's
    `gather` open forever.

    Only the DB. The CSV used to be appended here as rows arrived, which meant a
    file in processing order that also had to survive being open in Excel — a
    routine event, since it sits in a folder the user actively browses. It is now
    written once from the database at the end of the run, because it is sorted
    across the whole sweep and no row's position is known until every row exists.
    `_write_run_outputs` does that in a `finally`, so a sweep that dies part-way
    still leaves the CSV for everything it judged.
    """
    db = get_db()
    try:
        while True:
            record = await q.get()
            if record is None:
                break
            await asyncio.to_thread(db.record_pipeline_result, run_id, record)
            logger.info("Tracked result: %s — %s", record["company"], record["title"])
            if apply_q is not None:
                await apply_q.put(record)
                # The applier bar's denominator: representatives only, because
                # siblings never reach this queue and would hold the bar short.
                await asyncio.to_thread(db.bump_progress, run_id, apply_queued=1)
    finally:
        if apply_q is not None:
            await apply_q.put(None)


async def _write_run_outputs(run_id: str, results_dir: Path, stamp: str,
                             complete: bool = True) -> int:
    """Write every file the run produces, and return the shortlist row count.

    Split out of `_finalise_pipeline` so it can run from a `finally`. Everything
    it exports is already committed to a WAL database, so on a sweep that died
    half-scored these are honest partial exports — and that matters: before the
    split, a crash left no CSV, no JSON, and a `last_run.json` still pointing at
    the previous run, so fifteen minutes of real scoring was reachable only by
    opening the database by hand.

    `complete` is recorded rather than inferred. A partial shortlist is still a
    real shortlist — every row in it was genuinely judged and cleared the
    threshold — so the file is written either way and the flag says which it is.
    """
    db = get_db()
    rows = await asyncio.to_thread(db.load_pipeline_results, run_id)

    # Every row that reached the funnel, ordered by the database and re-sorted by
    # the exporter, which is the only place that can tell a real 0 from the
    # placeholder a budget drop carries.
    all_rows = await asyncio.to_thread(db.load_all_matches, run_id)
    # `applied` is keyed on the job alone and has no `run_id`, so this is read once
    # for the whole file rather than per row.
    applied_ids = await asyncio.to_thread(db.applied_ids)
    csv_path = await asyncio.to_thread(
        write_results_csv, all_rows, results_dir / results_name(stamp), applied_ids
    )

    json_path = results_dir / _json_name(stamp)
    try:
        json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError as exc:
        # Everything of value — the CSV and every DB row — is already written by
        # the time we get here, so letting this escape would report a fully
        # successful run as a failure.
        logger.error("Could not write %s: %s", json_path, exc)

    # A fixed pointer to the newest run, so /apply does not have to guess where
    # the results root is or which filename generation a directory holds. Metadata
    # about a run, not a result of it, so it belongs in the data dir.
    report_targets = reporting.report_paths(results_dir, stamp)
    try:
        paths.LAST_RUN_PATH.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "stamp": stamp,
                    "results_dir": str(results_dir),
                    # One CSV, every job that reached the funnel, best first. Blank
                    # rather than None when it could not be written, so a reader
                    # never builds a path out of the string "None".
                    "csv": str(csv_path) if csv_path else "",
                    "json": str(json_path),
                    # The two overview pages. `overview_html` spans every sweep and
                    # sits at the results root, `run_overview_html` covers this one
                    # and sits beside its CSV. Local files — nothing is published.
                    "overview_html": str(report_targets["overview"]),
                    "run_overview_html": str(report_targets["run_overview"]),
                    # False when the sweep did not reach the end. The files above
                    # are still real, just partial, and /apply reads `json` either
                    # way — this is how a reader tells the difference.
                    "complete": complete,
                    "total_results": len(rows),
                    "total_jobs_considered": len(all_rows),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write %s: %s", paths.LAST_RUN_PATH, exc)

    return len(rows)


async def _finalise_pipeline(run_id: str, results_dir: Path, started_at: str,
                             stamp: str, total_results: int = 0,
                             complete: bool = True) -> None:
    """Record the pipeline run's summary row, then refresh the reports one last time.

    Runs whether or not the sweep finished, and writes the `runs` row either way.
    That row is the reports' only signal for "is this sweep still going", so a
    crashed run without one leaves both pages meta-refreshing forever with their
    elapsed figure climbing on a process that is dead. `completed` is what keeps
    that honest without pretending the run succeeded.
    """
    db = get_db()
    await asyncio.to_thread(
        db.finalise_run, run_id, PHASE_PIPELINE, started_at, None,
        {"total_results": total_results, "completed": complete},
    )

    # Last, and deliberately after `finalise_run`: the reports read the pipeline's
    # own `runs` row to decide whether the sweep is still going, and that is what
    # arms their meta refresh. Refreshing before this line would leave a finished
    # run reloading itself forever.
    await asyncio.to_thread(reporting.refresh, run_id, results_dir, stamp, True)


def _write_current_run(run_id: str, stamp: str, results_dir: Path, started_at: str) -> None:
    """Record the sweep in flight, for `finalise_abandoned_runs` to find if it is killed.

    The folder cannot be re-derived afterwards: `make_run_dir` may have fallen back
    to the data dir. Losing this file only costs that fallback, so it never raises.
    """
    try:
        paths.CURRENT_RUN_PATH.write_text(
            json.dumps({"run_id": run_id, "stamp": stamp,
                        "results_dir": str(results_dir), "started_at": started_at}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write %s: %s", paths.CURRENT_RUN_PATH, exc)


def _read_current_run() -> dict:
    try:
        return json.loads(paths.CURRENT_RUN_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _clear_current_run(run_id: str) -> None:
    """Remove the marker, but only if it still names this run."""
    if _read_current_run().get("run_id") != run_id:
        return
    try:
        paths.CURRENT_RUN_PATH.unlink()
    except OSError:
        pass


def _run_started_at(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def finalise_abandoned_runs() -> list[str]:
    """Close out every sweep that was killed before its `finally` could, and return
    their stamps.

    A forced kill — `--stop` is `taskkill /F` on Windows, and a killed shell task is
    no gentler — runs no `finally`, so the pipeline `runs` row is never written and
    both dashboards keep their `running` chip and meta refresh forever. This writes
    what that `finally` would have, in the same order: the outputs, then the row
    (`completed: False, stopped: True`), then a final refresh.

    Only the **newest** orphan gets `_write_run_outputs`, because that is what writes
    `last_run.json`, and repointing it at an older run would be a lie. Older orphans
    get the row and the refresh only.

    The caller must have established that no sweep is running: the in-flight one is
    an orphan by this definition, and finalising it would disarm its live page.
    Never raises.
    """
    try:
        db = get_db()
        orphans = await asyncio.to_thread(db.abandoned_runs)
    except Exception:  # noqa: BLE001
        logger.exception("Could not look for abandoned runs")
        return []

    marker = _read_current_run()
    done: list[str] = []
    for i, orphan in enumerate(orphans):
        run_id = orphan["run_id"]
        try:
            began = _run_started_at(run_id)
            if marker.get("run_id") == run_id and marker.get("stamp"):
                stamp = marker["stamp"]
                results_dir = Path(marker.get("results_dir") or paths.results_root() / stamp)
                started_at = marker.get("started_at") or (began.isoformat() if began else None)
            else:
                stamp = _run_stamp(began) if began else run_id
                results_dir = paths.results_root() / stamp
                started_at = began.isoformat() if began else None

            total_results = 0
            if i == len(orphans) - 1:
                results_dir.mkdir(parents=True, exist_ok=True)
                total_results = await _write_run_outputs(
                    run_id, results_dir, stamp, complete=False
                )
            await asyncio.to_thread(
                db.finalise_run, run_id, PHASE_PIPELINE, started_at,
                orphan.get("updated_at"),
                {"total_results": total_results, "completed": False, "stopped": True},
            )
            # After the row, as in `_finalise_pipeline`: the row is what disarms the
            # pages' meta refresh.
            await asyncio.to_thread(reporting.refresh, run_id, results_dir, stamp, True)
            _clear_current_run(run_id)
            logger.info("Finalised stopped run %s", run_id)
            done.append(stamp)
        except Exception:  # noqa: BLE001
            logger.exception("Could not finalise stopped run %s", run_id)
    return done


# Quarters, not a percentage every company. Anyone tailing the log for progress
# gets a readable line per quarter rather than a flood — so there are four of them for a sweep that visits ~10,000 employers, and
# they are worded to be readable on their own.
_SCRAPE_MILESTONES = (25, 50, 75)
REPORT_MILESTONE_PREFIX = "Sweep progress:"


def _log_scrape_milestone(done: int, total: int, seen: set[int]) -> None:
    """Log a one-line progress marker as the sweep crosses each quarter."""
    if total <= 0:
        return
    pct = 100 * done // total
    for mark in _SCRAPE_MILESTONES:
        if pct >= mark and mark not in seen:
            seen.add(mark)
            logger.info(
                "%s %d%% — %d of %d employers swept", REPORT_MILESTONE_PREFIX,
                mark, done, total,
            )


# How often the reports are *offered* a rebuild while a sweep is in flight.
#
# Deliberately half the throttle floor, and derived from it rather than written out:
# `reporting.refresh` stays the single authority on cadence and this only guarantees
# the floor is actually exercised. Two numbers maintained independently would drift,
# and the failure would be silent — the same argument that keeps interpreter and
# directory discovery in one place each.
_REPORT_TICK_S = reporting.MIN_INTERVAL_S / 2


async def _tick_reports(schedule: Callable[[], None]) -> None:
    """Offer the reports a rebuild on a clock, for as long as this task lives.

    This exists because refreshing on pipeline *events* does not survive contact with
    the funnel. The two callbacks that used to drive it both stop: `on_company_start`
    ends with the scrape, and `on_job_score` fires only on an LLM score, which
    `top_k` caps. On the sweep that prompted this, ten scores landed in the first four
    minutes and the reports then sat frozen for the next forty-seven while the matcher
    recorded ~1,470 further rows — the user was reading a page that could not move.

    A clock cannot run out. It also makes a missed row self-correcting: the refresh a
    callback schedules can read SQLite before the row it was fired for has committed,
    and under the old wiring the last such miss was permanent (nothing fired again).
    Now the next tick simply picks it up.

    Never raises: `schedule` hands work to an executor and `reporting.refresh`
    swallows everything, but a stray exception here would cancel the sweep it is
    supposed to be reporting on.
    """
    while True:
        await asyncio.sleep(_REPORT_TICK_S)
        try:
            schedule()
        except Exception:  # noqa: BLE001 - a report is never worth a run
            logger.exception("Scheduling a report refresh failed")


async def _stop_report_ticker(ticker: asyncio.Task, busy: threading.Lock) -> None:
    """End the ticker and wait for any rebuild it already started.

    Both halves are required, and the ordering they protect is documented in
    CLAUDE.md: the pages' meta refresh is armed only while the pipeline's `runs`
    row is absent, so the `final=True` write must be the last one. Cancelling the task
    alone is not enough — `schedule` dispatches to an executor thread, so a rebuild
    fired a moment earlier can still be mid-write and would land *after* the final
    one, re-arming the refresh and leaving a finished run reloading itself forever.

    Draining goes through `asyncio.to_thread` because the lock is held by an executor
    thread doing blocking SQLite work; acquiring it on the event loop would stall the
    loop that thread is racing against.
    """
    ticker.cancel()
    await asyncio.gather(ticker, return_exceptions=True)
    await asyncio.to_thread(busy.acquire)
    busy.release()


def _make_progress() -> Progress:
    """One Progress shared by every phase, rendered inside the single Live.

    A per-task `count_str` field carries the human-readable count so one column
    set renders both the determinate scrape bar and the count-up match/tune/apply
    bars (BarColumn auto-pulses whenever a task's total is None).
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.fields[count_str]}"),
        console=console,
    )


async def run_pipeline(
    skip_matcher: bool = False,
    skip_llm: bool = False,
    apply: bool = False,
    quiet: bool = False,
) -> str | None:
    """Run one full sweep. Returns the run_id, or None if the run failed.

    `quiet` suppresses the Rich live view entirely — required under the monitor,
    where every stdout line becomes a user-facing notification.
    """
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y-%m-%dT%H-%M-%SZ")   # DB key across five tables: UTC, monotonic
    started_at = now.isoformat()
    stamp = _run_stamp(now)                       # display only — folder and file names

    # Never raises while a workspace is configured, so an unreachable output
    # folder cannot take down a sweep that has not started yet.
    results_dir = paths.make_run_dir(stamp)
    _write_current_run(run_id, stamp, results_dir, started_at)

    logger.info("=" * 60)
    logger.info("Pipeline starting — run %s → %s", run_id, results_dir)
    logger.info("=" * 60)

    q3: asyncio.Queue = asyncio.Queue()

    progress = _make_progress()
    tasks: dict[str, int] = {}          # phase → task id (only active phases get one)
    counts = {"match": 0}               # the match phase has no known total → count up
    milestones: set[int] = set()        # scrape quarters already logged, see below

    # The reports are rebuilt off the event loop and at most one at a time. Both
    # halves matter: the callbacks below fire thousands of times on a full sweep,
    # and `reporting.refresh` does blocking SQLite work, so calling it inline would
    # stall the very loop that is fetching boards. `reporting` throttles on top of
    # this, so the executor is normally handed a no-op.
    report_busy = threading.Lock()

    def _refresh_reports() -> None:
        if not report_busy.acquire(blocking=False):
            return
        try:
            reporting.refresh(run_id, results_dir, stamp)
        finally:
            report_busy.release()

    def schedule_report_refresh() -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, _refresh_reports)
        except RuntimeError:
            pass  # no loop (never in practice); a missed refresh is not worth raising

    def on_company_start(name: str, board: str, done: int, total: int) -> None:
        schedule_report_refresh()
        _log_scrape_milestone(done, total, milestones)
        if "scrape" not in tasks:
            tasks["scrape"] = progress.add_task(
                "[bold]Scraping[/bold]", total=total, count_str=f"0/{total}"
            )
        progress.update(
            tasks["scrape"],
            total=total,
            completed=done,
            description=f"[bold]Scraping[/bold] ({board}) {name}",
            count_str=f"{done}/{total} ({total - done} left)",
        )

    def on_job_score(board_token: str, title: str) -> None:
        schedule_report_refresh()
        counts["match"] += 1
        progress.update(
            tasks["match"],
            description=f"[bold]Matching[/bold] {board_token} — {title[:45]}",
            count_str=f"{counts['match']} scored",
        )

    live = (
        nullcontext() if quiet
        else Live(progress, console=console, refresh_per_second=4)
    )
    # Set at the end of the pipeline body below, and read by the `finally` that
    # writes the run's files. A sweep that raised still gets every file it earned;
    # this is how they record that they are partial.
    finished = False
    try:
        with live:
            # Covers both branches below, and is stopped before the outputs are
            # written rather than at the end of the run — see `_stop_report_ticker`.
            ticker = asyncio.create_task(_tick_reports(schedule_report_refresh))
            try:
                if skip_matcher:
                    await asyncio.to_thread(get_db().start_progress, run_id, False)
                    await scraper.main(quiet=True, run_id=run_id, on_company_start=on_company_start)
                    await q3.put(None)
                    await _track_results(q3, results_dir, run_id, stamp, quiet)
                else:
                    tasks["match"] = progress.add_task("[bold]Matching[/bold]", total=None, count_str="0 scored")
                    q1: asyncio.Queue = asyncio.Queue()
                    q2: asyncio.Queue = asyncio.Queue()
                    stages = [
                        scraper.main(out_queue=q1, quiet=True, run_id=run_id, on_company_start=on_company_start),
                        matcher.main(in_queue=q1, out_queue=q2, quiet=True, run_id=run_id, skip_llm=skip_llm, on_job_score=on_job_score),
                        _collect_results(q2, q3),
                    ]

                    # Applying streams too: each shortlisted job reaches the worker the
                    # moment it is tracked, so an application can go out while the
                    # scrape is still visiting other employers. It finishes before the
                    # outputs below are written, which is what keeps the CSV's
                    # `applied` column right without a second pass.
                    q4: asyncio.Queue | None = None
                    apply_inputs = _load_apply_inputs() if apply else None
                    if apply_inputs is not None:
                        from hireshire.applier.worker import run_apply_worker

                        applier_settings, resume_text = apply_inputs
                        q4 = asyncio.Queue()
                        tasks["apply"] = progress.add_task(
                            "[bold]Applying[/bold]", total=None, count_str="waiting"
                        )

                        def on_apply_progress(stats: dict[str, int]) -> None:
                            progress.update(
                                tasks["apply"],
                                count_str=f"{stats['submitted']} submitted, {stats['error']} error",
                            )

                        if skip_llm:
                            logger.warning(
                                "Applier is on with LLM scoring skipped: every job that "
                                "passes the free gates will be applied to."
                            )
                        stages.append(run_apply_worker(
                            q4, applier_settings, resume_text, run_id=run_id,
                            run_dir=results_dir, on_progress=on_apply_progress,
                        ))
                    stages.append(_track_results(q3, results_dir, run_id, stamp, quiet, apply_q=q4))
                    # Before anything starts writing counters: every progress write
                    # is an UPDATE against this row. Nothing has been awaited since
                    # the stages were built, so none of them is running yet.
                    await asyncio.to_thread(
                        get_db().start_progress, run_id, apply_inputs is not None
                    )

                    # `gather` does not cancel its siblings when one raises. That used to
                    # leave the scrape running orphaned; with the applier on the queue it
                    # would leave a browser submitting applications for a sweep that has
                    # already failed and written its outputs. Cancelling the worker kills
                    # its session in flight.
                    running = [asyncio.ensure_future(s) for s in stages]
                    try:
                        await asyncio.gather(*running)
                    except BaseException:
                        for t in running:
                            t.cancel()
                        await asyncio.gather(*running, return_exceptions=True)
                        raise
                finished = True
            finally:
                # All of this is in a `finally`, and that is the whole point: a sweep
                # that died fifteen minutes in has real scored rows in the database,
                # and before this they were reachable only by opening it by hand. No
                # CSV, no JSON, a `last_run.json` still naming the previous run, and
                # two pages left meta-refreshing forever because the `runs` row that
                # says "this sweep is over" was never written.
                #
                # Order matters. Stop the ticker first, so no rebuild is running
                # against the database while the files are read; then the files; then
                # the `runs` row and one final refresh, which must follow it because
                # the pages read that row to decide whether to keep reloading.
                await _stop_report_ticker(ticker, report_busy)
                try:
                    total_results = await _write_run_outputs(
                        run_id, results_dir, stamp, complete=finished
                    )
                    await _finalise_pipeline(
                        run_id, results_dir, started_at, stamp,
                        total_results, complete=finished,
                    )
                    _clear_current_run(run_id)
                except Exception:  # noqa: BLE001
                    # A `finally` that raises replaces the real traceback with its
                    # own, which would hide the failure this exists to survive.
                    logger.exception("Could not write the run's outputs — run %s", run_id)

        logger.info("Pipeline complete — run %s", run_id)
        # A reader of the console output gets the path here rather than reconstructing it.
        # Never under quiet: each monitor stdout line becomes a notification, and
        # the monitor emits exactly one summary line per cycle.
        if not quiet:
            console.print(f"\n[bold]Results:[/bold] {results_dir / results_name(stamp)}")
        return run_id
    except Exception:
        logger.exception("Pipeline failed — run %s", run_id)
        # Say where the partial results are. The `finally` above wrote them, and a
        # user whose twenty-minute sweep died at minute fifteen should not be told
        # only that it failed.
        if not quiet:
            console.print(
                f"\n[yellow]Partial results:[/yellow] "
                f"{results_dir / results_name(stamp)} — everything this sweep "
                f"judged before it failed."
            )
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(description="HireShire pipeline orchestrator")
    parser.add_argument(
        "--now", action="store_true",
        help="Run the pipeline immediately on start, then schedule",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run exactly once and exit (no scheduling)",
    )
    parser.add_argument(
        "--interval", type=float, default=4.0, metavar="HOURS",
        help="Hours between pipeline runs (default: 4)",
    )
    parser.add_argument(
        "--no-matcher", action="store_true",
        help="Run scraper only; skip scoring",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Skip LLM scoring in the matcher; all title-passing jobs are shortlisted automatically",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Force-enable the applier (overrides config/applier.yaml enable_applier)",
    )
    args = parser.parse_args()

    _setup_logging()

    # The applier defaults from config (enable_applier); --apply is an explicit
    # force-on override.
    from hireshire.applier.config import load_applier_config

    apply = args.apply or load_applier_config().settings.enable_applier

    interval_s = args.interval * 3600

    if not args.now and not args.once:
        logger.info("Orchestrator started — first run in %.1fh", args.interval)
        await asyncio.sleep(interval_s)

    while True:
        await run_pipeline(
            skip_matcher=args.no_matcher,
            skip_llm=args.no_llm,
            apply=apply,
        )
        if args.once:
            break
        logger.info("Next run in %.1fh", args.interval)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    asyncio.run(main())
