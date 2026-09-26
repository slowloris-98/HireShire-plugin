"""The per-company cap: at most `max_per_company` submissions per `company_window_hours`.

One rule, two readers. The apply worker asks it before launching a session, and the
overview page asks it to label a shortlisted job the next sweep will hold — so the page
cannot promise a retry the worker would not make, or stay silent about a hold it will.

**A hold is a deferral, never a verdict.** "This employer has had two applications this
week" changes with time, so the worker writes nothing: the job stays shortlisted with no
`applied` row, and `load_pending_applications` hands it back on every sweep until the
oldest counted submission ages out. Only `submitted` counts, which includes a job the
user marked applied by hand — that writes plain `submitted` too.

Companies are keyed on `board_token`, lowercased and trimmed: the key
`exclude_companies` matches on, and what `record_applied` stores.

Stdlib only, so the reporting package can import it without pulling in the worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: The defaults, here rather than only on `ApplierSettings` because the report reads
#: `applier.yaml` straight out of the file (it must not import the pydantic loaders) and
#: has to fall back to the same numbers the worker does.
DEFAULT_MAX_PER_COMPANY = 2
DEFAULT_WINDOW_H = 72

#: How far past the cap window the backlog keeps looking, so a job held on the day it
#: was scored is still in the backlog on the first sweep after its slot frees. The slot
#: frees `window` after the oldest counted submission, and that submission can be
#: minutes *after* the held job's `scored_at` (siblings of one employer's batch are
#: scored together, then applied to one at a time). Covers a poll interval up to a day.
BACKLOG_MARGIN_H = 24


def company_key(company: str | None) -> str:
    return (company or "").strip().lower()


def enabled(settings) -> bool:
    return settings.max_per_company > 0 and settings.company_window_hours > 0


def window_start(settings, now: datetime) -> datetime:
    return now - timedelta(hours=settings.company_window_hours)


def backlog_window_hours(settings) -> int:
    """How far back the backlog (and, on its other side, the expiry pass) looks.

    Both loaders must use the same number or they stop partitioning the shortlist —
    a gap would strand a job, an overlap would expire one still being retried.
    """
    if not enabled(settings):
        return settings.backlog_hours
    return max(settings.backlog_hours, settings.company_window_hours + BACKLOG_MARGIN_H)


def hold_until(stamps: list[str], settings, now: datetime) -> datetime | None:
    """When the company's next slot opens, or `None` if it has one now.

    `stamps` are the `applied_at` values of the company's submissions. Those outside the
    window are ignored here as well as in the query, so a caller may pass a wider set.
    Unparseable stamps are ignored: a hold is a deferral, so a garbled row must not be
    able to hold a job forever.
    """
    if not enabled(settings):
        return None
    start = window_start(settings, now)
    inside = []
    for s in stamps:
        try:
            t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        if t >= start:
            inside.append(t)
    if len(inside) < settings.max_per_company:
        return None
    # The slot opens when enough of the oldest have aged out to bring the count below
    # the cap: with a cap of 2 and three inside, that is the second-oldest, not the first.
    inside.sort()
    return inside[len(inside) - settings.max_per_company] + timedelta(
        hours=settings.company_window_hours)
