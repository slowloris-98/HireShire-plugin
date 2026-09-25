"""Close out a sweep that was killed, so its dashboards stop reading "running".

Run by `bootstrap.py --stop` right after the kill, inside the venv. A forced kill
never reaches `run_pipeline`'s `finally`, which is what writes the `runs` row the
pages read to decide whether a sweep is live; see
`orchestrate.finalise_abandoned_runs`.

A no-op while a sweep is still alive: the in-flight run looks exactly like an
abandoned one, and finalising it would disarm the page the user is watching.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from hireshire import paths, sweep_pid
    from hireshire.process_liveness import is_alive

    paths.ensure_data_dirs()
    logging.basicConfig(
        level=logging.INFO,
        filename=str(paths.LOGS_DIR / "orchestration.log"),
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    running = sweep_pid.read()
    if running is not None and is_alive(running):
        return 0

    import orchestrate

    stamps = asyncio.run(orchestrate.finalise_abandoned_runs())
    for stamp in stamps:
        print(f"HireShire: dashboards updated for stopped run {stamp}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
