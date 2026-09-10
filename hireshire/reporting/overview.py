"""The overview page: four numbers and three collapsible lists, and nothing else.

The other two reports explain themselves at length, and that is the right call for
a diagnostic someone opens when a sweep did something surprising. This one is for
the other 95% of the time, when the question is just *what have I got*. So it
carries no prose at all: the only sentences on the page are the judge's own
rationales, and they appear only inside a job the reader opened.

Written at two scopes from one renderer:

* ``overview.html`` at the results root — every sweep this install has done.
* ``<stamp>_overview.html`` inside a run folder, beside that run's CSVs — one sweep,
  plus how long it took and what it cost.

Both are complete local documents opened over ``file://`` and never published, so
unlike the matching report they may carry a meta refresh. The body itself is
envelope-agnostic: swapping ``document`` for ``artifact_page`` is the only change
needed to publish one.

The accordions are native ``<details>``/``<summary>``. No JavaScript, keyboard
support for free, and correct in the quirks mode that opening a local file puts the
browser in — the same constraint the shared CSS is written against.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hireshire.reporting import data
from hireshire.reporting.render import (
    SHOW_COST,
    document,
    duration,
    e,
    listing,
    local_time,
    num,
    rubric_rows,
    usd,
)

logger = logging.getLogger(__name__)

TITLE = "HireShire"
OVERVIEW_NAME = "overview.html"
RUN_SUFFIX = "_overview.html"

# Matches the dashboard's cadence, for the same reason: long enough not to thrash a
# browser, short enough to feel live against an engine that rewrites the file every
# ten seconds.
REFRESH_S = 15

OVERVIEW_CSS = """
.acc { border-top: 1px solid var(--rule); }
.acc:last-of-type { border-bottom: 1px solid var(--rule); }
.acc > summary {
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 1.15rem .25rem; cursor: pointer; font-size: 1.05rem; font-weight: 600;
  list-style: none;
}
.acc > summary::-webkit-details-marker { display: none; }
.acc > summary::after {
  content: "+"; font-family: "IBM Plex Mono", monospace; font-size: 1.1rem;
  font-weight: 400; color: var(--ink-faint); line-height: 1;
}
.acc[open] > summary::after { content: "\\00d7"; }
.acc > summary:hover { color: var(--accent); }
.acc > summary .n {
  font-family: "IBM Plex Mono", monospace; font-size: .8rem; font-weight: 500;
  color: var(--ink-faint); margin-left: .5rem;
}
.acc-body { padding: 0 .25rem 1.25rem; }

/* One job inside an accordion: a single line closed, the judge's reasoning open. */
.job {
  border-top: 1px solid var(--rule);
}
.job > summary {
  display: grid; grid-template-columns: 3rem 1fr auto; align-items: baseline;
  gap: 1rem; padding: .7rem .25rem; cursor: pointer; list-style: none;
}
.job > summary::-webkit-details-marker { display: none; }
.job > summary:hover .job-t { color: var(--accent); }
.job-s {
  font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
  font-size: 1.15rem; font-weight: 600;
}
.job-s.hit { color: var(--accent); }
.job-t { font-size: 1.02rem; line-height: 1.35; }
.job-m {
  font-family: "IBM Plex Mono", monospace; font-size: .68rem; letter-spacing: .07em;
  text-transform: uppercase; color: var(--ink-faint); white-space: nowrap;
}
.job-body { padding: .25rem .25rem 1.5rem 4rem; }
.job-body a.src {
  font-family: "IBM Plex Mono", monospace; font-size: .7rem; letter-spacing: .08em;
  text-transform: uppercase; color: var(--accent); text-decoration: none;
}
.job-body a.src:hover { text-decoration: underline; }
@media (max-width: 34rem) {
  .job > summary { grid-template-columns: 2.5rem 1fr; }
  .job-m { grid-column: 2; white-space: normal; }
  .job-body { padding-left: .25rem; }
}

.empty { color: var(--ink-faint); font-family: "IBM Plex Mono", monospace; font-size: .8rem; padding: .5rem .25rem 1rem; }
h2.section .n { color: var(--ink-faint); }

/* Locations run to "Hyderabad, Telangana, India ; Bengaluru, Karnataka, India ; …"
   and `th, td` are nowrap, so an uncapped column pushed the cross-encoder score —
   the column the table is *sorted by* — off the right edge of the box. */
