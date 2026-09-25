"""The recurring pipeline: sweep, report a line, sleep, repeat.

Started by `/hireshire:start-orchestration` as a background task —
`hireshire.sh --monitor`. `--once` is the same program with the loop stopped after one
cycle, and is what the OS scheduler entry runs.

Four things shape this file:

* **This is the only entrypoint that honours the user's schedule.** `poll_interval_hours`
  comes from their own config in the data dir. `orchestrate.py` takes `--interval`
  with a 4-hour default and never reads the config, so calling it directly for a
  recurring run silently ignores whatever the user chose at setup.
* **One line of stdout per completed cycle**, everything else to a log file, and the
  Rich live view off (`quiet=True`). Each line is surfaced to the agent, so anything
  chattier turns a background sweep into a stream of interruptions.
* **Exactly one sweeper at a time.** Two would be two writers on one SQLite database
  doing identical work, so a second start reads the pid file and exits.
* **It starts on the system interpreter**, which has none of the engine's
  dependencies, so it re-execs itself inside the plugin venv on first entry.

**This file knows nothing about Claude Code sessions, and must not learn.** It used to:
it read `CLAUDE_PID` and killed itself when that process disappeared, while a
`SessionEnd` hook killed it from the outside. Both depended on `CLAUDE_PID` existing,
which is not guaranteed — on a fresh Windows install it was simply absent, so the
watchdog never armed *and* the hook's "no recorded owner" fallback let every ending
session on the machine reap the sweep. Since the sweep spawns a `claude -p` per scoring
call, it manufactured its own killers: dead 60 s in, exit code 1, no traceback, on every
sweep path including `--once`.

The lesson is not "pick a better session signal". It is that a sweep must not depend on
host-specific identity it cannot verify, because the failure direction is destroying the
user's work, and the next host breaks it again.

It also has no runtime bound. A 24-hour cap used to stand in for the session tie, and
it stopped sweeps the user wanted running. A recurring sweep now ends only on `--stop`
or when its process is killed. With `enable_applier: true` it therefore keeps
submitting applications, unattended, until one of those happens. Surviving a closed
terminal *on purpose* is still the OS scheduler entry `/hireshire:setup` offers.
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
    # Same reason as run_engine.py: Windows defaults stdout to the console codepage,
    # which cannot encode most of what a job posting contains. This path prints only
    # ASCII summaries today, so it is insurance rather than a fix — but the two
    # launcher entrypoints having different encoding behaviour is how the next
    # summary line that carries a job title becomes a crash nobody is watching.
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    return subprocess.run(
        # argv is forwarded so `--once` survives the hop into the venv. Without this the
        # child always ran recurring, and a one-shot sweep would never return.
        [str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(ROOT),
        env=env,
    ).returncode


def _loop(once: bool = False) -> int:
    import asyncio
    import logging

    import orchestrate
    from hireshire import paths, sweep_pid
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
    #
    # A recorded pid is only a reason to refuse if that process actually exists — a
    # sweep killed from Task Manager leaves the file behind, and a stale file that
    # blocked every future sweep would be a worse bug than the one being prevented.
    # `is_alive` is safe to trust here in a way `CLAUDE_PID` never was: this pid is one
    # the sweeper wrote about *itself*, so there is no guessed identity involved.
    running = sweep_pid.read()
    if running is not None:
        from hireshire.process_liveness import is_alive
        if is_alive(running):
            print(f"HireShire: already running (pid {running}) — not starting a second.",
                  flush=True)
            return 0

    settings = load_config().settings
    interval_h = settings.poll_interval_hours
    db = get_db(settings.db_path)

    apply_enabled = False
    applier_unreadable = False
    try:
        from hireshire.applier.config import load_applier_config
        apply_enabled = load_applier_config().settings.enable_applier
    except Exception:
        logging.exception("Could not read applier config; continuing without it")
        applier_unreadable = True

    # No sweep is running (the guard above just established it), so any run still
    # without its pipeline `runs` row was killed before its `finally` — a shell task
    # killed directly, Task Manager, a power cut. Close it out now, or its dashboards
    # read "running" for good. `--stop` does the same thing straight after its kill.
    try:
        asyncio.run(orchestrate.finalise_abandoned_runs())
    except Exception:  # noqa: BLE001 - never worth refusing a sweep over
        logging.exception("Could not finalise abandoned runs")

    sweep_pid.write(os.getpid())
    print(
        "HireShire: sweeping once." if once
        else f"HireShire orchestration started — sweeping every {interval_h:g}h."
             " It runs until you run --stop.",
        flush=True,
    )
    # Said on stdout, not only logged: this is read once per start, so an unreadable
    # file turns off the feature the user enabled for every cycle until a restart —
    # and a log line alone is how a bare `disability: no` did exactly that unnoticed.
    if applier_unreadable:
        print("HireShire: auto-apply is OFF — the applier settings could not be read "
              "(see logs/orchestration.log).", flush=True)

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

            if once:
                print(f"HireShire: {summary}.", flush=True)
                return

            print(f"HireShire: {summary}. Next in {interval_h:g}h.", flush=True)
            await asyncio.sleep(interval_h * 3600)

    try:
        asyncio.run(cycles())
    except KeyboardInterrupt:
        pass
    finally:
        # A pid file outliving its process would make the duplicate guard refuse the
        # user's next sweep. The guard probes liveness for the cases this cannot cover
        # — a kill -9, a power cut — but a clean exit should leave nothing behind.
        sweep_pid.clear()
    return 0


if __name__ == "__main__":
    # `--once` is what the OS scheduler uses. It is the same program as the recurring
    # sweep with the loop stopped after one cycle, which is the point: a one-shot sweep
    # through `orchestrate.py --once` registers nothing, so it is unreachable by
    # `--stop` and invisible to the duplicate guard. One leaf, one pid file.
    _once = "--once" in sys.argv[1:]
    sys.exit(_loop(_once) if os.environ.get(_CHILD_FLAG) else _reexec_in_venv())
