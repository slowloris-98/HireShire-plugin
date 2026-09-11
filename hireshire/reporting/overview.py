"""The overview page: four numbers and the four collapsible lists that match them.

The other two reports explain themselves at length, and that is the right call for
a diagnostic someone opens when a sweep did something surprising. This one is for
the other 95% of the time, when the question is just *what have I got*. So it
explains nothing: past one line naming the scope and telling the reader the sections
open, the only sentences on the page are the judge's own rationales, and those appear
only inside a job the reader opened. That line earns its place because everything is
collapsed by default — without it the accordions read as headings rather than as
things to click.

Tiles and sections run in the same order and use the same words, but they count
differently and are *meant* to: the tiles are a cumulative funnel (every shortlisted
job is also a relevant one), the sections are a **partition** — a job renders in
exactly one of them. The page is the user's only list of what is left to do, so a job
appearing twice would double it. `data.partition_jobs` owns that split.

Both scopes render from the same code and differ only in the data they are handed,
with one exception: how long it took and what it cost belong to a sweep, so those
two tiles appear on the per-run page alone.

Written at two scopes from one renderer:

* ``overview.html`` at the results root — every sweep this install has done.
* ``<stamp>_overview.html`` inside a run folder, beside that run's CSVs — one sweep,
  plus how long it took and what it cost.

Both are complete local documents opened over ``file://`` and never published, which
is what licenses their meta refresh: a published page could not reload itself.

Every section reads the same way — a filter box, six aligned columns under a sticky
header, and a bounded scroll box — because three of them used to be unbounded flat
lists beside one that was not, and a sweep with 300 filtered jobs made the page a
wall. The rows stay native ``<details>``/``<summary>``: keyboard support for free,
correct in the quirks mode that opening a local file puts some browsers in, and — the
load-bearing part — they fire the ``toggle`` event that ``_STATE_SCRIPT`` needs to
put the reader's open rows back after a refresh. A click-handled table row fires
none, which is why this is a grid and not a table.
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

/* One job inside an accordion: six columns closed, the judge's reasoning open.
   The grid template is declared once for the header and the rows together — they
   are two *separate* grid containers, so the columns line up only while the
   template, the gap and the horizontal padding match in both. Edit one, edit both. */
.job-head, .job > summary {
  display: grid;
  grid-template-columns: 3rem minmax(12rem, 1fr) 8rem minmax(6rem, 11rem) 4rem 4.5rem;
  gap: 1rem; align-items: baseline;
}
/* BASE_CSS's sticky rule is `.scroll-y thead th`, which is table-only, so a grid
   header needs its own. `.job-head` is a direct child of the `overflow: auto` box,
   which is what makes that box the scrollport it sticks to — an intermediate
   wrapper with its own `overflow` would break it. The opaque background and the
   z-index are not decoration: without them the rows scroll through the header. */
.job-head {
  position: sticky; top: 0; z-index: 1;
  background: var(--panel); border-bottom: 1px solid var(--rule);
  padding: .55rem .25rem;
  font-family: "IBM Plex Mono", monospace; font-size: .64rem; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint);
}
.job-head span:nth-child(1),
.job-head span:nth-child(5),
.job-head span:nth-child(6) { text-align: right; }

.job { border-top: 1px solid var(--rule); }
/* The header already draws that rule; two of them read as a gap. */
.job-head + .job { border-top: none; }
/* An author `display` rule beats the UA's `[hidden]` rule, so the filter says out
   loud that a hidden row is gone rather than relying on nobody adding one. */
.job[hidden] { display: none; }
.job > summary { padding: .7rem .25rem; cursor: pointer; list-style: none; }
.job > summary::-webkit-details-marker { display: none; }
.job > summary:hover .job-t { color: var(--accent); }

.job-i {
  font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
  font-size: .75rem; color: var(--ink-faint); text-align: right;
}
.job-s {
  font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
  font-size: 1.15rem; font-weight: 600; text-align: right;
}
.job-s.hit { color: var(--accent); }
.job-t { font-size: 1.02rem; line-height: 1.35; min-width: 0; }
/* The type the old single `.job-m` blob carried, now shared by the three cells and
   the sub-line that took over what it used to concatenate. */
.job-c, .job-l, .job-x, .job-sub {
  font-family: "IBM Plex Mono", monospace; font-size: .68rem; letter-spacing: .07em;
  text-transform: uppercase; color: var(--ink-faint); white-space: nowrap;
}
/* A grid item defaults to `min-width: auto`, which refuses to shrink below its
   content — without this the ellipsis never fires and "Hyderabad, Telangana, India
   ; Bengaluru, Karnataka, India ; …" comes back as a horizontal scrollbar, which is
   the problem the `.scroll-y td:nth-child(4)` rule below was written for. */
.job-c, .job-l { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.job-x { font-variant-numeric: tabular-nums; text-align: right; }
/* A whole sentence, so it wraps where the cells above it do not. */
.job-sub { display: block; margin-top: .25rem; white-space: normal; }

/* A block child of `.job`, not a grid item — so it spans the full width for free,
   inherits body typography rather than a table cell's nowrap, and simply makes the
   scroll box's content taller when it opens. The left padding lines it up under the
   title column: 3rem of rank plus the 1rem gap. */
.job-body { padding: .25rem .25rem 1.5rem 4rem; }
.job-body a.src {
  font-family: "IBM Plex Mono", monospace; font-size: .7rem; letter-spacing: .08em;
  text-transform: uppercase; color: var(--accent); text-decoration: none;
}
.job-body a.src:hover { text-decoration: underline; }
@media (max-width: 34rem) {
  /* The header is a desktop affordance: at this width the row wraps to three lines
     and six column labels would line up with nothing. */
  .job-head { display: none; }
  .job > summary { grid-template-columns: 2.5rem 1fr 3.5rem; }
  .job-c, .job-l { grid-column: 2; white-space: normal; overflow: visible; }
  /* The one number that only means anything beside other rows. A phone-width
     reader is not comparing cross-encoder logits. */
  .job-x { display: none; }
  .job-body { padding-left: .25rem; }
}

.empty { color: var(--ink-faint); font-family: "IBM Plex Mono", monospace; font-size: .8rem; padding: .5rem .25rem 1rem; }

/* The one instruction on the page. Everything collapses by default, so without it
   the accordions read as headings rather than as things to click. */
.hint { color: var(--ink-faint); font-size: .92rem; margin: 0 0 2.25rem; }

/* Locations run to "Hyderabad, Telangana, India ; Bengaluru, Karnataka, India ; …"
   and `th, td` are nowrap, so an uncapped column pushed the cross-encoder score —
   the column the table is *sorted by* — off the right edge of the box. */
.scroll-y td:nth-child(4) { max-width: 20rem; overflow: hidden; text-overflow: ellipsis; }
"""


