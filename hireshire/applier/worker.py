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
  outcomes about the job and go to the `applied` table, which retires it. So is
  `excluded`, which no session produces: the employer's portal needs an account login,
  so the answer is the same on every future sweep and the user has to do it by hand. A
  session that never got going — the CLI missing, a non-zero exit — writes nothing, so
  the backlog (`Database.load_pending_applications`) retries it on a later sweep. That
  matters because the matcher retires a judged job: without the backlog, a broken MCP
  server would lose a whole sweep's shortlist for good.
* **Never apply twice.** A timeout, or a clean exit with an unreadable result, may
  come *after* the submit click, so those are recorded as `error` with a message
  telling the user to check — the one case where the job is retired on something other
  than a verdict, because the alternative is a second application to the same
  employer. `applied_ids` is re-read before every launch, so a job recorded by
  anything else since the queue was built is not applied to twice either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from hireshire import claude_cli, paths
from hireshire.applier.config import ApplierSettings
from hireshire.storage.db import Database, get_db

logger = logging.getLogger(__name__)

#: The per-job instructions each apply session follows.
PROMPT_PATH = Path(__file__).resolve().parent / "apply_one.md"

#: Consecutive launch failures before the worker stops launching for the rest of the
#: sweep. A missing Playwright MCP server or a logged-out CLI fails every job the same
#: way; there is nothing to learn from the fourth attempt.
BREAKER_LIMIT = 3

#: Why an excluded employer never becomes an application. Recorded as the `error` of
#: an `excluded` row and printed verbatim under the overview's Needs Attention
#: section, so it obeys the same one-short-line contract `apply_one.md` imposes.
EXCLUDED_REASON = ("Requires human verification — this employer's portal needs an "
                   "account login, so apply to it yourself.")


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


@dataclass(frozen=True)
class SessionDirs:
    """Where an apply session runs, where its files go, and the resume it uploads."""

    cwd: Path
    out_dir: Path
    resume_path: Path


def session_dirs(resume_path: Path, run_dir: Path) -> SessionDirs:
    """Pick the apply session's working directory, and a resume path it may upload.

    `out_dir` is **this run's** `applied/` folder, beside the CSV, JSON and dashboard
    the same sweep writes. It used to be one folder shared by every sweep, which left
    the user's only record of each submitted form in a pile with no way to tell which
    run it came from. `run_dir` is whatever `paths.make_run_dir` returned, so the
    fallback when no workspace is usable — `DATA/results/<stamp>/applied` — follows
    the same rule and there is only one layout.

    `cwd` is a different question. Playwright MCP refuses to upload a file outside the
    client's roots, and Claude Code's root is the session's cwd; setup puts the resume
    in the workspace, so a session that ran under DATA failed every form that required
    a resume, with nothing submitted. So cwd is the workspace whenever it actually
    contains `out_dir` — which is the real invariant, since the server also refuses to
    *write* outside the roots. It can fail to hold with a workspace configured: if
    `make_run_dir` fell back to DATA (the folder went missing, or is unwritable), the
    run's own folder is not under the workspace any more, and then the session runs in
    `out_dir` itself. A resume still outside `cwd` is copied in.

    `out_dir` is meant to hold screenshots only, so browser-server output is cleared
    from it here: a `.browser/` a killed session never got to delete, and the loose
    snapshots and console logs from before the scratch dir existed. That reaches only
    this run's folder now — an earlier sweep's leftovers stay in that sweep's folder,
    which is where its screenshots are anyway.
    """
    out_dir = run_dir / paths.APPLIED_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    ws = paths.workspace_dir()
    cwd = ws if (ws is not None and ws.is_dir()
                 and out_dir.resolve().is_relative_to(ws.resolve())) else out_dir
    _clear_browser_output(out_dir)

    upload = resume_path
    if not resume_path.resolve().is_relative_to(cwd.resolve()):
        upload = out_dir / resume_path.name
        src = resume_path.stat()
        if not (upload.exists() and upload.stat().st_size == src.st_size
                and upload.stat().st_mtime == src.st_mtime):
            shutil.copy2(resume_path, upload)
    return SessionDirs(cwd=cwd, out_dir=out_dir, resume_path=upload)


