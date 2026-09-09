"""The recurring pipeline: sweep, report a line, sleep, repeat.

Started by `/hireshire:start-orchestration` as a background task —
`hireshire.sh --monitor`. It used to be launched by a plugin monitor declared in
`monitors/monitors.json`; that was dropped in 0.2.4 because monitors are an
experimental component that is skipped on some hosts, so the skill's promise that
sweeps had begun was sometimes simply untrue. The skill now starts this itself and
confirms with `--status` before telling the user anything.

Four things shape this file:

* **This is the only entrypoint that honours the user's schedule.** `poll_interval_hours`
  comes from their own config in the data dir. `orchestrate.py` takes `--interval`
  with a 4-hour default and never reads the config, so calling it directly for a
  recurring run silently ignores whatever the user chose at setup.
* **One line of stdout per completed cycle**, everything else to a log file, and the
  Rich live view off (`quiet=True`). Each line is surfaced to the agent, so anything
  chattier turns a background sweep into a stream of interruptions.
* **Exactly one sweeper at a time.** Two would be two writers on one SQLite database
  doing identical work; `hireshire.orchestration_status` decides, and a second start
  exits rather than joining in.
* **It starts on the system interpreter**, which has none of the engine's
  dependencies, so it re-execs itself inside the plugin venv on first entry.

It stays session-scoped, and that is now enforced rather than assumed. Being a child of
the session is not enough on Windows: an orphan there is re-parented in silence — no
process group, no SIGHUP — and this monitor outlived its session more than once, the
last time with a shortlist in hand and auto-apply switched on. Two mechanisms end it,
and neither is sufficient alone:

* the `SessionEnd` hook runs `hireshire.sh --stop` when the session exits in order;
* the heartbeat below watches the session's own pids, for the crash and force-kill
  cases where no hook can fire.

Nothing here may detach, `--stop` stays the manual backstop, and surviving a closed
session *on purpose* is the OS scheduler entry `/hireshire:setup` offers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import DATA, ROOT, is_current, main as bootstrap_main, venv_python  # noqa: E402

_CHILD_FLAG = "HIRESHIRE_IN_VENV"

#: Environment variables naming the session processes this sweep must not outlive.
#: `scripts/hireshire.sh --monitor` is the only thing that sets them, and
#: `_reexec_in_venv` copies the environment down, so they arrive here unaided.
#:
#: Their absence is what keeps the watchdog inert everywhere it would be wrong. The
#: scheduled route `/hireshire:setup` offers runs `orchestrate.py --once` and never
#: this file, so it is unaffected either way — but a sweep with no session behind it
#: must never acquire one by accident, which is why this is opt-in by construction
#: rather than a platform check.
_SESSION_PID_VARS = ("HIRESHIRE_CLI_PID", "HIRESHIRE_SHELL_PID")


def _watched_session_pids() -> dict[str, int]:
    """The session pids to watch, skipping anything absent or unparseable."""
    watched: dict[str, int] = {}
    for name in _SESSION_PID_VARS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid > 0:
            watched[name] = pid
    return watched


def _end_with_session(gone: dict[str, int]) -> None:
    """Leave now, because the session this sweep belongs to is gone.

    Immediate rather than graceful, deliberately. Every job already judged is in the
    `matches` table — `append_result` writes as it goes — so what is lost is the
    employer batch in flight and the end-of-run report, not any work the user paid
    for. Draining the queues instead would keep an unattended sweep alive for minutes
    after the session went, which is the thing being fixed.

    `os._exit` skips interpreter shutdown on purpose: the sweep is parked deep inside
    the pipeline's own awaits, and there is no cancellation path through it that the
    engine supports. The status file is cleared first, by hand, so `--status` is
    correct the instant this returns. Nothing above needs killing — every parent in
    the re-exec chain is blocked in `subprocess.run`, so they unwind on their own the
    moment this process is gone.
    """
    import logging

    import orchestrate
    from hireshire import orchestration_status as status

    logging.warning(
        "Session ended (%s); stopping the sweep.",
        ", ".join(f"{name} {pid} is gone" for name, pid in gone.items()),
    )
    try:
        orchestrate.terminate_apply_subprocess()
    except Exception:  # noqa: BLE001 - nothing may stop the exit below
        logging.exception("Could not terminate the apply subprocess")
    status.clear()
    logging.shutdown()
    os._exit(0)


def _reexec_in_venv() -> int:
    if not is_current():
        rc = bootstrap_main()
        if rc != 0:
            return rc
    env = dict(os.environ)
    env[_CHILD_FLAG] = "1"
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(ROOT))
    env["CLAUDE_PLUGIN_DATA"] = str(DATA)  # informational; the child derives the same from ROOT
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Same reason as run_engine.py: Windows defaults stdout to the console codepage,
    # which cannot encode most of what a job posting contains. This path prints only
    # ASCII summaries today, so it is insurance rather than a fix — but the two
    # launcher entrypoints having different encoding behaviour is how the next
    # summary line that carries a job title becomes a crash nobody is watching.
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    return subprocess.run(
        [str(venv_python()), str(Path(__file__).resolve())],
        cwd=str(ROOT),
        env=env,
    ).returncode


def _loop() -> int:
    import asyncio
    import logging
    import time

    import orchestrate
    from hireshire import orchestration_status as status
    from hireshire import paths
    from hireshire.config import load_config
    from hireshire.storage.db import get_db

    # Logging goes to a file, never stdout — see the module docstring.
    paths.ensure_data_dirs()
    logging.basicConfig(
        level=logging.INFO,
        filename=str(paths.LOGS_DIR / "orchestration.log"),
        # Explicit, because the default is the platform's codepage: on Windows a job
        # title with an em-dash lands in this file as an escaped `—`. Nothing
        # crashes (basicConfig defaults errors to "backslashreplace"), but this log
        # is the only record an unattended sweep leaves, and it should be readable.
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Refuse to become the second sweeper. Both would scrape the same boards and
    # write the same database, and the user would be told twice that orchestration
    # had started. Exit 0: a second request is satisfied by the running one, not an
    # error to report.
    if status.is_running():
        print(f"HireShire: already running — not starting a second.\n{status.describe()}",
              flush=True)
        return 0

    settings = load_config().settings
    interval_h = settings.poll_interval_hours
    db = get_db(settings.db_path)

    apply_enabled = False
    try:
        from hireshire.applier.config import load_applier_config
        apply_enabled = load_applier_config().settings.enable_applier
    except Exception:
        logging.exception("Could not read applier config; continuing without it")

    status.write(pid=os.getpid(), started_at=time.time(), interval_hours=interval_h,
                 apply_enabled=apply_enabled, last_sweep=None, last_summary=None,
                 next_sweep=None)
    print(f"HireShire orchestration started — sweeping every {interval_h:g}h.", flush=True)

    watched = _watched_session_pids()

    async def heartbeat() -> None:
        """Keep the status file fresh for as long as this process lives, and stop the
        moment the session that started it is gone.

        Runs alongside the sweep rather than between cycles: a sweep takes ~20
        minutes, longer than the staleness window, so a heartbeat that only ticked
        at cycle boundaries would read as dead mid-sweep and let a second sweeper in.

        The session check rides along here because this is already the one thing that
        ticks reliably while a sweep is deep in the pipeline. It is the half of the
        teardown that survives a crash: the SessionEnd hook handles an orderly exit,
        but it cannot fire if Claude Code is force-killed, and on Windows nothing else
        will — an orphan there is re-parented in silence.
        """
        from hireshire.process_liveness import is_alive

        if watched:
            logging.info(
                "Watching the session: %s.",
                ", ".join(f"{name} {pid}" for name, pid in watched.items()),
            )
        while True:
            await asyncio.sleep(status.HEARTBEAT_INTERVAL_S)
            gone = {n: p for n, p in watched.items() if not is_alive(p)}
            if gone:
                _end_with_session(gone)
            status.write()

    async def cycles() -> None:
        while True:
            run_id = await orchestrate.run_pipeline(apply=apply_enabled, quiet=True)
            if run_id is None:
                summary = "sweep failed — see logs/orchestration.log"
            else:
                rows = db.load_pipeline_results(run_id)
                scored = [r for r in rows if r.get("relevance_score") is not None]
                best = max((r["relevance_score"] for r in scored), default=None)
                summary = (f"{len(rows)} new match(es) this sweep"
                           + (f", best score {best}" if best is not None else ""))

            status.write(last_sweep=time.time(), last_summary=summary,
                         next_sweep=time.time() + interval_h * 3600)
            print(f"HireShire: {summary}. Next in {interval_h:g}h.", flush=True)
            await asyncio.sleep(interval_h * 3600)

    async def run() -> None:
        beat = asyncio.create_task(heartbeat())
        try:
            await cycles()
        finally:
            beat.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        # Say so immediately instead of leaving a stale file to time out, so the next
        # `--status` is right the moment this stops.
        status.clear()
    return 0


if __name__ == "__main__":
    sys.exit(_loop() if os.environ.get(_CHILD_FLAG) else _reexec_in_venv())