def run_overview_name(stamp: str) -> str:
    return f"{stamp}{RUN_SUFFIX}"


# The one column set, and the whole point of the layout: the same six labels over
# every section, so a reader scanning down the page never has the columns move under
# them. Three sections render it as a sticky grid row over a list of `<details>`; the
# last renders the same labels as a real `<thead>` over a script-built table.
#
# No classes on the header cells. Alignment is a property of the *column*, so
# `nth-child` does it and a renamed data class cannot silently unalign the header.
_JOB_HEAD = (
    '<div class="job-head">'
    "<span>#</span><span>Title</span><span>Company</span>"
    "<span>Location</span><span>LLM</span><span>Cross</span>"
    "</div>"
)


def _cross(job: dict) -> str:
    """The cross-encoder logit to two places, or an em dash where none was taken.

    The same formatting as `_tail_payload`'s `x` key, so the Cross column reads
    identically in all four sections.
    """
    value = job.get("rerank_score")
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _job_entry(job: dict, rank: int, applied: bool = False) -> str:
    """One job as a `<details>`: a six-column row closed, the rationales open.

    Still a native `<details>` with a stable id, and that is not a style choice.
    `<summary>` is focusable and Enter/Space toggles it with no script at all. And
    `_STATE_SCRIPT` puts the open set back after the meta refresh by listening for
    `toggle` on elements whose `tagName` is `DETAILS` — a row built as a
    click-handled table row fires no such event, so every refresh would shut the job
    whose reasoning the reader was halfway through.

    `rank` is stamped here rather than counted in the browser so it survives
    filtering, the same rule the last section's script follows: row 214 stays row
    214 rather than becoming "the third result for nurse".

    The score is an em dash whenever no verdict actually stands behind it. A cluster
    sibling counts as judged — one call, copied to every member — but only when the
    representative *returned* something: one whose representative hit an API error
    inherits that failure and a placeholder `relevance_score` of 0. Six such rows
    printed a bold "0" beside a real job on the first render against live data, which
    reads as "the model judged this worthless" and is the exact misreading the
    results CSV leaves `llm_score` blank to avoid.
    """
    reason = job.get("skip_reason") or ""
    judged = reason in ("", "duplicate_of_cluster")
    score = job.get("relevance_score") if judged else None
    shortlisted = bool(job.get("shortlisted"))
    url = job.get("absolute_url") or ""
    title = job.get("title") or ""
    company = job.get("board_token") or ""
    where = job.get("location") or ""

    # The two facts that cannot be a column and would be a lie as one: a reason label
    # is a whole sentence, and the applied stamp is a status plus a timestamp. Both
    # sit under the title, which keeps the six columns scannable and — the part that
    # matters — keeps a *why* on the section whose only question is why.
    sub = "" if judged else data.reason_label(reason)
    if applied:
        status = job.get("applied_status") or "applied"
        sub = " · ".join(
            x for x in (sub, f"{status} {local_time(job.get('applied_at'))}") if x
        )
    sub_html = f'<span class="job-sub">{e(sub)}</span>' if sub else ""

    # Lowercased server-side so filtering is one `indexOf` per row per keystroke,
    # with no allocation. Space-separated to match the last section's
    # `(r.t + " " + r.c + " " + r.l)`, so one query behaves the same in every box.
    hay = " ".join(x for x in (title, company, where, sub) if x).lower()

    body = (
        rubric_rows(job, data.RUBRIC)
        + listing(job, "match_reasons", "What matched", "good")
        + listing(job, "disqualifiers", "What counted against it", "bad")
    )
    if url:
        body += f'<p style="margin-top:1.4rem"><a class="src" href="{e(url)}" target="_blank" rel="noopener">Open posting →</a></p>'

    return (
        f'<details class="job" id="j:{e(job.get("job_id"))}" data-hay="{e(hay)}">'
        "<summary>"
        f'<span class="job-i">{rank}</span>'
        f'<span class="job-t">{e(title)}{sub_html}</span>'
        f'<span class="job-c">{e(company)}</span>'
        f'<span class="job-l">{e(where)}</span>'
        f'<span class="job-s{" hit" if shortlisted else ""}">{num(score)}</span>'
        f'<span class="job-x">{_cross(job)}</span>'
        "</summary>"
        f'<div class="job-body">{body}</div></details>'
    )


