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

It stays session-scoped: started as a child of the session, it dies with it. That is
what the skill tells the user, so nothing here may detach. Surviving a closed session
is the OS scheduler entry `/hireshire:setup` offers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import DATA, ROOT, is_current, main as bootstrap_main, venv_python  # noqa: E402

_CHILD_FLAG = "HIRESHIRE_IN_VENV"


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

    async def heartbeat() -> None:
        """Keep the status file fresh for as long as this process lives.

        Runs alongside the sweep rather than between cycles: a sweep takes ~20
        minutes, longer than the staleness window, so a heartbeat that only ticked
        at cycle boundaries would read as dead mid-sweep and let a second sweeper in.
        """
        while True:
            await asyncio.sleep(status.HEARTBEAT_INTERVAL_S)
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
