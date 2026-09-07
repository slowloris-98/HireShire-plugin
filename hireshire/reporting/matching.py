"""The matching report: why each job scored what it did, and what never got scored.

This is `<stamp>_results_all_jobs.csv` in a form a person can actually read. The
CSV answers "what happened to every posting" in sixteen columns; this answers the
question underneath it — *why* — by surfacing the four rationales the scoring
prompt returns, which until now were written into the `matches` table's
`raw_json` and never shown anywhere.

The motivating case: a sweep that shortlists nothing leaves the user with an empty
CSV. The all-jobs export tells them 6,488 postings were considered and ten were
scored, but not that the top one lost because the judge discounted project work
against professional work. That sentence is in the database. It just had nowhere
to go.

Two rules inherited from `hireshire/results_export.py`, both load-bearing:

* **A never-scored job renders a blank score, never 0.** `filtered_result` builds
  budget drops with `relevance_score=0`, and printing that reads as "the model
  judged this worthless" — the exact misreading that hid a broken reranker for a
  whole run.
* **The two rerank columns are never merged.** They are logits from two different
  models, so they get two columns and are never averaged, blended, or sorted
  against each other.

Output is *body content only* — see `render.artifact_page`. This file is published
by the find-jobs and start-orchestration skills, and the Artifact tool supplies
the document skeleton itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hireshire.reporting import data
from hireshire.reporting.render import (
    artifact_page,
    e,
    funnel_step,
    local_time,
    num,
    pct,
)

logger = logging.getLogger(__name__)

# Stable across every run, deliberately. The skills find this artifact again by
# title (`Artifact action:"list"`) so that each sweep republishes to the same URL
# instead of leaving a trail of stale pages in the user's gallery. Putting the run
# date in here would break that and is the one edit to avoid.
TITLE = "HireShire Match Report"

MATCHING_SUFFIX = "_matching.html"
LATEST_NAME = "latest_matching.html"

EXTRA_CSS = """
.job {
  display: grid; grid-template-columns: 5.5rem 1fr; gap: 1.75rem;
  background: var(--panel); border: 1px solid var(--rule); border-radius: 3px;
  padding: 1.6rem 1.75rem; box-shadow: var(--shadow); margin-bottom: 1.25rem;
}
.rail {
  display: flex; flex-direction: column; align-items: flex-start; gap: .15rem;
  border-right: 1px solid var(--rule); padding-right: 1rem;
}
.rank { font-size: .72rem; letter-spacing: .1em; color: var(--ink-faint); }
.total { font-size: 2.5rem; font-weight: 600; line-height: 1.05; letter-spacing: -.03em; }
.cutoff-note { font-size: .68rem; text-transform: uppercase; letter-spacing: .08em; }
.cutoff-note.short { color: var(--warn); }
.cutoff-note.over { color: var(--accent); }

.job-head h3 { font-size: 1.32rem; font-weight: 600; line-height: 1.25; letter-spacing: -.01em; text-wrap: balance; }
.job-head h3 a { text-decoration: none; border-bottom: 1px solid transparent; }
.job-head h3 a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.meta { color: var(--ink-soft); font-size: .95rem; margin-top: .3rem; }
.co { font-weight: 600; color: var(--ink); }
.sep { color: var(--ink-faint); padding: 0 .45rem; }
.funnel-scores {
  display: flex; flex-wrap: wrap; gap: .9rem; font-size: .7rem;
  text-transform: uppercase; letter-spacing: .07em; color: var(--ink-faint); margin-top: .7rem;
}
.funnel-scores b { color: var(--ink-soft); font-weight: 600; }
.verdict.no { color: var(--warn); }
.verdict.yes { color: var(--accent); }

.rubric { margin-top: 1.6rem; }
.rubric-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
.rubric h4 {
  font-family: "IBM Plex Mono", monospace; font-size: .7rem; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; color: var(--ink-soft);
}
.pts { font-size: .95rem; color: var(--ink-faint); }
.pts b { font-size: 1.05rem; font-weight: 600; color: var(--ink); }
.denom { font-size: .78rem; }
.prose { max-width: 62ch; color: var(--ink-soft); font-size: 1rem; line-height: 1.68; }

