"""The streaming applier: one browser session per shortlisted job, as it is shortlisted.

The apply phase used to run once, after the sweep: a single `claude -p` session was
handed the whole apply skill, rebuilt the shortlist out of `last_run.json` and recorded
its own results through Bash. None of that can consume a queue — its input was a file
written at the end of the run, and the engine never learned what it did. So a job
shortlisted in minute three waited for the other ~15,000 employers before anything
applied to it.

Now `orchestrate._track_results` hands each shortlisted job to `run_apply_worker` the
moment it is recorded. The browser is still driven by Claude through the plugin's
Playwright MCP — forms differ per employer and the questions need a model that has
read the resume — but each session applies to exactly one job and returns a
structured `ApplyOutcome`, which the engine records itself.

Three rules shape this file:

* **One application at a time.** Each session opens its own browser and shares the
  subscription with the scorer's calls, so the worker drains the queue serially with
  `inter_job_delay_s` between launches.
* **A verdict is recorded; a failure to launch is not.** `submitted` and `error` are
  outcomes about the job and go to the `applied` table, which retires it. A session
  that never got going — the CLI missing, a non-zero exit — writes nothing, so the
  backlog (`Database.load_pending_applications`) retries it on a later sweep. That
  matters because the matcher retires a judged job: without the backlog, a broken MCP
  server would lose a whole sweep's shortlist for good.
* **Never apply twice.** A timeout, or a clean exit with an unreadable result, may
  come *after* the submit click, so those are recorded as `error` with a message
  telling the user to check — the one case where the job is retired on something other
  than a verdict, because the alternative is a second application to the same
  employer. `applied_ids` is re-read before every launch, so a manual
  `/hireshire:apply` running alongside a sweep cannot double up either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from hireshire import claude_cli, paths
from hireshire.applier.config import ApplierSettings
from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)

#: The per-job instructions, shared with the manual `/hireshire:apply` skill so the
#: form-filling rules exist once.
PROMPT_PATH = Path(__file__).resolve().parent / "apply_one.md"

#: Consecutive launch failures before the worker stops launching for the rest of the
#: sweep. A missing Playwright MCP server or a logged-out CLI fails every job the same
#: way; there is nothing to learn from the fourth attempt.
BREAKER_LIMIT = 3


class ApplyOutcome(BaseModel):
    """What one apply session reports back, as its `--json-schema` result."""

    status: Literal["submitted", "error", "skipped_location"]
    screenshot: Optional[str] = None
    error: Optional[str] = None


class ApplyLaunchError(RuntimeError):
    """The session failed before it could reach a verdict about the job.

    Not recorded, so the job stays pending and the backlog retries it.
    """


#: The `claude -p` subprocess driving a browser, while one is running; None otherwise.
_apply_proc: "asyncio.subprocess.Process | None" = None


def terminate_apply_subprocess() -> None:
    """Kill the apply subprocess and the browser under it, if one is running.

    Tree-wide, because `claude -p` spawns the browser itself — killing only the CLI
    would leave a Playwright process sitting on a half-filled application form.

    Never raises: the callers are a timeout and a cancellation, both already on their
    way out.
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


def build_prompt(job: dict, settings: ApplierSettings, resume_path: Path,
                 resume_text: str) -> str:
    """The shared per-job instructions, plus this job's inputs and how to finish."""
    inputs = {
        "job": {
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "job_url": job.get("job_url"),
        },
        "applicant": {
            "first_name": settings.first_name,
            "last_name": settings.last_name,
            "email": settings.email,
            "phone": settings.phone,
        },
        "resume_path": str(resume_path),
        "generate_cover_letter": settings.generate_cover_letter,
    }
    return (
        PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\n## How to finish\n\n"
        "Return the outcome as your structured result. Do not run shell commands and "
        "do not record the application anywhere — HireShire records it from your "
        "result.\n\n"
        "## This job\n\n```json\n" + json.dumps(inputs, indent=2) + "\n```\n\n"
        "## Resume text\n\n" + resume_text + "\n"
    )


