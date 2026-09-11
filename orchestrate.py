"""
HireShire pipeline orchestrator — runs Scraper → Matcher over asyncio queues,
then repeats on a schedule.

    python orchestrate.py              # wait 4h, then run; repeat every 4h
    python orchestrate.py --now        # run immediately, then every 4h
    python orchestrate.py --once       # run exactly once, no scheduling
    python orchestrate.py --interval 2 # every 2 hours instead of 4
    python orchestrate.py --no-matcher # scraper only (no scoring)
    python orchestrate.py --apply      # run the /hireshire:apply skill afterwards
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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


#: The `claude -p` subprocess driving a browser through the apply phase, while one is
#: running; None otherwise.
#:
#: Held here so the session watchdog in `scripts/run_orchestration.py` can take it down
#: before the sweep exits. It is a *child* of the sweep, so nothing else will: an
#: orphaned apply phase goes on submitting real applications, unattended, after the
#: session that authorised it has gone. That is the exact failure this whole mechanism
#: exists to prevent, so leaving it running would make the fix cosmetic.
_apply_proc: "asyncio.subprocess.Process | None" = None


def terminate_apply_subprocess() -> None:
    """Kill the apply subprocess and the browser under it, if one is running.

    Tree-wide, because `claude -p` spawns the browser itself — killing only the CLI
    would leave a Playwright process sitting on a half-filled application form.

    Never raises: the only caller is already on its way out, and a failure here must
    not stop it from clearing the status file and exiting.
    """
    proc = _apply_proc
    if proc is None or proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            # Children first, then the process itself — the same order and the same
            # reasoning as `bootstrap.stop()`. `pkill -P` never signals the parent.
            subprocess.run(
                ["pkill", "-KILL", "-P", str(proc.pid)], capture_output=True, text=True
            )
            proc.kill()
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not terminate the apply subprocess (pid %s)", proc.pid)


async def _launch_skill(skill_name: str, extra: str = "") -> bool:
    """Run a Claude Code skill as a `claude -p` subprocess. Returns success."""
    # Plugin layout: skills live at ROOT/skills/<name>/SKILL.md. The whole body is
    # passed as the literal prompt — this is not a slash-command invocation.
    skill_path = paths.ROOT / "skills" / skill_name / "SKILL.md"
    if not skill_path.exists():
        logger.error("%s skill not found at %s", skill_name, skill_path)
        return False

    skill_prompt = skill_path.read_text(encoding="utf-8") + extra
    logger.info("Launching /%s skill...", skill_name)

    # load_dotenv() puts ANTHROPIC_API_KEY (needed by the matcher LLM
    # backends) into our environment, and the Claude CLI prefers that key over
    # the claude.ai subscription login — billing pay-as-you-go credits and
    # failing with "Credit balance is too low" when they run out. Strip the API
    # auth vars so the subprocess uses the subscription instead.
    skill_env = {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }

    # The prompt goes on STDIN, never in argv. A SKILL.md opens with `---` YAML
    # frontmatter, and the CLI parses a leading-dash argument as an option:
    #     error: unknown option '---\nname: apply...'
    # That failed every apply phase with exit code 1, visible only as one line in a
    # 1.4 MB log. stdin is used rather than a `--` separator because it also keeps
    # the prompt off the process table and has no length limit to trip over.
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p",
        "--permission-mode", "auto",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=skill_env,
    )
    # Published for `terminate_apply_subprocess`, and cleared the moment it exits so a
    # later shutdown cannot go hunting for a pid the OS has already recycled.
    global _apply_proc
    _apply_proc = proc
    try:
        stdout, stderr = await proc.communicate(skill_prompt.encode("utf-8"))
    finally:
        _apply_proc = None
    if stdout:
        logger.info("%s output:\n%s", skill_name, stdout.decode(errors="replace"))
    if proc.returncode != 0:
        logger.error(
            "%s skill exited with code %d\n%s",
            skill_name,
            proc.returncode,
            stderr.decode(errors="replace"),
        )
        return False
    logger.info("%s skill completed successfully", skill_name)
    return True


async def _launch_apply() -> None:
    await _launch_skill("apply")


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
    q: asyncio.Queue, results_dir: Path, run_id: str, stamp: str, quiet: bool = False
) -> None:
    """Persist each pipeline result to the DB, O(1) per row.

    Only the DB. The CSV used to be appended here as rows arrived, which meant a
    file in processing order that also had to survive being open in Excel — a
    routine event, since it sits in a folder the user actively browses. It is now
    written once from the database at the end of the run, because it is sorted
    across the whole sweep and no row's position is known until every row exists.
    `_write_run_outputs` does that in a `finally`, so a sweep that dies part-way
    still leaves the CSV for everything it judged.
    """
    db = get_db()
    while True:
        record = await q.get()
        if record is None:
            break
        await asyncio.to_thread(db.record_pipeline_result, run_id, record)
        logger.info("Tracked result: %s — %s", record["company"], record["title"])


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


# Quarters, not a percentage every company. The find-jobs skill tails the log for
# these to relay progress, and every line it matches becomes a message in the user's
# session — so there are four of them for a sweep that visits ~10,000 employers, and
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
                    await scraper.main(quiet=True, run_id=run_id, on_company_start=on_company_start)
                    await q3.put(None)
                    await _track_results(q3, results_dir, run_id, stamp, quiet)
                else:
                    tasks["match"] = progress.add_task("[bold]Matching[/bold]", total=None, count_str="0 scored")
                    q1: asyncio.Queue = asyncio.Queue()
                    q2: asyncio.Queue = asyncio.Queue()
                    await asyncio.gather(
                        scraper.main(out_queue=q1, quiet=True, run_id=run_id, on_company_start=on_company_start),
                        matcher.main(in_queue=q1, out_queue=q2, quiet=True, run_id=run_id, skip_llm=skip_llm, on_job_score=on_job_score),
                        _collect_results(q2, q3),
                        _track_results(q3, results_dir, run_id, stamp, quiet),
                    )
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
                except Exception:  # noqa: BLE001
                    # A `finally` that raises replaces the real traceback with its
                    # own, which would hide the failure this exists to survive.
                    logger.exception("Could not write the run's outputs — run %s", run_id)

            # Apply runs inside the same Live so its bar shares this Progress —
            # never a second Live. Needs shortlisted jobs, so skip it when the
            # matcher was skipped. Unreachable on a failed sweep: the exception is
            # already on its way out through the `finally` above.
            if apply and not skip_matcher:
                apply_task = progress.add_task("[bold]Applying[/bold]", total=None, count_str="running")
                await _launch_apply()
                # The `applied` table is only written during the phase above, and
                # both the CSV's `applied` column and the pages' Applied section
                # were written before it — so without this they report a run that
                # applied to nothing. Safe after `finalise_run`: the `runs` row now
                # exists, so this renders the run as finished rather than re-arming
                # the meta refresh.
                await _write_run_outputs(run_id, results_dir, stamp, complete=True)
                await asyncio.to_thread(
                    reporting.refresh, run_id, results_dir, stamp, True
                )
                progress.update(apply_task, count_str="done")

        logger.info("Pipeline complete — run %s", run_id)
        # The find-jobs skill reads this line rather than reconstructing the path.
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