#: Under `out_dir`: the browser server's own output, one subdirectory per session.
SCRATCH_DIRNAME = ".browser"

#: What the browser server writes into `--output-dir` unasked: a `page-*.yml` for
#: every snapshot, a `console-*.log` for the page's console.
_LEGACY_OUTPUT = ("page-*.yml", "console-*.log")


def _clear_browser_output(out_dir: Path) -> None:
    """Delete browser-server output from `out_dir`, leaving screenshots alone."""
    shutil.rmtree(out_dir / SCRATCH_DIRNAME, ignore_errors=True)
    for pattern in _LEGACY_OUTPUT:
        for f in out_dir.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass


def _safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)


def scratch_dir(dirs: SessionDirs, job: dict) -> Path:
    """Where one session's browser server writes its automatically named files.

    Playwright MCP saves every page snapshot as a `page-*.yml` and the console as a
    `console-*.log` in `--output-dir`, whether or not anything asks for them — 204
    snapshots and 20 logs beside 7 screenshots on one sweep. The session may read those
    snapshots while it fills the form, so they live here until it has exited and its
    outcome is recorded, then `run_apply_worker` deletes the directory. Under `out_dir`,
    and therefore under `cwd`, because the server refuses to write outside its roots.
    """
    return dirs.out_dir / SCRATCH_DIRNAME / _safe_name(str(job.get("job_id") or "unknown"))


