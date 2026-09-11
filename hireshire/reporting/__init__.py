"""HTML reports for a run: a live local dashboard and a publishable match report.

The engine writes these, not the agent. That is the whole design decision, and
three things follow from it:

* It runs on **every** sweep, including the unattended ones the recurring monitor
  drives, where no agent turn exists to generate anything.
* It costs no tokens and cannot vary between runs.
* It keeps the skills honest. A skill must not state runtime facts it has not
  asked for; here it publishes a file the engine handed it rather than numbers it
  assembled itself.

`refresh()` is the only entry point, and it is safe to call from the pipeline's
progress callbacks: it throttles, and it swallows everything. A report is a
diagnostic, and losing one must never take down a twenty-minute sweep whose CSV,
JSON and database rows are already on disk — the same trade
`hireshire.results_export.write_all_jobs_csv` makes, for the same reason.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from hireshire import paths
from hireshire.reporting import dashboard, data, matching, overview
from hireshire.storage.db import get_db

logger = logging.getLogger(__name__)

__all__ = ["refresh", "report_paths", "MIN_INTERVAL_S", "LIFETIME_INTERVAL_S"]

# Floor between two throttled refreshes. The sweep calls this from a per-company
# callback that fires thousands of times, so without a floor the reports would
# dominate a run that is otherwise waiting on rate limits.
MIN_INTERVAL_S = 10.0

# The lifetime overview gets a slower one of its own. Its queries group the whole
# `matches` table, which on a mature install is six figures of rows; everything else
# here is indexed on `run_id` and stays cheap however long the user has been at it.
LIFETIME_INTERVAL_S = 60.0

_last_refresh = 0.0
_last_lifetime = 0.0


def report_paths(results_dir: Path, stamp: str) -> dict[str, Path]:
    """Where this run's report files go.

    The stamped ones live with the run they describe; `latest_matching.html`, the
    dashboard and the lifetime overview sit at the results root, which is what gives
    the skills a fixed path to publish from run after run.
    """
    root = paths.results_root()
    return {
        "matching": results_dir / matching.matching_name(stamp),
        "latest_matching": root / matching.LATEST_NAME,
        "dashboard": root / dashboard.DASHBOARD_NAME,
        "overview": root / overview.OVERVIEW_NAME,
        "run_overview": results_dir / overview.run_overview_name(stamp),
    }


def refresh(run_id: str, results_dir: Path, stamp: str, final: bool = False) -> None:
    """Rebuild both reports from the database.

    `final=True` bypasses the throttle and is used once, from
    `_finalise_pipeline`, after every other output file is written — that call is
    the one that must not be skipped, because it is the only one that sees the
    completed run.

    Cost, for the throttled calls: `matches` now fills from the first employer on,
    because selection is a streaming per-job cutoff rather than a global top-K
    resolved at the sentinel. So the row load below is real work for most of a sweep,
    and what keeps an early refresh cheap is the `snapshot["candidates"]` guard, not
    an empty table. Do not restore the old claim — a reader who believed it would
    conclude these calls are free and remove the throttle.
    """
    global _last_refresh, _last_lifetime

    now = time.monotonic()
    if not final and now - _last_refresh < MIN_INTERVAL_S:
        return
    _last_refresh = now

    try:
        db = get_db()
        snapshot = data.run_snapshot(db, run_id)
        # Only pay for the row load once there is something to load. `rows_total`
        # is a COUNT, so this check is free.
        records = db.load_all_matches(run_id) if snapshot["candidates"] else []

        targets = report_paths(results_dir, stamp)
        matching.write(snapshot, records, stamp, results_dir, targets["latest_matching"])
        dashboard.write(data.dashboard_snapshot(db), targets["dashboard"])

        overview.write(
            data.overview_snapshot(db, run_id, run=snapshot),
            targets["run_overview"], stamp,
        )
        # The lifetime page rides its own, slower throttle — but never skips the
        # final write, which is the only one that sees the completed run.
        if final or now - _last_lifetime >= LIFETIME_INTERVAL_S:
            _last_lifetime = now
            overview.write(
                data.overview_snapshot(db, None, live=snapshot.get("in_progress")),
                targets["overview"],
            )
    except Exception:  # noqa: BLE001
        # Never propagate. This is called from inside the pipeline's own callbacks
        # and from its finaliser; an exception here would fail a run whose real
        # output is already safe.
        logger.exception("Report refresh failed for run %s", run_id)