def _accordion(key: str, label: str, jobs: list[dict], total: int,
               applied: bool = False) -> str:
    """One section: a filter box, a sticky column header, a bounded scroll box.

    No pagination, unlike the last section, and deliberately: every row is already
    in the document, which is what `_STATE_SCRIPT` needs — a restored row at
    position 250 would not yet exist in a paginated list and would silently fail to
    reopen. The box bounds the *height*, not the DOM, and the DOM is no heavier than
    it was as a flat list.

    An empty section keeps the bare "Nothing yet." it has always had. A filter over
    no rows and a header over no data are both noise, and this is the page a
    first-time user sees before their first sweep finishes.
    """
    head = (
        f'<details class="acc" id="acc:{e(key)}"><summary>'
        f'<span>{e(label)}<span class="n">{num(total)}</span></span>'
        '</summary><div class="acc-body">'
    )
    if not jobs:
        return head + '<p class="empty">Nothing yet.</p></div></details>'

    rows = "".join(
        _job_entry(job, i, applied) for i, job in enumerate(jobs, start=1)
    )
    capped = (
        f'<p class="empty">Showing the first {num(len(jobs))} of {num(total)}.</p>'
        if total > len(jobs) else ""
    )
    return (
        head
        + '<div class="filterable">'
        '<div class="toolbar">'
        '<input class="f-box" type="search" placeholder="Filter" '
        f'aria-label="Filter {e(label.split(" (")[0].lower())}" '
        f'aria-controls="rows:{e(key)}">'
        '<span class="filter-note f-note"></span>'
        "</div>"
        f'<div class="scroll-y" id="rows:{e(key)}" tabindex="0" data-keep-scroll="1">'
        f"{_JOB_HEAD}{rows}"
        "</div></div>"
        f"{capped}</div></details>"
    )


