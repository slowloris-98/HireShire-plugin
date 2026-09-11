"""The shared look, and the one HTML envelope the overview page needs.

``document()`` emits a complete document — doctype, ``<html>``, ``<head>`` — and
may carry a meta refresh, because every page written here is a local file opened
with ``file://`` from the user's own results folder and none of them is ever
published. There used to be a second envelope, ``artifact_page``, emitting body
content only for the Artifact tool to wrap; the report that used it is gone.

Opening a local file puts some browsers in quirks mode, and everything here is
written to survive that — an explicit ``box-sizing`` on every element, flex and
grid for layout, no percentage heights.

Colour lives in tokens defined three times over (bare ``:root``, then
``prefers-color-scheme`` guarded against an explicit light choice, then
``[data-theme="dark"]``). That is more than a local file strictly needs, and it is
kept deliberately: it costs nothing, and it is what makes the pages correct under a
viewer that stamps an explicit theme on the root element as well as one that stamps
nothing at all. A colour whose only definition sits inside a media query never
applies in that un-stamped state, which is how a page ends up rendering one theme's
text on the other theme's ground.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# The one switch for every cost figure the reports print. Set it to False and the
# `Est. cost` tile disappears, leaving the pages exactly as they were before scoring
# cost was recorded — the number stays in `runs.stats_json` either way. It is a
# constant rather than a setting because turning it off is an edit to this repo, not
# a decision a user makes; `config_writer.py` deliberately whitelists what users may
# change.
SHOW_COST = True

# Every face names a real fallback stack. These pages are opened from disk and are
# often read on a machine with no network, or with the fonts blocked, so the page has
# to stay readable with nothing fetched.
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Newsreader:ital,opsz,wght@0,6..72,400;0,500;0,600;1,6..72,400"
    '&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
)

BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  --ground: #F5F6F3;
  --panel: #FFFFFF;
  --ink: #14180F;
  --ink-soft: #4B5348;
  --ink-faint: #7C8579;
  --rule: #DCE0D6;
  --accent: #1F4D3D;
  --accent-soft: #E2EBE4;
  --warn: #9A6508;
  --warn-soft: #F2E7CF;
  --shadow: 0 1px 2px rgba(20,24,15,.05), 0 8px 24px -16px rgba(20,24,15,.25);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0F120E; --panel: #171B15; --ink: #E9EDE4; --ink-soft: #A9B2A3;
    --ink-faint: #7C866F; --rule: #2A3126; --accent: #7FC3A2; --accent-soft: #21301F;
    --warn: #D8A94E; --warn-soft: #302713;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"] {
  --ground: #0F120E; --panel: #171B15; --ink: #E9EDE4; --ink-soft: #A9B2A3;
  --ink-faint: #7C866F; --rule: #2A3126; --accent: #7FC3A2; --accent-soft: #21301F;
  --warn: #D8A94E; --warn-soft: #302713;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
}
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: "Newsreader", Georgia, "Times New Roman", serif;
  font-size: 17px; line-height: 1.6; -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 64rem; margin: 0 auto; padding: 3.5rem 1.5rem 6rem; }
a { color: inherit; }
a:focus-visible, summary:focus-visible, input:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 3px;
}
h1, h2, h3, h4, p, ul, ol, figure { margin: 0; }
ul, ol { padding: 0; }

.mono, .eyebrow, .stat-n, .stat-l, .pts, .chip,
th, td, input, .denom, .filter-note {
  font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}

.eyebrow {
  font-size: .72rem; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-faint); margin-bottom: 1rem;
  display: flex; flex-wrap: wrap; align-items: center; gap: .75rem;
}
h1 {
  font-size: clamp(2rem, 5vw, 3rem); font-weight: 600; line-height: 1.1;
  letter-spacing: -.02em; text-wrap: balance; margin-bottom: .9rem;
}
.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 1px; background: var(--rule); border: 1px solid var(--rule);
  border-radius: 3px; overflow: hidden; margin: 2.5rem 0 1rem;
}
.stat { background: var(--panel); padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .3rem; }
.stat-n { font-size: 1.6rem; font-weight: 600; line-height: 1; }
.stat-l { font-size: .68rem; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); }
.stat.good .stat-n { color: var(--accent); }

.chip {
  display: inline-flex; align-items: center; gap: .4rem; font-size: .66rem;
  letter-spacing: .09em; text-transform: uppercase; padding: .25rem .55rem;
  border: 1px solid var(--rule); border-radius: 2px; color: var(--ink-faint);
}
.chip.live { color: var(--warn); border-color: var(--warn); }
.chip.live::before {
  content: ""; width: .45rem; height: .45rem; border-radius: 50%;
  background: var(--warn); animation: hs-pulse 1.6s ease-in-out infinite;
}
@keyframes hs-pulse { 0%,100% { opacity: 1 } 50% { opacity: .2 } }
@media (prefers-reduced-motion: reduce) { .chip.live::before { animation: none; } }

.track { height: 4px; background: var(--rule); border-radius: 2px; overflow: hidden; margin: .5rem 0 .8rem; }
.fill { height: 100%; background: var(--accent); }

/* The judge's four rationales: three scored categories and two bullet lists,
   rendered by `rubric_rows()` / `listing()` inside an opened job. */
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

/* The filter toolbar and the self-scrolling box it drives. Every job list on the
   overview page is bounded this way, and the longest of them embeds thousands of
   never-scored rows as data rather than as markup. */
.toolbar { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin-bottom: 1rem; }
.toolbar input {
  flex: 1 1 16rem; padding: .55rem .7rem; font-size: .85rem;
  background: var(--panel); color: var(--ink);
  border: 1px solid var(--rule); border-radius: 3px;
}
.filter-note { font-size: .78rem; color: var(--ink-faint); }
.blank { color: var(--ink-faint); }

/* `overflow: auto` covers both axes, so wide columns scroll sideways in here
   instead of pushing the body sideways. */
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

table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-size: .64rem; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); font-weight: 600; }
td.wide { white-space: normal; min-width: 18rem; }
td.numeric { text-align: right; }
"""


