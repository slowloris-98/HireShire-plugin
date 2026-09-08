"""The dashboard: every sweep this install has done, on one local page.

Deliberately **not** an artifact. It sits at the root of the user's results folder
next to the per-run directories, is opened with `file://`, and is never published
— so unlike the matching report it is a complete HTML document and it may carry a
meta refresh.

That refresh is the whole reason this page is worth having during a run. The
scrape counts genuinely stream: `run_companies` and `jobs` fill continuously for
the ~20 minutes a sweep takes. So a user who leaves this open watches the numbers
climb, which is the thing a spinner in a terminal cannot tell them.

The refresh is armed **only while a run is in progress**. A finished run that kept
reloading would look like one that never ended.

One honest limitation is stated on the page rather than papered over: the `applied`
table has no `run_id` — an application is a fact about a job, not about the sweep
that surfaced it — so the applications figure is a lifetime total for the install
and cannot be broken down per run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hireshire.reporting.render import SHOW_COST, document, e, funnel_step, local_time, num, usd

logger = logging.getLogger(__name__)

TITLE = "HireShire Control Room"
DASHBOARD_NAME = "dashboard.html"

# Long enough not to thrash a browser, short enough that the page feels live
# against an engine that regenerates it at most every 10 seconds.
REFRESH_S = 15

EXTRA = """
.run-row a { color: var(--accent); }
.pill {
  display: inline-block; font-size: .62rem; letter-spacing: .08em; text-transform: uppercase;
  padding: .15rem .45rem; border-radius: 2px; border: 1px solid var(--rule); color: var(--ink-faint);
}
.pill.live { color: var(--warn); border-color: var(--warn); }
.pill.hit { color: var(--accent); border-color: var(--accent); }
"""


def _live_panel(live: dict[str, Any] | None) -> str:
    if not live:
        return (
            '<p class="note calm"><b>No sweep running.</b> '
            "Start one with <code>/hireshire:find-jobs</code> for a single pass, or "
            "<code>/hireshire:start-orchestration</code> to keep sweeping on a schedule.</p>"
        )
    jobs = live["jobs"]
    steps = [
        funnel_step("Employers swept", live["companies"], live["companies"],
                    f"{num(live['companies_with_jobs'])} had at least one opening"),
        funnel_step("Postings found", jobs, max(jobs, 1)),
    ]
    return (
        '<p class="note"><b>A sweep is running.</b> Jobs are scored as they are found, '
        "one employer at a time, so the scored and shortlisted figures below climb "
        "throughout the run rather than arriving all at once at the end. This page "
        f"reloads itself every {REFRESH_S} seconds while the sweep is going.</p>"
        f'<div class="funnel">{"".join(steps)}</div>'
    )


def _cost_total_tile(totals: dict[str, Any]) -> str:
    """Lifetime estimated spend, or nothing when no sweep here was ever measured.

    A falsy total means exactly that — every run predates the tally, or ran on a
    backend with no meters — so the tile is omitted rather than claiming $0.00.
    """
    if not SHOW_COST or not totals.get("cost_usd"):
        return ""
    return (
        f'<div class="stat"><span class="stat-n">{usd(totals["cost_usd"])}</span>'
        f'<span class="stat-l">Est. cost</span></div>'
    )


def _cost_cell(run: dict[str, Any]) -> str:
    """One sweep's estimated cost, blank when that sweep was never measured.

    An em dash rather than $0.00 on runs made before the tally existed, or by a
    backend that cannot read its own meters — the same rule the stat tiles follow.
    """
    if not SHOW_COST:
        return ""
    return f'<td class="numeric">{usd((run.get("usage") or {}).get("cost_usd"))}</td>'


def _run_rows(runs: list[dict[str, Any]]) -> str:
    cells = []
    for r in runs:
        if r["in_progress"]:
            state = '<span class="pill live">running</span>'
        elif r["shortlisted"]:
            state = f'<span class="pill hit">{num(r["shortlisted"])} shortlisted</span>'
        else:
            state = '<span class="pill">no matches</span>'
        cells.append(
            f'<tr class="run-row">'
            f"<td>{e(local_time(r.get('started_at')))}</td>"
            f'<td class="numeric">{num(r["companies"])}</td>'
            f'<td class="numeric">{num(r["jobs"])}</td>'
            f'<td class="numeric">{num(r["candidates"])}</td>'
            f'<td class="numeric">{num(r["scored"])}</td>'
            f'<td class="numeric">{num(r.get("top_score"))}</td>'
            f'<td class="numeric">{num(r["shortlisted"])}</td>'
            f"{_cost_cell(r)}"
            f"<td>{state}</td></tr>"
        )
    return "".join(cells)


def build(snapshot: dict[str, Any], results_root: Path) -> str:
    totals, applied, runs = snapshot["totals"], snapshot["applied"], snapshot["runs"]
    live = snapshot["live"]

    newest = runs[0] if runs else None
    threshold = newest.get("threshold") if newest else None

    applied_rows = "".join(
        f'<tr><td class="wide">{e(a.get("title"))}</td><td>{e(a.get("board_token"))}</td>'
        f"<td>{e(local_time(a.get('applied_at')))}</td><td>{e(a.get('status'))}</td></tr>"
        for a in applied["recent"]
    )
    applied_block = (
        f'<div class="scroll-x"><table><thead><tr><th>Title</th><th>Company</th>'
        f"<th>When</th><th>Status</th></tr></thead><tbody>{applied_rows}</tbody></table></div>"
        if applied_rows
        else '<p class="note calm"><b>No applications recorded yet.</b> '
             "<code>/hireshire:apply</code> fills forms for the shortlist and records each "
             "one here. While <code>dry_run</code> is on it stops short of submitting.</p>"
    )

    body = f"""<div class="wrap">
  <p class="eyebrow"><span>HireShire</span>{'<span class="chip live">sweep running</span>' if live else ''}</p>
  <h1>Control room</h1>
  <p class="standfirst">
    Everything this install has swept, scored and applied to. Results live in
    <b>{e(results_root)}</b>, one folder per run.
  </p>

  <div class="stats">
    <div class="stat"><span class="stat-n">{num(totals['runs'])}</span><span class="stat-l">Sweeps</span></div>
    <div class="stat"><span class="stat-n">{num(totals['jobs'])}</span><span class="stat-l">Jobs scraped</span></div>
    <div class="stat"><span class="stat-n">{num(totals['candidates'])}</span><span class="stat-l">Reranked</span></div>
    <div class="stat"><span class="stat-n">{num(totals['scored'])}</span><span class="stat-l">LLM-scored</span></div>
    <div class="stat good"><span class="stat-n">{num(totals['shortlisted'])}</span><span class="stat-l">Shortlisted</span></div>
    <div class="stat"><span class="stat-n">{num(applied['total'])}</span><span class="stat-l">Applied</span></div>
    {_cost_total_tile(totals)}
  </div>
  <p class="standfirst" style="font-size:.9rem">
    Totals count every row across all {num(totals['runs'])} sweeps, so a job that resurfaced
    in several sweeps is counted once per sweep. Applications are a lifetime total for this
    install — the record of an application belongs to the job, not to the sweep that found it.
  </p>

  <h2 class="section">Right now</h2>
  {_live_panel(live)}

  <h2 class="section">Every sweep, newest first</h2>
  <div class="scroll-x">
    <table>
      <thead><tr>
        <th>Started</th><th>Employers</th><th>Jobs</th><th>Reranked</th>
        <th>Scored</th><th>Best</th><th>Shortlisted</th>{'<th>Est. cost</th>' if SHOW_COST else ''}<th></th>
      </tr></thead>
      <tbody>{_run_rows(runs) or f'<tr><td colspan="{9 if SHOW_COST else 8}">No sweeps yet.</td></tr>'}</tbody>
    </table>
  </div>
  <p class="standfirst" style="font-size:.9rem;margin-top:1rem">
    <b>Best</b> is the highest LLM score that sweep produced. A job is shortlisted when it
    reaches the threshold{f' of <b>{num(threshold)}</b>' if threshold is not None else ''}.
    Jobs that lost the top-K race are never marked seen, so they come back around in the
    next sweep and may win against weaker competition.
    {"<b>Est. cost</b> is Claude Code's own client-side estimate at list price for the "
     "sweeps that recorded one, so it is neither a bill nor a share of your plan's "
     "5-hour or weekly allowance. Sweeps run before costs were recorded show a dash."
     if SHOW_COST else ""}
  </p>

  <h2 class="section">Applications</h2>
  <div class="stats">
    <div class="stat good"><span class="stat-n">{num(applied['submitted'])}</span><span class="stat-l">Submitted</span></div>
    <div class="stat"><span class="stat-n">{num(applied['dry_run'])}</span><span class="stat-l">Dry run</span></div>
    <div class="stat flag"><span class="stat-n">{num(applied['errors'])}</span><span class="stat-l">Errors</span></div>
  </div>
  {applied_block}

  <footer>
    Written by the engine on every sweep — this file is local and is never published.
    Per-run reasoning lives in each run folder as <code>&lt;stamp&gt;_matching.html</code>,
    and the newest is mirrored to <code>latest_matching.html</code> beside this page.
  </footer>
</div>"""

    return document(TITLE, body, refresh_s=REFRESH_S if live else None)


def write(snapshot: dict[str, Any], path: Path) -> Path | None:
    """Write the dashboard. Never raises — see `matching.write` for why."""
    try:
        html = build(snapshot, path.parent)
    except Exception:  # noqa: BLE001
        logger.exception("Could not build the dashboard")
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        return None
    return path