def _tail_payload(rows: list[dict]) -> str:
    """The last section's jobs as compact JSON, rendered client-side.

    Data rather than markup, and this is the one section that earns it: JSON is
    several times denser than the equivalent table rows, which keeps a page holding
    thousands of them light enough to filter instantly. Since it began reading `jobs`
    rather than `matches` it holds the title-gate rejections too, which is thousands
    of rows on a real sweep where the other three are dozens. The three above it stay
    server-rendered markup — their bodies are the judge's prose, and `_STATE_SCRIPT`
    can only reopen a row that is already in the document.

    No LLM score key of any kind: everything here was dropped by a gate that costs
    nothing to run, so nothing read these descriptions, and a key holding 0 invites a
    renderer to print it as a verdict. The cross-encoder logit is the one score some
    of them have, and the rows that never reached it render an em dash.

    The renderer still prints an em dash in the LLM column, so all four sections
    carry the same six. That is not the same thing as printing 0: a dash says
    nothing read this, which is the fact — the same statement `num(None)` makes
    everywhere else on the page. What must not come back is the *key*.
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


# The last section only. It does a job the other three do not — build rows from a
# JSON array and page them in on scroll — which is why it keeps its own ids and its
# own script. `_FILTER_SCRIPT` below serves the three server-rendered lists, and
# merging the two would mean one function that both re-renders an array and toggles
# existing DOM.
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
      // The LLM cell is a literal dash, not a value: nothing here was ever read by
      // a judge. The column exists so this section carries the same six as the
      // three above it; the payload deliberately has no key behind it.
      html += "<tr><td class='rank-cell'>" + r.n + "</td><td class='wide'>" + title +
        "</td><td>" + esc(r.c) + "</td><td>" + esc(r.l) +
        "</td><td class='numeric blank'>—</td><td class='numeric'>" + esc(r.x) +
        "</td></tr>";
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


# One script for the three server-rendered lists, not three copies. Their rows are
# already in the DOM, so the whole filter is `hidden` over a haystack the server
# stamped, and the only thing it needs per section is a container to scope its
# lookups to. `.filterable` is that container — so a fourth filterable list is
# markup and no script at all.
_FILTER_SCRIPT = """
<script>
(function () {
  var boxes = document.querySelectorAll(".filterable");
  for (var i = 0; i < boxes.length; i++) wire(boxes[i]);

  function wire(root) {
    var input = root.querySelector(".f-box");
    var note = root.querySelector(".f-note");
    var scroller = root.querySelector(".scroll-y");
    if (!input || !scroller) return;
    var rows = scroller.querySelectorAll("details.job");
    var total = rows.length;

    function paint(n) {
      if (!note) return;
      note.textContent = n === total
        ? total.toLocaleString() + " shown"
        : n.toLocaleString() + " of " + total.toLocaleString();
    }
    input.addEventListener("input", function () {
      // `data-hay` is lowercased server-side, so this is one `indexOf` per row per
      // keystroke with no allocation — 300 rows stay instant on a phone.
      var q = input.value.trim().toLowerCase(), n = 0;
      for (var j = 0; j < total; j++) {
        var row = rows[j];
        var hit = !q || (row.getAttribute("data-hay") || "").indexOf(q) !== -1;
        // `hidden` rather than a class: it takes the row out of layout *and* out of
        // the accessibility tree, and a row the reader had open stays open while
        // hidden — clearing the filter brings it back expanded rather than reset.
        row.hidden = !hit;
        if (hit) n++;
      }
      scroller.scrollTop = 0;
      paint(n);
    });
    paint(total);
  }
})();
</script>
"""


# Every `<details>` on the page carries a stable id, and this puts the open ones back
# after a reload. Not a nicety: while a sweep is running the page meta-refreshes every
# REFRESH_S seconds, and without this it slams shut every accordion the reader had
# opened — including the job whose rationale they were halfway through. Under the old
# always-expanded layout a refresh cost only scroll position; collapsing by default is
# what makes losing the open set expensive.
#
# `sessionStorage` rather than `localStorage`: reopening the file tomorrow should give
# the resting state, not whatever was open during last night's sweep. Every access is
# guarded — a file:// origin can be opaque enough that touching storage throws, and a
# report must degrade rather than die.
_STATE_SCRIPT = """
<script>
(function () {
  var KEY = "hs-overview-open", SKEY = "hs-overview-scroll", store;
  try { store = window.sessionStorage; if (!store) return; } catch (e) { return; }

  function read(key, empty) {
    try { return JSON.parse(store.getItem(key) || empty); }
    catch (e) { return JSON.parse(empty); }
  }
  function write(key, value) {
    try { store.setItem(key, JSON.stringify(value)); } catch (e) {}
  }

  var open = read(KEY, "[]"), live = [];
  for (var i = 0; i < open.length; i++) {
    var el = document.getElementById(open[i]);
    if (el) { el.open = true; live.push(open[i]); }
  }
  // Prune ids that no longer resolve. A row leaves the page for good when it drops
  // out of MAX_JOB_ROWS or a later sweep moves it to another section, and its id
  // would otherwise sit in the set for the rest of the session. Safe against the
  // loop above: `toggle` fires asynchronously, so those events land after this write
  // and each finds its id already present, taking the `return` branch below.
  if (live.length !== open.length) write(KEY, live);

  // `toggle` does not bubble, so listen in the capture phase to catch every one.
  document.addEventListener("toggle", function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== "DETAILS" || !el.id) return;
    var set = read(KEY, "[]"), at = set.indexOf(el.id);
    if (el.open && at === -1) set.push(el.id);
    else if (!el.open && at !== -1) set.splice(at, 1);
    else return;
    write(KEY, set);
  }, true);

  // Scroll position inside the bounded lists. The browser restores the *document's*
  // scroll across a meta refresh but never an `overflow: auto` div's, and the job
  // lists only became scroll boxes recently — so without this a reader 200 rows into
  // Jobs Filtered is snapped back to row 1 every REFRESH_S seconds. Restored after
  // the reopen loop above, for two independent reasons: an open row's body is
  // hundreds of pixels, so the box's scrollHeight is the all-closed one until the
  // reopen lands and a restored scrollTop would clamp low; and a box inside a closed
  // accordion has no layout box at all, so assigning scrollTop to it is silently
  // dropped. Setting `.open` takes effect synchronously — only `toggle` is async.
  //
  // Opt-in by attribute: the last section deliberately does not carry it, because
  // only its first page of rows exists on load and its own script zeroes the box a
  // moment later anyway.
  var tops = read(SKEY, "{}"), pending = null;
  function save(ev) {
    var box = ev.currentTarget;
    if (!box.id) return;
    // `scroll` fires per frame; `setItem` is a synchronous, disk-backed write.
    if (pending) clearTimeout(pending);
    pending = setTimeout(function () {
      var all = read(SKEY, "{}");
      all[box.id] = box.scrollTop;
      write(SKEY, all);
    }, 200);
  }
  var lists = document.querySelectorAll("[data-keep-scroll]");
  for (var k = 0; k < lists.length; k++) {
    if (lists[k].id && tops[lists[k].id]) lists[k].scrollTop = tops[lists[k].id];
    lists[k].addEventListener("scroll", save);
  }
})();
</script>
"""


def _stat(value: str, label: str, css: str = "", hint: str = "") -> str:
    """One tile. `hint` becomes a `title=` tooltip, for a label too long to print.

    The labels have to stay short: `.stats` is a `minmax(9rem, 1fr)` grid, `.stat-l`
    is `.68rem` uppercase with `.1em` tracking, and grid rows equalise — so one
    three-line label makes every tile in the row three lines tall.
    """
    tip = f' title="{e(hint)}"' if hint else ""
    return (
        f'<div class="stat{css}"{tip}><span class="stat-n">{value}</span>'
        f'<span class="stat-l">{e(label)}</span></div>'
    )


def build(snapshot: dict[str, Any], stamp: str | None = None) -> str:
    """Render the page. Pure — takes data, returns HTML, touches no disk."""
    counts = snapshot["counts"]
    per_run = snapshot.get("run_id") is not None

    tiles = [
        _stat(num(counts["seen"]), "Jobs in scope",
              hint="Total jobs in the given location and time window"),
        _stat(num(counts["relevant"]), "Relevant jobs",
              hint="Cleared every free gate: keywords, title relevance, the "
                   "cross-encoder cutoff and the years-of-experience check"),
        _stat(num(counts["shortlisted"]), "Jobs shortlisted",
              " good" if counts["shortlisted"] else ""),
        _stat(num(counts["applied"]), "Jobs applied"),
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

    # The third accordion. Its rows are built by script rather than written out as
    # markup, but that is orthogonal to being collapsed: the script runs on load and
    # fills a `<tbody>` that happens to be inside a closed `<details>`, so the list is
    # ready the moment it opens. The scroll listener cannot misfire while it is shut
    # either — a hidden box is never scrolled.
    seen, seen_total = snapshot["seen"], snapshot["seen_total"]
    seen_html = ""
    if seen:
        seen_html = f"""
  <details class="acc" id="acc:seen"><summary><span>Total Jobs Seen<span class="n">{num(seen_total)}</span></span></summary>
  <div class="acc-body">
  <div class="toolbar">
    <input id="ov-filter" type="search" placeholder="Filter" aria-label="Filter jobs">
    <span class="filter-note" id="ov-note"></span>
  </div>
  <div class="scroll-y" id="ov-scroll" tabindex="0">
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Company</th><th>Location</th><th>LLM</th><th>Cross</th></tr></thead>
      <tbody id="ov-rows"></tbody>
    </table>
  </div>
  <script type="application/json" id="ov-tail-data">{_tail_payload(seen)}</script>
  </div></details>"""

    body = f"""<div class="wrap">
  <p class="eyebrow"><span>HireShire</span><span>{e(scope)}</span>{live_chip}</p>
  <h1>Control room</h1>

  <div class="stats">{''.join(tiles)}</div>
  <p class="hint">In scope means matching your location and posted inside your time window. Click a section to open it, filter it if it is long, then click any job for the full reasoning behind its score.</p>

  {_accordion("applied", "Jobs Applied", snapshot["applied"], snapshot["applied_total"], applied=True)}
  {_accordion("shortlisted", "Jobs Shortlisted (to be applied)", snapshot["shortlisted"], snapshot["shortlisted_total"])}
  {_accordion("filtered", "Jobs Filtered (yet to be scored or not picked)", snapshot["filtered"], snapshot["filtered_total"])}
{seen_html}
</div>"""

    # The filter script is wired by class, so it covers however many of the three
    # rendered a list — but there is no point shipping it when none of them did.
    listed = bool(snapshot["applied"] or snapshot["shortlisted"] or snapshot["filtered"])

    return document(
        f"{TITLE} — {stamp}" if per_run and stamp else TITLE,
        body + (_SCRIPT if seen else "") + (_FILTER_SCRIPT if listed else "")
        + _STATE_SCRIPT,
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