async def apply_one(job: dict, settings: ApplierSettings, resume_path: Path,
                    resume_text: str) -> ApplyOutcome:
    """Run one apply session and return what it reported.

    Raises `ApplyLaunchError` when the session never reached a verdict. A timeout or
    an unreadable result is *returned* as an `error` outcome instead, because by then
    the form may already have been submitted — see the module docstring.
    """
    global _apply_proc
    prompt = build_prompt(job, settings, resume_path, resume_text)
    schema = json.dumps(ApplyOutcome.model_json_schema())

    # The prompt goes on stdin, never in argv: it opens with a Markdown heading today,
    # but the SKILL.md it was split out of opened with `---`, which the CLI parsed as
    # an option and failed every apply phase with. stdin also has no length limit and
    # keeps the resume off the process table. `--no-session-persistence` for the
    # scorer's reason: one transcript and one billed title per job, for nothing.
    #
    # The session brings its own browser server. A `claude -p` the engine starts is not
    # guaranteed to load the plugin, and measured on a dev machine it did not: the
    # `mcp__plugin_hireshire_playwright__*` tools were absent and only an unrelated
    # user-level server was there. `--strict-mcp-config` pins it to the plugin's own
    # `.mcp.json` whatever is installed, which also keeps the user's other MCP servers
    # out of an unattended session — at the cost that the tools are named
    # `mcp__playwright__*` here, which `apply_one.md` says.
    #
    # It also runs in `applied_dir`, under DATA. The Playwright MCP server writes its
    # screenshots into `.playwright-mcp/` beneath the session's working directory, and
    # a sweep's working directory is ROOT — which every plugin update replaces, taking
    # the only record of each submitted form with it.
    workdir = paths.resolve_data(settings.applied_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--permission-mode", "auto",
            "--no-session-persistence",
            "--mcp-config", str(paths.ROOT / ".mcp.json"),
            "--strict-mcp-config",
            "--output-format", "json",
            "--json-schema", schema,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=claude_cli.subscription_env(),
            cwd=str(workdir),
        )
    except OSError as exc:
        raise ApplyLaunchError(f"could not start the claude CLI: {exc}") from exc

    _apply_proc = proc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=settings.apply_timeout_s,
        )
    except asyncio.TimeoutError:
        terminate_apply_subprocess()
        return ApplyOutcome(
            status="error",
            error=(f"Timed out after {settings.apply_timeout_s:g}s. The application "
                   "may or may not have been submitted — check before applying again."),
        )
    except asyncio.CancelledError:
        # The sweep is being torn down. An orphaned session would go on submitting
        # an application nobody is watching.
        terminate_apply_subprocess()
        raise
    finally:
        _apply_proc = None

    if proc.returncode != 0:
        detail = (stderr.decode(errors="replace").strip()
                  or stdout.decode(errors="replace").strip() or "(no output)")
        raise ApplyLaunchError(f"claude CLI exited {proc.returncode}: {detail[:500]}")

    raw = stdout.decode(errors="replace")
    try:
        return ApplyOutcome.model_validate(claude_cli.unwrap_envelope(json.loads(raw)))
    except (json.JSONDecodeError, RuntimeError, ValidationError):
        logger.error("Unreadable apply result for %s: %s", job.get("job_id"), raw[:500])
        return ApplyOutcome(
            status="error",
            error=("The browser session ended without a readable result. The "
                   "application may or may not have been submitted — check before "
                   "applying again."),
        )


