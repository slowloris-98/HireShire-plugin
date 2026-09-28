"""A run's two names, and the calendar day they put it under.

Every sweep carries two strings and they are deliberately different:

* ``run_id`` — ``%Y-%m-%dT%H-%M-%SZ``, **UTC**. The key across five tables, so it
  has to sort and must not collide when the clock goes back an hour at the end of
  DST.
* ``stamp`` — ``%Y-%m-%d_%H%M%S``, **local**. Display only: the results folder and
  the files in it, read by a human browsing their own directory.

The day a sweep belongs to is the date in its **stamp**, not its run id. A run at
21:30 local on the 27th is `2026-09-27T…Z` or `2026-09-28T…Z` depending on the
offset, so bucketing on the run id would file a sweep under a date the user never
sees. That is why there is no ``day_window`` here returning UTC bounds: a range
recomputed at render time can silently exclude the very sweeps in the folder it
names — the user travels, the machine's offset changes — and the page and its
folder would disagree with nothing saying why. ``day_of_run_id`` goes the other
way instead: it is *by construction* the function that produced the folder name,
so the two cannot drift.

Stdlib only, and it must stay that way. ``hireshire/paths.py`` and
``hireshire/reporting/`` both need these, and ``orchestrate.py`` imports both — so
anything heavier here would be an import cycle. ``orchestrate._run_stamp`` and
``orchestrate._run_started_at`` are thin delegates onto this module.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, tzinfo

__all__ = [
    "RUN_ID_FMT",
    "STAMP_FMT",
    "day_of",
    "day_of_run_id",
    "run_stamp",
    "run_started_at",
]

RUN_ID_FMT = "%Y-%m-%dT%H-%M-%SZ"   # UTC, the DB key
STAMP_FMT = "%Y-%m-%d_%H%M%S"       # local, the folder name

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def run_stamp(now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """The run's display stamp, used for the results folder and the files in it.

    LOCAL time, unlike `run_id`, because this one is read by a human browsing
    their own folder — a run at 14:30 filed under `090005` would be a bug report.
    It is never a key: `run_id` stays UTC so it is monotonic and cannot collide
    when the clock goes back an hour at the end of DST.

    `tz` overrides the system zone. Production never passes it; it exists so the
    DST behaviour is testable on a machine where `time.tzset()` does not exist
    (Windows) and `zoneinfo` has no database to read (no `tzdata` dependency).
    """
    now = now or datetime.now(timezone.utc)
    return now.astimezone(tz).strftime(STAMP_FMT)


def run_started_at(run_id: str) -> datetime | None:
    """The instant a run id names, or None if it is not one."""
    try:
        return datetime.strptime(run_id, RUN_ID_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def day_of(stamp: str) -> str:
    """The calendar day a stamp belongs to — ``"2026-09-27"`` — or ``""``.

    Validated rather than sliced. `orchestrate.finalise_abandoned_runs` falls back
    to `stamp = run_id` when a run id will not parse, and a bare `stamp[:10]` would
    then build a folder name out of a fragment of one. `""` is the signal for "no
    day layout and no day page", which every caller degrades to the flat, pre-day
    behaviour on.
    """
    head = (stamp or "")[:10]
    return head if _DAY_RE.fullmatch(head) else ""


def day_of_run_id(run_id: str, tz: tzinfo | None = None) -> str:
    """The day a run belongs to, derived exactly as its folder name was.

    This is the whole basis of the day scope: the page's set of runs is chosen by
    the same composition that named the directory the page sits in, so a sweep
    cannot be in the folder and absent from the numbers.
    """
    began = run_started_at(run_id)
    return day_of(run_stamp(began, tz)) if began else ""