.notes { margin-top: 1.4rem; }
.notes h4 {
  font-family: "IBM Plex Mono", monospace; font-size: .7rem; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; margin-bottom: .5rem;
}
.notes.good h4 { color: var(--accent); }
.notes.bad h4 { color: var(--warn); }
.notes ul { list-style: none; display: flex; flex-direction: column; gap: .35rem; max-width: 62ch; }
.notes li { position: relative; padding-left: 1.1rem; color: var(--ink-soft); font-size: .98rem; line-height: 1.55; }
.notes li::before { position: absolute; left: 0; top: -.02em; }
.notes.good li::before { content: "+"; color: var(--accent); }
.notes.bad li::before { content: "\\2212"; color: var(--warn); }

.toolbar { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin-bottom: 1rem; }
.toolbar input {
  flex: 1 1 16rem; padding: .55rem .7rem; font-size: .85rem;
  background: var(--panel); color: var(--ink);
  border: 1px solid var(--rule); border-radius: 3px;
}
.toolbar button {
  padding: .55rem .9rem; font-size: .72rem; letter-spacing: .08em; text-transform: uppercase;
  background: var(--panel); color: var(--ink-soft); cursor: pointer;
  border: 1px solid var(--rule); border-radius: 3px;
}
.toolbar button:hover { color: var(--accent); border-color: var(--accent); }
.filter-note { font-size: .78rem; color: var(--ink-faint); }
.blank { color: var(--ink-faint); }

/* The unscored table scrolls inside itself rather than adding six thousand rows
   to the page. `overflow: auto` covers both axes, so the wide columns still
   scroll sideways in here instead of pushing the body sideways. */
