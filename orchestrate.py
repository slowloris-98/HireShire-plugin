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
import csv
import json
import logging
import logging.handlers
import os
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

import matcher
import scraper
from hireshire import paths
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


# `found_at` is when we processed the job; `posted_at` is when the employer
# posted it. They are different questions and the CSV needs both — the old
# schema carried only the former, under a name that read like the latter.
_CSV_FIELDS = [
    "title", "company", "location", "posted_at", "job_url",
    "relevance_score", "rerank_score", "job_id", "found_at",
]


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
            "found_at": datetime.now(timezone.utc).isoformat(),
        })
    await out_q.put(None)


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

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p",
        "--permission-mode", "auto",
        skill_prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=skill_env,
    )
    stdout, stderr = await proc.communicate()
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


def _csv_name(stamp: str) -> str:
    return f"{stamp}_results.csv"


def _json_name(stamp: str) -> str:
    return f"{stamp}_results.json"


async def _open_csv_append(path: Path, attempts: int = 5, base_delay: float = 0.5):
    """Open `path` in append mode, retrying on a transient Windows lock
    (PermissionError) with exponential backoff. Returns the open file handle,
    or None if it could not be opened after `attempts` tries."""
    for i in range(attempts):
        try:
            return path.open("a", newline="", encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                return None
            delay = base_delay * (2 ** i)  # 0.5, 1, 2, 4 s
            logger.warning(
                "CSV %s is locked (attempt %d/%d); retrying in %.1fs",
                path, i + 1, attempts, delay,
            )
            await asyncio.sleep(delay)


async def _track_results(
    q: asyncio.Queue, results_dir: Path, run_id: str, stamp: str, quiet: bool = False
) -> None:
    """Persist each pipeline result to the DB (O(1) per row) and append it to the
    per-run CSV. The CSV handle is opened once for the run's lifetime; a transient
    file lock retries with backoff and, if it never clears, degrades to DB-only
    writes rather than crashing the pipeline (the DB is the source of truth).

    That degrade path matters more than it used to: the CSV now sits in a folder
    the user actively browses and may well have open in Excel, so the lock is a
    routine event rather than a theoretical one."""
    db = get_db()
    csv_path = results_dir / _csv_name(stamp)

    write_header = not csv_path.exists()
    f = await _open_csv_append(csv_path)
    writer = None
    if f is None:
        logger.error(
            "Could not open %s after retries; continuing with DB-only writes",
            csv_path,
        )
        # The run still "succeeds" with rows only in the DB, so say so where the
        # user will see it — a logger.error in a file nobody opens is not enough.
        # Never under quiet: each monitor stdout line becomes a notification.
        if not quiet:
            console.print(
                f"[yellow]Could not write {csv_path} — it looks locked by another "
                f"program (Excel?). Results are in the database; close the file "
                f"and re-run to get the CSV.[/yellow]"
            )
    else:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
            f.flush()

    try:
        while True:
            record = await q.get()
            if record is None:
                break

            await asyncio.to_thread(db.record_pipeline_result, run_id, record)

            if writer is not None:
                try:
                    writer.writerow(record)
                    f.flush()
                except OSError as exc:
                    logger.warning("Failed to append row to %s: %s", csv_path, exc)

            logger.info("Tracked result: %s — %s", record["company"], record["title"])
    finally:
        if f is not None:
            f.close()


async def _finalise_pipeline(run_id: str, results_dir: Path, started_at: str, stamp: str) -> None:
    """Export the run's pipeline results to JSON once from the DB (read by the
    /apply skill) and record the pipeline run's summary row."""
    db = get_db()
    rows = await asyncio.to_thread(db.load_pipeline_results, run_id)

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
    try:
        paths.LAST_RUN_PATH.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "stamp": stamp,
                    "results_dir": str(results_dir),
                    "csv": str(results_dir / _csv_name(stamp)),
                    "json": str(json_path),
                    "total_results": len(rows),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write %s: %s", paths.LAST_RUN_PATH, exc)

    await asyncio.to_thread(
        db.finalise_run, run_id, PHASE_PIPELINE, started_at, None,
        {"total_results": len(rows)},
    )


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

    def on_company_start(name: str, board: str, done: int, total: int) -> None:
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
    try:
        with live:
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

            await _finalise_pipeline(run_id, results_dir, started_at, stamp)

            # Apply runs inside the same Live so its bar shares this Progress —
            # never a second Live. Needs shortlisted jobs, so skip it when the
            # matcher was skipped.
            if apply and not skip_matcher:
                apply_task = progress.add_task("[bold]Applying[/bold]", total=None, count_str="running")
                await _launch_apply()
                progress.update(apply_task, count_str="done")

        logger.info("Pipeline complete — run %s", run_id)
        # The find-jobs skill reads this line rather than reconstructing the path.
        # Never under quiet: each monitor stdout line becomes a notification, and
        # the monitor emits exactly one summary line per cycle.
        if not quiet:
            console.print(f"\n[bold]Results:[/bold] {results_dir / _csv_name(stamp)}")
        return run_id
    except Exception:
        logger.exception("Pipeline failed — run %s", run_id)
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