.scroll-y td:nth-child(4) { max-width: 20rem; overflow: hidden; text-overflow: ellipsis; }
"""


def run_overview_name(stamp: str) -> str:
    return f"{stamp}{RUN_SUFFIX}"


def _job_entry(job: dict, applied: bool = False) -> str:
    """One job as a `<details>`: a line closed, the four rationales open.

    The score is an em dash whenever no verdict actually stands behind it. A cluster
    sibling counts as judged — one call, copied to every member — but only when the
    representative *returned* something: one whose representative hit an API error
    inherits that failure and a placeholder `relevance_score` of 0. Six such rows
    printed a bold "0" beside a real job on the first render against live data, which
    reads as "the model judged this worthless" and is the exact misreading the
    all-jobs CSV leaves `llm_score` blank to avoid.
    """
    reason = job.get("skip_reason") or ""
    judged = reason in ("", "duplicate_of_cluster")
    score = job.get("relevance_score") if judged else None
    shortlisted = bool(job.get("shortlisted"))
    url = job.get("absolute_url") or ""

    meta = " · ".join(
        x for x in (
            job.get("board_token"),
            job.get("location"),
            "" if judged else data.reason_label(reason),
        ) if x
    )
    if applied:
        stamped = local_time(job.get("applied_at"))
        status = job.get("applied_status") or "applied"
        meta = " · ".join(x for x in (meta, f"{status} {stamped}") if x)

    body = (
        rubric_rows(job, data.RUBRIC)
        + listing(job, "match_reasons", "What matched", "good")
        + listing(job, "disqualifiers", "What counted against it", "bad")
    )
    if url:
        body += f'<p style="margin-top:1.4rem"><a class="src" href="{e(url)}" target="_blank" rel="noopener">Open posting →</a></p>'

    return (
        '<details class="job"><summary>'
        f'<span class="job-s{" hit" if shortlisted else ""}">{num(score)}</span>'
        f'<span class="job-t">{e(job.get("title"))}</span>'
        f'<span class="job-m">{e(meta)}</span>'
        "</summary>"
        f'<div class="job-body">{body}</div></details>'
    )


def _accordion(label: str, jobs: list[dict], total: int, applied: bool = False) -> str:
    shown = "".join(_job_entry(j, applied) for j in jobs) or '<p class="empty">Nothing yet.</p>'
    capped = (
        f'<p class="empty">Showing the first {num(len(jobs))} of {num(total)}.</p>'
        if total > len(jobs) else ""
    )
    return (
        '<details class="acc"><summary>'
        f'<span>{e(label)}<span class="n">{num(total)}</span></span>'
        f'</summary><div class="acc-body">{shown}{capped}</div></details>'
    )


def _tail_payload(rows: list[dict]) -> str:
    """The never-scored jobs as compact JSON, rendered client-side.

    Data rather than markup for the same reason the matching report does it: JSON is
    several times denser than the equivalent table rows, which keeps a page holding
    thousands of them light enough to filter instantly. No score key of any kind —
    nothing read these descriptions, and a key holding 0 invites a renderer to print
    it as a verdict.
    """
    payload = [
        {
            "t": r.get("title") or "",
            "c": r.get("board_token") or "",
            "l": r.get("location") or "",
            "x": "—" if r.get("rerank_score") is None else f"{float(r['rerank_score']):.2f}",
            "u": r.get("absolute_url") or "",
        }
        for r in rows
    ]
    # `</script>` inside a JSON string would close the block early; escaping the
    # slash keeps it valid JSON and inert as markup.
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


# A trimmed cousin of `matching._SCRIPT` rather than a shared helper: this table has
# four columns to matching's nine, and matching's own tests parse its row template
# out of the script source, so a shared one would be pinned to that shape forever.
_SCRIPT = """
<script>
(function () {
  var el = document.getElementById("ov-tail-data");
  if (!el) return;
  var rows = JSON.parse(el.textContent);
  var body = document.getElementById("ov-rows");
  var note = document.getElementById("ov-note");
  var box = document.getElementById("ov-filter");
  var scroller = document.getElementById("ov-scroll");
  var PAGE = 200, shown = 0, view = rows;

  // Rank is stamped server-side so it survives filtering: row 4,102 stays row
  // 4,102 rather than becoming "the third result for nurse".
  for (var n = 0; n < rows.length; n++) rows[n].n = n + 1;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function render(reset) {
    if (reset) { body.innerHTML = ""; shown = 0; scroller.scrollTop = 0; }
    var slice = view.slice(shown, shown + PAGE), html = "";
    for (var i = 0; i < slice.length; i++) {
      var r = slice[i];
      var title = r.u
        ? '<a href="' + esc(r.u) + '" target="_blank" rel="noopener">' + esc(r.t) + "</a>"
        : esc(r.t);
      html += "<tr><td class='rank-cell'>" + r.n + "</td><td class='wide'>" + title +
        "</td><td>" + esc(r.c) + "</td><td>" + esc(r.l) +
        "</td><td class='numeric'>" + esc(r.x) + "</td></tr>";
    }
    body.insertAdjacentHTML("beforeend", html);
    shown += slice.length;
    note.textContent = shown.toLocaleString() + " of " + view.length.toLocaleString();
  }
  box.addEventListener("input", function () {
    var q = box.value.trim().toLowerCase();
    view = q
      ? rows.filter(function (r) {
          return (r.t + " " + r.c + " " + r.l).toLowerCase().indexOf(q) !== -1;
        })
      : rows;
    render(true);
  });
  scroller.addEventListener("scroll", function () {
    if (shown >= view.length) return;
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 200) {
      render(false);
    }
  });
  render(true);
})();
</script>
"""


def _stat(value: str, label: str, css: str = "") -> str:
    return (
        f'<div class="stat{css}"><span class="stat-n">{value}</span>'
        f'<span class="stat-l">{e(label)}</span></div>'
    )


def build(snapshot: dict[str, Any], stamp: str | None = None) -> str:
    """Render the page. Pure — takes data, returns HTML, touches no disk."""
    counts = snapshot["counts"]
    per_run = snapshot.get("run_id") is not None

    tiles = [
        _stat(num(counts["seen"]), "Seen"),
        _stat(num(counts["filtered"]), "Filtered"),
        _stat(num(counts["shortlisted"]), "Shortlisted",
              " good" if counts["shortlisted"] else ""),
        _stat(num(counts["applied"]), "Applied"),
    ]
    if per_run:
        # Elapsed while the sweep is live — `finished_at` is only written once the
        # pipeline's own run row lands — so the figure climbs with each refresh.
        tiles.append(_stat(
            duration(snapshot.get("started_at"), snapshot.get("finished_at")), "Took"
        ))
        if SHOW_COST:
            # `usage` is written once, when the matcher finalises. Mid-sweep this is
            # an em dash rather than $0.00: no measurement exists yet, and a printed
            # zero would claim the sweep was free.
            tiles.append(_stat(
                usd((snapshot.get("usage") or {}).get("cost_usd")), "Est. cost"
            ))

    scope = f"Run {stamp}" if per_run and stamp else "All sweeps"
    live_chip = '<span class="chip live">running</span>' if snapshot["live"] else ""

    tail, tail_total = snapshot["tail"], snapshot["tail_total"]
    tail_html = ""
    if tail:
        tail_html = f"""
  <h2 class="section">Not scored <span class="n">{num(tail_total)}</span></h2>
  <div class="toolbar">
    <input id="ov-filter" type="search" placeholder="Filter" aria-label="Filter jobs">
    <span class="filter-note" id="ov-note"></span>
  </div>
  <div class="scroll-y" id="ov-scroll" tabindex="0">
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Company</th><th>Location</th><th>Cross</th></tr></thead>
      <tbody id="ov-rows"></tbody>
    </table>
  </div>
  <script type="application/json" id="ov-tail-data">{_tail_payload(tail)}</script>"""

    body = f"""<div class="wrap">
  <p class="eyebrow"><span>{e(scope)}</span>{live_chip}</p>

  <div class="stats">{''.join(tiles)}</div>

  {_accordion("Applied", snapshot["applied"], snapshot["applied_total"], applied=True)}
  {_accordion("Scored, not applied", snapshot["scored"], snapshot["scored_total"])}
{tail_html}
</div>"""

    return document(
        f"{TITLE} — {stamp}" if per_run and stamp else TITLE,
        body + (_SCRIPT if tail else ""),
        refresh_s=REFRESH_S if snapshot["live"] else None,
        extra_css=OVERVIEW_CSS,
    )


def write(snapshot: dict[str, Any], path: Path, stamp: str | None = None) -> Path | None:
    """Write one overview file. Never raises — see `matching.write` for why."""
    try:
        html = build(snapshot, stamp)
    except Exception:  # noqa: BLE001
        logger.exception("Could not build the overview page for %s", stamp or "all sweeps")
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        return None
    return path
