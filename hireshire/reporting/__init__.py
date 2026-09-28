"""HTML reports for a run: the overview page, at three scopes.

The engine writes these, not the agent. That is the whole design decision, and
three things follow from it:

* It runs on **every** sweep, including the unattended ones the recurring monitor
  drives, where no agent turn exists to generate anything.
* It costs no tokens and cannot vary between runs.
* It keeps the skills honest. A skill must not state runtime facts it has not
  asked for; here it hands the user a path to a file the engine wrote rather than
  numbers it assembled itself.

Nothing here is published. Both pages are complete local documents opened over
``file://`` from the user's own results folder, which is what licenses their meta
refresh — a published artifact could not reload itself. The dashboard and the
per-run matching report used to sit alongside them and are gone: three pages
answered overlapping questions, and the overview is the one that answers *what have
I got* at both scopes.

`refresh()` is the only entry point, and it is safe to call from the pipeline's
progress callbacks: it throttles, and it swallows everything. A report is a
diagnostic, and losing one must never take down a twenty-minute sweep whose CSV,
JSON and database rows are already on disk — the same trade
`hireshire.results_export.write_results_csv` makes, for the same reason.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from hireshire import paths, run_ids as run_id_utils
from hireshire.reporting import data, overview
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
    """Where this run's report files go. Two keys, or three.

    The lifetime page sits at the results root, which is what gives the skills a
    fixed path to hand the user run after run; the stamped one lives with the run
    it describes, beside that run's CSV.

    The day page sits in the day folder — `results_dir.parent` — and the key is
    **absent** unless that folder is really this run's day. Two reasons for taking it
    off `results_dir` rather than resolving it from `paths`:

    * `paths.run_dir_for(stamp).parent` would name the *configured* workspace even when
      `make_run_dir` had already fallen back to `DATA/results/` because the drive is
      unplugged. `overview.write` swallows the `OSError` and returns `None`, so the page
      would be lost with nothing but a warning. Taking the parent of the folder the run
      actually got follows the fallback for free.
    * The absent key is how a run with no day folder says so, which is what keeps this
      honest for the run folders that predate the day layout: they sit flat at the
      results root, and an unguarded `.parent` would drop a day page beside
      `Dashboard_Lifetime.html` — duplicating the one a new-layout sweep writes inside
      the day folder the same day, with neither authoritative.
    """
    root = paths.results_root()
    targets = {
        "overview": root / overview.LIFETIME_NAME,
        "run_overview": results_dir / overview.run_overview_name(stamp),
    }
    day = run_id_utils.day_of(stamp)
    if day and results_dir.parent.name == day:
        targets["day_overview"] = results_dir.parent / overview.day_overview_name(day)
    return targets


def refresh(run_id: str, results_dir: Path, stamp: str, final: bool = False) -> None:
    """Rebuild both overview pages from the database.

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

    `snapshot` and `records` are loaded here and handed to the page rather than
    re-derived inside it. The overview used to load the identical rows a second
    time, which on a clock-driven refresh is the same query ~300 times a sweep for
    nothing.

    The day page rides the lifetime throttle, and the reason is the **render**, not the
    SQL: its queries are `run_id IN (<a day's worth>)` and index-backed, so they are
    nothing like the lifetime ones. What costs is a third full `build()` of up to 5,300
    rows. Verifying that the queries are cheap is therefore not a reason to move it onto
    the fast tick. It shares `_last_lifetime` because the two are always written
    together, so one timestamp is the honest record of when they last were.
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
        # One indexed read. The lifetime page reads its own totals below, on its
        # slower throttle, because those group whole tables.
        progress = db.run_progress(run_id)

        targets = report_paths(results_dir, stamp)
        overview.write(
            data.overview_snapshot(db, run_id, run=snapshot, records=records,
                                   progress=progress),
            targets["run_overview"], stamp,
        )
        # The lifetime and day pages ride their own, slower throttle — but never skip
        # the final write, which is the only one that sees the completed run.
        if final or now - _last_lifetime >= LIFETIME_INTERVAL_S:
            _last_lifetime = now
            overview.write(
                data.overview_snapshot(db, None, live=snapshot.get("in_progress"),
                                       progress=db.lifetime_progress()),
                targets["overview"],
            )
            day_target = targets.get("day_overview")
            if day_target is not None:
                day = run_id_utils.day_of(stamp)
                day_ids = data.day_run_ids(db, day)
                # An empty list would render zeros under a heading naming a day that
                # does have sweeps in it, so skip rather than publish that. It can only
                # happen if this run is in neither `run_progress` nor `runs`, which a
                # standalone phase run is not.
                if day_ids:
                    overview.write(
                        data.overview_snapshot(
                            db, None, run_ids=day_ids,
                            live=snapshot.get("in_progress"),
                            progress=db.lifetime_progress(run_ids=day_ids),
                        ),
                        day_target, day,
                    )
    except Exception:  # noqa: BLE001
        # Never propagate. This is called from inside the pipeline's own callbacks
        # and from its finaliser; an exception here would fail a run whose real
        # output is already safe.
        logger.exception("Report refresh failed for run %s", run_id)