.scroll-y {
  max-height: 70vh; overflow: auto;
  border: 1px solid var(--rule); border-radius: 3px; background: var(--panel);
}
.scroll-y table { font-size: .84rem; }
.scroll-y thead th {
  position: sticky; top: 0; z-index: 1;
  background: var(--panel); border-bottom: 1px solid var(--rule);
}
.scroll-y td, .scroll-y th { padding: .45rem .7rem; }
.rank-cell { color: var(--ink-faint); text-align: right; width: 1%; }
.scroll-hint { font-size: .78rem; color: var(--ink-faint); margin-top: .6rem; }
"""


def matching_name(stamp: str) -> str:
    return f"{stamp}{MATCHING_SUFFIX}"


def _rail(job: dict, threshold: int | None) -> str:
    total = job.get("relevance_score")
    gap = ""
    if threshold is not None and total is not None:
        delta = threshold - total
        gap = (
            f'<span class="cutoff-note short">{delta} short</span>' if delta > 0
            else f'<span class="cutoff-note over">+{-delta} over</span>'
        )
    return gap


def _rubric_rows(job: dict) -> str:
    rows = []
    for score_key, rationale_key, label, maximum in data.RUBRIC:
        value = job.get(score_key)
        rationale = job.get(rationale_key)
        if value is None and not rationale:
            continue
        rows.append(
            f'<section class="rubric"><header class="rubric-head">'
            f"<h4>{e(label)}</h4>"
            f'<span class="pts"><b>{num(value)}</b><span class="denom">/{maximum}</span></span>'
            f"</header>"
            f'<div class="track"><div class="fill" style="width:{pct(value, maximum):.1f}%"></div></div>'
            f'<p class="prose">{e(rationale or "—")}</p></section>'
        )
    return "".join(rows)


def _listing(job: dict, key: str, title: str, css: str) -> str:
    items = [str(x) for x in (job.get(key) or []) if str(x).strip()]
    if not items:
        return ""
    lis = "".join(f"<li>{e(x)}</li>" for x in items)
    return f'<section class="notes {css}"><h4>{e(title)}</h4><ul>{lis}</ul></section>'


def _scored_entry(index: int, job: dict, threshold: int | None) -> str:
    total = job.get("relevance_score")
    shortlisted = bool(job.get("shortlisted"))
    years = job.get("years_experience_required")
    years_txt = f"{float(years):g} yrs asked" if years else "years not stated"

    cluster = job.get("cluster_size") or 1
    cluster_txt = (
        f'<span class="sep">·</span>{cluster} locations' if cluster > 1 else ""
    )

    url = job.get("absolute_url") or ""
    title_html = e(job.get("title"))
    if url:
        title_html = f'<a href="{e(url)}" target="_blank" rel="noopener">{title_html}</a>'

    return (
        f'<article class="job"><div class="rail">'
        f'<span class="rank">{index:02d}</span>'
        f'<span class="total">{num(total)}</span>'
        f"{_rail(job, threshold)}</div>"
        f'<div class="body"><header class="job-head"><h3>{title_html}</h3>'
        f'<p class="meta"><span class="co">{e(job.get("board_token"))}</span>'
        f'<span class="sep">·</span>{e(job.get("location") or "location not given")}'
        f'<span class="sep">·</span>{e(years_txt)}{cluster_txt}</p>'
        f'<p class="funnel-scores">'
        f'<span>bi <b>{_fmt(job.get("encoder_score"), 3)}</b></span>'
        # The wide column only appears on rows from before the rerank cascade was
        # collapsed to one model. Rendering an empty one on every new row would put a
        # dash where a number used to be and invite the question of what broke.
        + (
            f'<span>wide <b>{_fmt(job.get("rerank_score_wide"), 2)}</b></span>'
            if job.get("rerank_score_wide") is not None else ""
        )
        + f'<span>cross <b>{_fmt(job.get("rerank_score"), 2)}</b></span>'
        + (
            '<span class="verdict yes">shortlisted</span>' if shortlisted
            else '<span class="verdict no">not shortlisted</span>'
        )
        + "</p></header>"
        f"{_rubric_rows(job)}"
        f'{_listing(job, "match_reasons", "What matched", "good")}'
        f'{_listing(job, "disqualifiers", "What counted against it", "bad")}'
        "</div></article>"
    )


def _fmt(value, places: int) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _unscored_payload(rows: list[dict]) -> str:
    """The not-scored jobs, as compact JSON for client-side rendering.

    Embedded as data rather than emitted as 6,000-plus table rows because JSON is
    several times denser than the equivalent markup — which keeps the page inside
    the artifact size cap and keeps the DOM light enough to filter instantly. The
    `llm_score` field is deliberately absent from every row here: these jobs were
    never scored, and a key holding 0 would invite a renderer to print it.
    """
    payload = [
        {
            "t": r.get("title") or "",
            "c": r.get("board_token") or "",
            "l": r.get("location") or "",
            "r": data.reason_label(r.get("skip_reason")),
            "w": _fmt(r.get("rerank_score_wide"), 2),
            "f": _fmt(r.get("rerank_score"), 2),
            "b": _fmt(r.get("encoder_score"), 3),
            "u": r.get("absolute_url") or "",
        }
        for r in rows
    ]
    # `</script>` inside a JSON string would close the block early; escaping the
    # slash keeps it valid JSON and inert as markup.
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


_SCRIPT = """
<script>
(function () {
  var el = document.getElementById("hs-unscored-data");
  if (!el) return;
  var rows = JSON.parse(el.textContent);
  var body = document.getElementById("hs-rows");
  var note = document.getElementById("hs-note");
  var box = document.getElementById("hs-filter");
  var more = document.getElementById("hs-more");
  var scroller = document.getElementById("hs-scroll");
  var PAGE = 200, shown = 0, view = rows;

  // The server sends the rows already ranked — refined cross-encoder score first,
  // then the wide-pass ones, each descending within its own model's scale. Stamping
  // that position now keeps it stable when the list is filtered: row 4,102 stays
  // row 4,102 rather than becoming "the third result for nurse".
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
        "</td><td>" + esc(r.c) + "</td><td>" + esc(r.l) + "</td><td>" + esc(r.r) +
        "</td><td class='numeric blank'>—</td>" +
        "<td class='numeric'>" + esc(r.f) + "</td><td class='numeric'>" + esc(r.w) +
        "</td><td class='numeric'>" + esc(r.b) + "</td></tr>";
    }
    body.insertAdjacentHTML("beforeend", html);
    shown += slice.length;
    more.style.display = shown < view.length ? "" : "none";
    note.textContent = "Showing " + shown.toLocaleString() + " of " +
      view.length.toLocaleString() +
      (view.length === rows.length ? "" : " (filtered from " + rows.length.toLocaleString() + ")");
  }
  box.addEventListener("input", function () {
    var q = box.value.trim().toLowerCase();
    view = q
      ? rows.filter(function (r) {
          return (r.t + " " + r.c + " " + r.l + " " + r.r).toLowerCase().indexOf(q) !== -1;
        })
      : rows;
    render(true);
  });
  more.addEventListener("click", function () { render(false); });
  // Pull the next batch in as the reader nears the bottom of the box, so the list
  // behaves like one long list without ever putting 6,000 rows in the DOM.
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