async def run_apply_worker(
    in_q: asyncio.Queue,
    settings: ApplierSettings,
    resume_text: str,
    *,
    db: Database | None = None,
    include_backlog: bool = True,
    on_progress: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """Apply to each job put on `in_q` until the `None` sentinel. Returns the tallies.

    Jobs are pipeline records: `job_id`, `title`, `company`, `job_url`. The backlog of
    recent shortlisted jobs that were never applied to is worked through first.

    Never raises for a single job, and always drains `in_q` to the sentinel — even
    once the breaker has tripped — so the producer is never left holding the queue
    and a failure here cannot end the sweep it is part of.
    """
    db = db or get_db()
    stats = {"submitted": 0, "error": 0, "skipped_location": 0,
             "excluded": 0, "deferred": 0}
    excluded = {c.strip().lower() for c in settings.exclude_companies}
    attempted: set[str] = set()
    state = {"consecutive": 0, "tripped": False, "launched": False}

    resume_path = paths.resolve_data(settings.resume_path) if settings.resume_path else None
    blocked = None
    if resume_path is None or not resume_path.exists():
        blocked = f"resume not found at {settings.resume_path or '(not set)'}"
        logger.error("Applier: %s — not applying to anything this sweep.", blocked)

    async def handle(job: dict, from_backlog: bool) -> None:
        job_id = job.get("job_id")
        if not job_id or job_id in attempted:
            return
        attempted.add(job_id)
        company = job.get("company") or ""
        title = job.get("title") or ""

        if company.strip().lower() in excluded:
            # Not a failure: those portals need an account login. The job stays in the
            # overview's Shortlisted section, and the log line says why nothing ran.
            stats["excluded"] += 1
            log = logger.debug if from_backlog else logger.info
            log("Apply manually: %s — %s %s", company, title, job.get("job_url"))
            return
        if blocked or state["tripped"]:
            stats["deferred"] += 1
            return
        if job_id in await asyncio.to_thread(db.applied_ids):
            return

        if state["launched"] and settings.inter_job_delay_s > 0:
            await asyncio.sleep(settings.inter_job_delay_s)
        state["launched"] = True

        logger.info("Applying: %s — %s", company, title)
        try:
            outcome = await apply_one(job, settings, resume_path, resume_text)
        except ApplyLaunchError as exc:
            stats["deferred"] += 1
            state["consecutive"] += 1
            logger.warning("Apply session failed for %s — %s (will retry next sweep): %s",
                           company, title, exc)
            if state["consecutive"] >= BREAKER_LIMIT:
                state["tripped"] = True
                logger.error(
                    "Applier: %d apply sessions failed in a row — not launching any "
                    "more this sweep. Nothing was recorded; those jobs stay pending "
                    "and are retried next sweep. Last error: %s", BREAKER_LIMIT, exc,
                )
            return
        state["consecutive"] = 0

        if outcome.status == "skipped_location":
            stats["skipped_location"] += 1
            logger.info("Skipped (location): %s — %s", company, title)
        else:
            await asyncio.to_thread(
                db.record_applied, job_id, company, title, job.get("job_url") or "",
                datetime.now(timezone.utc).isoformat(), outcome.status,
                outcome.screenshot, outcome.error,
            )
            stats[outcome.status] += 1
            logger.info("Applied (%s): %s — %s%s", outcome.status, company, title,
                        f" — {outcome.error}" if outcome.error else "")
        if on_progress:
            on_progress(dict(stats))

    async def safely(job: dict, from_backlog: bool) -> None:
        try:
            await handle(job, from_backlog)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one job is never worth the sweep
            logger.exception("Applier failed on job %s", job.get("job_id"))

    if include_backlog and not blocked:
        since = datetime.now(timezone.utc) - timedelta(hours=settings.backlog_hours)
        try:
            backlog = await asyncio.to_thread(db.load_pending_applications, since.isoformat())
        except Exception:  # noqa: BLE001
            logger.exception("Could not load the apply backlog")
            backlog = []
        if backlog:
            logger.info("Applier: %d pending job(s) from earlier sweeps", len(backlog))
        for job in backlog:
            await safely(job, from_backlog=True)

    while True:
        job = await in_q.get()
        if job is None:
            break
        await safely(job, from_backlog=False)

    logger.info(
        "Applier done: %d submitted, %d error, %d skipped for location, "
        "%d at excluded companies, %d deferred to a later sweep",
        stats["submitted"], stats["error"], stats["skipped_location"],
        stats["excluded"], stats["deferred"],
    )
    return stats
