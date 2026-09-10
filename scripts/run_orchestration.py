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

#: Claude Code publishes its own process id here, and it is the only session signal
#: this file trusts. `_reexec_in_venv` copies the environment down, so it arrives at
#: the leaf unaided — nothing has to pass it along.
#:
#: The obvious alternative — have `hireshire.sh --monitor` hand over `$$` and `$PPID`
#: — shipped once and was a real bug. Git Bash is MSYS, MSYS keeps its **own** pid
#: namespace, and `is_alive` asks Win32 about Windows pids, so the watchdog polled two
#: numbers that meant nothing in the namespace it was querying, read them as dead, and
#: stopped a healthy sweep on its first tick. Measured: `ps` reports PID 1684 for a
#: shell that Windows calls WINPID 14072.
#:
#: Walking the process tree instead does not work either, and the reason is worth
#: keeping. The ancestry measured under the VS Code extension was
#: `python -> bash -> bash -> bash -> claude.exe -> Code.exe`: three shell levels, and
#: nothing pins that depth, so no `getppid()`-and-its-parent rule can be correct.
_SESSION_PID_VAR = "CLAUDE_PID"

#: How often the watchdog asks whether the session is still there.
#:
#: Deliberately not `status.HEARTBEAT_INTERVAL_S`, which it used to share. That one is
#: chosen against `STALE_AFTER_S` — how long a status file may look live after the
#: sweeper dies. This one is what the user feels: the gap between closing the CLI and
#: the sweep, plus its `claude -p` children, actually stopping. Tying them made a
#: closed session take up to a minute to take effect for no reason. The probe is a
#: single `OpenProcess` call, so five seconds costs nothing measurable.
_WATCHDOG_INTERVAL_S = 5


def _session_pid() -> int | None:
    """The session process to watch, or None when there is nothing trustworthy.

    **Absence must mean "do not arm".** A plain terminal, a different interface, or the
    OS scheduler route all reach this file with no session behind them, and a watchdog
    that guessed there would stop sweeps nobody asked it to. Unknown never means kill:
    the `SessionEnd` hook still covers an orderly exit, and `--stop` is always there.

    Only "the user closed the CLI" and a crash are covered by design. Killing the
    background Bash task alone leaves this sweep running until one of those two: the
    CLI's pid is a measured signal, a parent-shell pid would have been a reasoned one,
    and a false stop is the failure that already cost a real run.
    """
    raw = os.environ.get(_SESSION_PID_VAR, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _end_with_session(pid: int) -> None:
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
        "Session ended (%s %s is gone); stopping the sweep.", _SESSION_PID_VAR, pid
    )
    try:
        orchestrate.terminate_apply_subprocess()
    except Exception:  # noqa: BLE001 - nothing may stop the exit below
        logging.exception("Could not terminate the apply subprocess")
    status.clear()
    logging.shutdown()

    # Take the whole tree, not just this process. The scorer runs up to `concurrency`
    # `claude -p` children at a time, and `os._exit` would leave every one of them
    # re-parented and running — silently on Windows, where an orphan gets no signal
    # at all. They would eventually hit `claude_cli_timeout_s` (600 s by default), but
    # "the plugin stops when the session stops" has to mean now, not in ten minutes.
    #
    # Killing self tree-wide is enough for everything above, too: each parent in the
    # re-exec chain is blocked in `subprocess.run` and unwinds as soon as its child is
    # gone. `os._exit` below is the fallback for the platform paths that do not take
    # this process down themselves.
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                           capture_output=True)
        else:
            subprocess.run(["pkill", "-KILL", "-P", str(os.getpid())],
                           capture_output=True)
    except Exception:  # noqa: BLE001 - the exit below must happen regardless
        pass
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

    session_pid = _session_pid()

    # `session_pid` is recorded so the SessionEnd hook can tell *whose* sweep this is.
    # Without it that hook stops whatever the status file names, so with several Claude
    # Code sessions open, any one of them ending kills another session's sweep. Written
    # here rather than derived later because this is the only moment the owning session
    # is known for certain. None is written when the watchdog is unarmed — the scheduled
    # route has no session, and an absent owner must not read as "owned by everyone".
    status.write(pid=os.getpid(), started_at=time.time(), interval_hours=interval_h,
                 apply_enabled=apply_enabled, last_sweep=None, last_summary=None,
                 next_sweep=None, session_pid=session_pid,
                 mode="once" if once else "recurring")
    print(
        "HireShire: sweeping once." if once
        else f"HireShire orchestration started — sweeping every {interval_h:g}h.",
        flush=True,
    )

    async def heartbeat() -> None:
        """Keep the status file fresh for as long as this process lives.

        Runs alongside the sweep rather than between cycles: a sweep takes ~20
        minutes, longer than the staleness window, so a heartbeat that only ticked
        at cycle boundaries would read as dead mid-sweep and let a second sweeper in.
        """
        while True:
            await asyncio.sleep(status.HEARTBEAT_INTERVAL_S)
            status.write()

    async def watchdog() -> None:
        """Stop the moment the session that started this sweep is gone.

        The half of the teardown that survives a crash: the SessionEnd hook handles an
        orderly exit, but it cannot fire if Claude Code is force-killed, and on Windows
        nothing else will — an orphan there is re-parented in silence.

        **Separate from the heartbeat, and ticking far faster.** It used to ride along
        with the status write, which tied how quickly a sweep noticed a dead session to
        how often the status file needs refreshing. Those are unrelated: the status
        interval is chosen against `STALE_AFTER_S`, while this one is the delay the user
        experiences between closing the CLI and the sweep — plus its `claude -p`
        children — actually stopping. A minute of that is a sweep that outlives its
        session for a minute.
        """
        from hireshire.process_liveness import is_alive

        if session_pid is None:
            logging.info(
                "No usable %s in the environment, so the session watchdog is NOT "
                "armed; this sweep will not notice a closed CLI. The SessionEnd hook "
                "and `--stop` still apply.",
                _SESSION_PID_VAR,
            )
            return

        logging.info("Watching the session: %s %s.", _SESSION_PID_VAR, session_pid)
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            if not is_alive(session_pid):
                _end_with_session(session_pid)

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
                # One cycle is the whole job: `next_sweep` would be a promise nothing
                # is left running to keep, and `--status` renders it as a due date.
                status.write(last_sweep=time.time(), last_summary=summary)
                print(f"HireShire: {summary}.", flush=True)
                return

            status.write(last_sweep=time.time(), last_summary=summary,
                         next_sweep=time.time() + interval_h * 3600)
            print(f"HireShire: {summary}. Next in {interval_h:g}h.", flush=True)
            await asyncio.sleep(interval_h * 3600)

    async def run() -> None:
        beat = asyncio.create_task(heartbeat())
        guard = asyncio.create_task(watchdog())
        try:
            await cycles()
        finally:
            beat.cancel()
            guard.cancel()

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
    # `--once` is what `/hireshire:find-jobs` and the OS scheduler use. It is the same
    # program as the recurring sweep with the loop stopped after one cycle, which is the
    # point: find-jobs used to run `orchestrate.py --once` through `run_engine.py`, a
    # second program with no status registration and no session watchdog, so every
    # teardown mechanism the plugin has was built on the monitor and simply did not
    # cover it. One leaf, one registration, one watchdog.
    _once = "--once" in sys.argv[1:]
    sys.exit(_loop(_once) if os.environ.get(_CHILD_FLAG) else _reexec_in_venv())