def e(value) -> str:
    """HTML-escape anything, including None and numbers."""
    return html.escape("" if value is None else str(value), quote=True)


def num(value) -> str:
    """Thousands-separated, or an em dash when there is genuinely no number.

    An em dash rather than 0, for the same reason the results CSV leaves
    ``llm_score`` blank: a printed zero reads as a measured zero.
    """
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def usd(value) -> str:
    """A dollar figure to the cent, or an em dash when there is no measurement.

    Separate from ``num`` because that one casts to ``int``, which would render
    $1.87 as "1". Same em-dash-not-zero rule: a run whose backend cannot read its
    own meters has no cost figure, and printing $0.00 would claim it was free.
    """
    if value is None:
        return "—"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def pct(part: int | None, whole: int | None) -> float:
    """`part` as a percentage of `whole`, clamped, safe on zero and None."""
    if not whole or part is None:
        return 0.0
    return max(0.0, min(100.0, 100.0 * part / whole))


def local_time(iso: str | None) -> str:
    """Render a stored UTC timestamp in the reader's local time.

    Every timestamp in the database is UTC because it has to sort and must not
    collide when the clock goes back an hour; every timestamp on these pages is
    local because a person is reading it.
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def duration(start_iso: str | None, end_iso: str | None = None) -> str:
    """How long a span took, as "18m 42s" — or how long it has been going.

    A missing ``end_iso`` means the sweep has not finished, so *now* stands in and
    the figure climbs with each refresh of the page. An em dash when there is no
    start at all, for the same reason ``num`` uses one: an unmeasured span is not a
    zero-length one.
    """
    if not start_iso:
        return "—"
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
            if end_iso else datetime.now(timezone.utc)
        )
    except (ValueError, TypeError):
        return "—"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    total = int((end - start).total_seconds())
    if total < 0:
        return "—"
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def rubric_rows(job: dict, rubric) -> str:
    """The scoring rubric as labelled bars, one per category, with its rationale.

    Used by the overview page inside an opened job. `rubric` is
    `reporting.data.RUBRIC`, passed in rather than imported so this module stays a
    pure renderer with no dependency on the data layer.
    """
    rows = []
    for score_key, rationale_key, label, maximum in rubric:
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


def listing(job: dict, key: str, title: str, css: str) -> str:
    """One of the judge's bullet lists — what matched, or what counted against it."""
    items = [str(x) for x in (job.get(key) or []) if str(x).strip()]
    if not items:
        return ""
    lis = "".join(f"<li>{e(x)}</li>" for x in items)
    return f'<section class="notes {css}"><h4>{e(title)}</h4><ul>{lis}</ul></section>'


def document(title: str, body: str, refresh_s: int | None = None,
             extra_css: str = "") -> str:
    """A complete standalone HTML document. The only envelope there is.

    ``refresh_s`` arms a meta refresh, and is passed only while a sweep is actually
    running. A page that keeps reloading after the run has finished burns battery
    and, worse, makes a finished run look like it is still going.

    ``extra_css`` is not optional decoration: a caller that defines its own rules
    and cannot pass them here has written dead CSS, silently, which is exactly what
    once happened to a page whose own rules never reached it.
    """
    meta = f'<meta http-equiv="refresh" content="{int(refresh_s)}">\n' if refresh_s else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{meta}<title>{e(title)}</title>\n{FONTS}\n"
        f"<style>{BASE_CSS}{extra_css}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