def build(snapshot: dict[str, Any], records: list[dict], stamp: str) -> str:
    """Render the report. Pure — takes data, returns HTML, touches no disk."""
    scored, unscored = data.split_matches(records)
    threshold = snapshot.get("threshold")
    live = snapshot["in_progress"]

    if live:
        state = '<span class="chip live">sweep running</span>'
        headline = "This sweep is still running."
        stand = (
            f"<b>{num(snapshot['companies'])}</b> employers swept so far and "
            f"<b>{num(snapshot['jobs'])}</b> postings found. Each employer's jobs are "
            "scored as soon as they are scraped, so the reasoning below fills in "
            "while the sweep runs rather than appearing all at once at the end."
        )
    elif scored:
        top = snapshot.get("top_score")
        shortlisted = snapshot["shortlisted"]
        state = f'<span class="chip">{e(snapshot.get("model") or "scored")}</span>'
        if shortlisted:
            headline = f"{num(shortlisted)} of {num(len(scored))} scored jobs cleared the cutoff."
            stand = (
                f"The sweep considered <b>{num(snapshot['jobs'])}</b> postings and spent "
                f"its scoring budget on <b>{num(len(scored))}</b>. "
                f"<b>{num(shortlisted)}</b> reached or beat the threshold of "
                f"<b>{num(threshold)}</b>."
            )
        else:
            headline = f"{num(len(scored))} jobs reached the judge. None cleared {num(threshold)}."
            stand = (
                f"The sweep saw <b>{num(snapshot['jobs'])}</b> postings. The funnel spent its "
                f"whole scoring budget on the {num(len(scored))} below, and the highest of "
                f"them came back at <b>{num(top)}</b>. That is why the shortlist is empty — "
                "not a bug, a cutoff."
            )
    else:
        state = '<span class="chip">no scores</span>'
        headline = "This sweep scored nothing."
        stand = (
            f"<b>{num(snapshot['jobs'])}</b> postings were found and "
            f"<b>{num(snapshot['candidates'])}</b> reached the reranker, but no job was "
            "sent to the judge. If that is unexpected, the run log is the place to look."
        )

    jobs = snapshot["jobs"]
    steps = [
        funnel_step("Employers swept", snapshot["companies"], snapshot["companies"],
                    f"{num(snapshot['companies_with_jobs'])} had at least one opening"),
        funnel_step("Postings found", jobs, jobs),
    ]
    if snapshot["gated_out"] is not None:
        steps.append(funnel_step(
            "Dropped by the free gates", snapshot["gated_out"], jobs,
            "Location, age and title keyword filters — no model involved", drop=True,
        ))
    steps.append(funnel_step("Reached the reranker", snapshot["candidates"], jobs))
    # Two separate steps, deliberately. They look alike — both are "cleared the gates,
    # never scored" — but one is a verdict and the other is a deferral, and a user
    # deciding whether to change a setting needs to know which they are looking at.
    # A large count here with nothing scored is also the signal that `min_score` is
    # set wrong for this resume rather than that the market is quiet.
    if snapshot.get("below_cutoff"):
        steps.append(funnel_step(
            "Below the relevance cutoff", snapshot["below_cutoff"], jobs,
            "The cross-encoder read the full description and said no — lower "
            "min_score to let more through",
            drop=True,
        ))
    if snapshot.get("cap_reached"):
        steps.append(funnel_step(
            "Reached the run's call cap", snapshot["cap_reached"], jobs,
            f"Still eligible next sweep — raising top_k (now {num(snapshot['top_k'])}) recovers them",
            drop=True,
        ))
    if snapshot["duplicates"]:
        steps.append(funnel_step(
            "Duplicate requisitions", snapshot["duplicates"], jobs,
            "Judged by proxy — one representative scored, the verdict copied to its siblings",
        ))
    steps.append(funnel_step("Scored by the LLM", len(scored), jobs))
    steps.append(funnel_step("Shortlisted", snapshot["shortlisted"], jobs))

    scored_html = "".join(
        _scored_entry(i, j, threshold) for i, j in enumerate(scored, 1)
    ) or '<p class="note">Nothing has been scored in this run yet.</p>'

    unscored_html = ""
    if unscored:
        unscored_html = f"""
  <h2 class="section">Considered but never scored — {num(len(unscored))} jobs</h2>
  <p class="standfirst" style="margin-bottom:1.25rem">
    These reached the funnel but not the judge, so they have cross-encoder scores and no
    LLM score. The score column is <b>blank</b> rather than zero on purpose: nothing
    read these descriptions. They are <b>already ranked</b>, best first — the ones that
    came closest to winning a budget slot are at the top.
  </p>
  <div class="toolbar">
    <input id="hs-filter" type="search" placeholder="Filter by title, company, location or reason…"
           aria-label="Filter jobs">
    <button id="hs-more" type="button">Show more</button>
    <span class="filter-note" id="hs-note"></span>
  </div>
  <div class="scroll-y" id="hs-scroll" tabindex="0">
    <table>
      <thead><tr>
        <th>#</th><th>Title</th><th>Company</th><th>Location</th><th>Why not scored</th>
        <th>LLM</th><th>Refine</th><th>Wide</th><th>Bi</th>
      </tr></thead>
      <tbody id="hs-rows"></tbody>
    </table>
  </div>
  <p class="scroll-hint">
    Ranked by the cross-encoder score that decided whether each job was worth scoring.
    Runs made before the funnel used a single model carry a second score from an earlier
    pass; the two came from <b>different models</b>, so they are ordered one after the
    other rather than merged. The <b>#</b> column keeps each job's place in that ranking
    even while you filter.
  </p>
  <script type="application/json" id="hs-unscored-data">{_unscored_payload(unscored)}</script>"""

    body = f"""<div class="wrap">
  <p class="eyebrow"><span>Run {e(stamp)}</span>{state}</p>
  <h1>{e(headline)}</h1>
  <p class="standfirst">{stand}</p>

  <div class="stats">
    <div class="stat"><span class="stat-n">{num(snapshot['jobs'])}</span><span class="stat-l">Jobs seen</span></div>
    <div class="stat"><span class="stat-n">{num(len(scored))}</span><span class="stat-l">LLM-scored</span></div>
    <div class="stat"><span class="stat-n">{num(snapshot.get('top_score'))}</span><span class="stat-l">Top score</span></div>
    <div class="stat flag"><span class="stat-n">{num(threshold)}</span><span class="stat-l">Threshold</span></div>
    <div class="stat {'good' if snapshot['shortlisted'] else 'flag'}"><span class="stat-n">{num(snapshot['shortlisted'])}</span><span class="stat-l">Shortlisted</span></div>
  </div>

  <p class="note">
    <b>How the 100 points split:</b> core technical skills 40, relevant experience 40,
    education &amp; nice-to-haves 20. When a job marks a skill mandatory and the resume
    shows no evidence of it, that category is capped at half its maximum — additively,
    once per missing item. Each rationale below names the caps it applied.
  </p>

  <h2 class="section">Where the {num(snapshot['jobs'])} postings went</h2>
  <div class="funnel">{''.join(steps)}</div>

  <h2 class="section">Scored, highest first</h2>
  {scored_html}
{unscored_html}

  <footer>
    Generated {e(local_time(snapshot.get('finished_at') or snapshot.get('started_at')))} local.
    The two cross-encoder columns come from <b>different models</b> and are never
    comparable to each other; the bi-encoder column is a 0–1 cosine over the title only.
    Full machine-readable rows are in <code>{e(stamp)}_results_all_jobs.csv</code>.
  </footer>
</div>"""

    return artifact_page(TITLE, body, EXTRA_CSS, _SCRIPT if unscored else "")


def write(snapshot: dict[str, Any], records: list[dict], stamp: str,
          run_dir: Path, latest_path: Path) -> Path | None:
    """Write the per-run report and refresh the stable `latest_matching.html`.

    Two files by design. The stamped one accumulates, so a user keeps every run's
    reasoning on disk; the `latest` copy gives the skills one fixed path to publish
    from, which is what lets every sweep redeploy to the same artifact URL.

    Never raises. Losing a report must not take down a run whose CSV, JSON and
    database rows are already safe — the same trade `write_all_jobs_csv` makes.
    """
    try:
        html = build(snapshot, records, stamp)
    except Exception:  # noqa: BLE001
        logger.exception("Could not build the matching report for %s", stamp)
        return None

    written: Path | None = None
    for path in (run_dir / matching_name(stamp), latest_path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            written = written or path
        except OSError as exc:
            logger.warning("Could not write %s: %s", path, exc)
    return written