def _mcp_config(output_dir: Path) -> str:
    """The plugin's `.mcp.json`, with the browser server's output sent to `output_dir`.

    `--output-dir` only covers files the server names itself; an explicit filename
    resolves against the root instead, which is why the prompt hands the model an
    absolute `screenshot_path` — and why the screenshot lands in `out_dir` while
    everything else lands in the per-session `scratch_dir`.
    """
    config = json.loads((paths.ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["playwright"]
    server["args"] = [*server.get("args", []), "--output-dir", str(output_dir)]
    return json.dumps(config)


def _screenshot_name(job: dict) -> str:
    raw = f"{job.get('company') or 'job'}-{job.get('job_id') or 'unknown'}"
    return _safe_name(raw) + ".png"


def build_prompt(job: dict, settings: ApplierSettings, dirs: SessionDirs,
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
            "linkedin_url": settings.linkedin_url,
            "portfolio_url": settings.portfolio_url,
            "work_authorized": settings.work_authorized,
            "requires_sponsorship": settings.requires_sponsorship,
            "willing_to_relocate": settings.willing_to_relocate,
        },
        "resume_path": str(dirs.resume_path),
        "screenshot_path": str(dirs.out_dir / _screenshot_name(job)),
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


async def apply_one(job: dict, settings: ApplierSettings, dirs: SessionDirs,
                    resume_text: str) -> ApplyOutcome:
    """Run one apply session and return what it reported.

    Raises `ApplyLaunchError` when the session never reached a verdict. A timeout or
    an unreadable result is *returned* as an `error` outcome instead, because by then
    the form may already have been submitted — see the module docstring.
    """
    global _apply_proc
    prompt = build_prompt(job, settings, dirs, resume_text)
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
    # It runs in the user's workspace — see `session_dirs` — and never in the sweep's
    # own working directory, ROOT: every plugin update replaces that, and it is outside
    # the roots the browser server may upload the resume from. The config goes as a
    # JSON string, the same way `--json-schema` does, because it carries the session's
    # `scratch_dir` — which the caller deletes once the outcome is recorded.
    scratch = scratch_dir(dirs, job)
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--permission-mode", "auto",
            "--no-session-persistence",
            "--mcp-config", _mcp_config(scratch),
            "--strict-mcp-config",
            "--output-format", "json",
            "--json-schema", schema,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=claude_cli.subscription_env(),
            cwd=str(dirs.cwd),
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
            # One line: the overview's Needs Attention section prints it verbatim.
            error=(f"Timed out after {settings.apply_timeout_s:g}s — check whether it "
                   "was submitted before applying again."),
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
        raise ApplyLaunchError(
            f"claude CLI exited {claude_cli.describe_exit(proc.returncode)}: {detail[:500]}"
        )

    raw = stdout.decode(errors="replace")
    try:
        return ApplyOutcome.model_validate(claude_cli.unwrap_envelope(json.loads(raw)))
    except (json.JSONDecodeError, RuntimeError, ValidationError):
        logger.error("Unreadable apply result for %s: %s", job.get("job_id"), raw[:500])
        return ApplyOutcome(
            status="error",
            error=("Session ended without a result — check whether it was submitted "
                   "before applying again."),
        )


async def run_apply_worker(
    in_q: asyncio.Queue,
    settings: ApplierSettings,
    resume_text: str,
    *,
    run_dir: Path,
    db: Database | None = None,
    include_backlog: bool = True,
    on_progress: Callable[[dict[str, int]], None] | None = None,
    run_id: str | None = None,
) -> dict[str, int]:
    """Apply to each job put on `in_q` until the `None` sentinel. Returns the tallies.

    Jobs are pipeline records: `job_id`, `title`, `company`, `job_url`. The backlog of
    recent shortlisted jobs that were never applied to is worked through first — its
    screenshots land in `run_dir` with the rest, because that is the sweep that applied
    to them, whichever sweep first shortlisted them.

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
    dirs: SessionDirs | None = None
    if resume_path is None or not resume_path.exists():
        blocked = f"resume not found at {settings.resume_path or '(not set)'}"
    else:
        try:
            dirs = session_dirs(resume_path, run_dir)
        except OSError as exc:
            blocked = f"could not prepare the apply directory ({exc})"
    if blocked:
        logger.error("Applier: %s — not applying to anything this sweep.", blocked)

    async def handle(job: dict, from_backlog: bool) -> None:
        job_id = job.get("job_id")
        if not job_id or job_id in attempted:
            return
        attempted.add(job_id)
        company = job.get("company") or ""
        title = job.get("title") or ""

        if company.strip().lower() in excluded:
            # Not a failure, but a verdict all the same: that portal needs an account
            # login, so no sweep can ever complete this job. Recording it is what puts
            # it under the overview's Needs Attention section rather than leaving it
            # under Shortlisted looking like work the applier still has to do — and
            # what takes it out of the backlog, which otherwise re-queued and
            # re-dropped it every sweep for `backlog_hours` with only a log line.
            # Checked before `blocked`: an exclusion holds whatever happened to the
            # resume or the breaker.
            stats["excluded"] += 1
            log = logger.debug if from_backlog else logger.info
            log("Apply manually: %s — %s %s", company, title, job.get("job_url"))
            await asyncio.to_thread(
                db.record_applied, job_id, company, title, job.get("job_url") or "",
                datetime.now(timezone.utc).isoformat(), "excluded", None,
                EXCLUDED_REASON,
            )
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
        # The session's snapshots and console logs are deleted only once it has exited
        # and its outcome is written: it may read them while filling the form, and
        # nothing after that does. `ignore_errors`, because on Windows a just-killed
        # browser can still hold a file, and tidying up is never worth a job.
        try:
            try:
                outcome = await apply_one(job, settings, dirs, resume_text)
            except ApplyLaunchError as exc:
                stats["deferred"] += 1
                state["consecutive"] += 1
                logger.warning(
                    "Apply session failed for %s — %s (will retry next sweep): %s",
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
        finally:
            scratch = scratch_dir(dirs, job)
            shutil.rmtree(scratch, ignore_errors=True)
            try:
                scratch.parent.rmdir()          # only succeeds once it is empty
            except OSError:
                pass
        if on_progress:
            on_progress(dict(stats))

    async def safely(job: dict, from_backlog: bool) -> None:
        try:
            await handle(job, from_backlog)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one job is never worth the sweep
            logger.exception("Applier failed on job %s", job.get("job_id"))
        # The overview's applier bar counts every job this sweep queued, whatever
        # happened to it: an excluded company, a deferral or a location skip writes
        # no `applied` row, and counting rows instead would hold the bar short. The
        # backlog belongs to earlier sweeps' shortlists, so it is left out.
        if run_id and not from_backlog:
            await asyncio.to_thread(db.bump_progress, run_id, apply_handled=1)

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
