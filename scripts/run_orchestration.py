"""Monitor wrapper for the recurring pipeline.

Started by monitors/monitors.json when /hireshire:start-orchestration is invoked.
Three constraints from the monitor contract shape this file:

* **Every stdout line becomes a notification.** So this prints exactly one
  summary line per completed cycle and sends everything else to a log file. The
  Rich live view is off (`quiet=True`) for the same reason.
* **A monitor command cannot reference `${user_config.*}`** — Claude Code rejects
  the monitor outright rather than substituting, and monitor processes don't get
  `CLAUDE_PLUGIN_OPTION_*` either. So the poll interval is read here, from the
  user's own config in the data dir.
* **Monitors start on the system interpreter**, which has none of the engine's
  dependencies, so this re-execs itself inside the plugin venv on first entry.

It is also session-scoped: it stops when the Claude Code session ends. Surviving
a closed session needs the OS scheduler entry /hireshire:setup offers.
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
    env["CLAUDE_PLUGIN_DATA"] = str(DATA)  # never let the child re-derive a different one
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(venv_python()), str(Path(__file__).resolve())],
        cwd=str(ROOT),
        env=env,
    ).returncode


def _loop() -> int:
    import asyncio
    import logging

    import orchestrate
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

    settings = load_config().settings
    interval_h = settings.poll_interval_hours
    db = get_db(settings.db_path)

    apply_enabled = False
    try:
        from hireshire.applier.config import load_applier_config
        apply_enabled = load_applier_config().settings.enable_applier
    except Exception:
        logging.exception("Could not read applier config; continuing without it")

    print(f"HireShire orchestration started — sweeping every {interval_h:g}h.", flush=True)

    async def cycles() -> None:
        while True:
            run_id = await orchestrate.run_pipeline(apply=apply_enabled, quiet=True)
            if run_id is None:
                print("HireShire: sweep failed — see logs/orchestration.log", flush=True)
            else:
                rows = db.load_pipeline_results(run_id)
                scored = [r for r in rows if r.get("relevance_score") is not None]
                best = max((r["relevance_score"] for r in scored), default=None)
                print(
                    f"HireShire: {len(rows)} new match(es) this sweep"
                    + (f", best score {best}" if best is not None else "")
                    + f". Next in {interval_h:g}h.",
                    flush=True,
                )
            await asyncio.sleep(interval_h * 3600)

    try:
        asyncio.run(cycles())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(_loop() if os.environ.get(_CHILD_FLAG) else _reexec_in_venv())
