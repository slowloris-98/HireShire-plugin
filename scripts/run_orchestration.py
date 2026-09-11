"""The recurring pipeline: sweep, report a line, sleep, repeat.

Started by `/hireshire:start-orchestration` as a background task —
`hireshire.sh --monitor`. `--once` is the same program with the loop stopped after one
cycle, and is what `/hireshire:find-jobs` and the OS scheduler entry run.

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
user's work, and the next host breaks it again. What replaces it is `_MAX_RUNTIME_S`: the
loop cannot run forever, so an unattended sweep with auto-apply on has a bound without
anyone watching a pid. `--stop` is the manual backstop, and surviving a closed terminal
*on purpose* is the OS scheduler entry `/hireshire:setup` offers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import DATA, ROOT, is_current, main as bootstrap_main, venv_python  # noqa: E402

_CHILD_FLAG = "HIRESHIRE_IN_VENV"

#: How long a recurring sweep may run before it stops on its own.
#:
#: This is the whole safety story now that nothing watches a session. An unattended
#: monitor with `enable_applier: true` submits real applications with no human
#: checkpoint, and before this it could do so indefinitely — the incident that
#: motivated the watchdog was exactly that, a sweep outliving its session with a
#: shortlist in hand and auto-apply on. A bound achieves the same protection without
#: asking the host a question it may not be able to answer.
#:
#: Not configurable on purpose: a setting invites `0` or `99999`, and this is a
#: backstop rather than a preference. The user who genuinely wants unattended sweeps
#: forever has the OS scheduler, which is honest about what it is.
_MAX_RUNTIME_S = 24 * 3600


def _another_cycle_fits(now: float, deadline: float, interval_s: float) -> bool:
    """Whether there is room for one more sweep before the bound.

    Asked *before* sleeping rather than after waking, so the loop never parks a process
    for four hours only to exit the moment it comes back. A sweep that would finish
    past the deadline is not started at all.

    Its own function because it is the whole of the safety story and the rest of the
    loop is untestable without a database, a config file and a scrape.
    """
    return now + interval_s < deadline


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
    import time

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
    try:
        from hireshire.applier.config import load_applier_config
        apply_enabled = load_applier_config().settings.enable_applier
    except Exception:
        logging.exception("Could not read applier config; continuing without it")

    sweep_pid.write(os.getpid())
    deadline = time.monotonic() + _MAX_RUNTIME_S
    print(
        "HireShire: sweeping once." if once
        else f"HireShire orchestration started — sweeping every {interval_h:g}h."
             f" It stops on its own after {_MAX_RUNTIME_S // 3600}h,"
             " or when you run --stop.",
        flush=True,
    )

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

            if not _another_cycle_fits(time.monotonic(), deadline, interval_h * 3600):
                print(f"HireShire: {summary}. Stopping —"
                      f" the {_MAX_RUNTIME_S // 3600}h limit is up."
                      " Run /hireshire:start-orchestration again to continue.",
                      flush=True)
                logging.info("Reached the %ss runtime bound; stopping.", _MAX_RUNTIME_S)
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
    # `--once` is what `/hireshire:find-jobs` and the OS scheduler use. It is the same
    # program as the recurring sweep with the loop stopped after one cycle, which is the
    # point: find-jobs used to run `orchestrate.py --once` through `run_engine.py`, a
    # second program that registered nothing, so a find-jobs sweep was unreachable by
    # `--stop` and invisible to the duplicate guard. One leaf, one pid file.
    _once = "--once" in sys.argv[1:]
    sys.exit(_loop(_once) if os.environ.get(_CHILD_FLAG) else _reexec_in_venv())
