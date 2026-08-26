"""The shared look, and the two different HTML envelopes the reports need.

One design system, two delivery shapes, and the difference is not cosmetic:

* **The dashboard is a local file.** It is opened with ``file://`` from the user's
  own results folder and is never published, so it is a complete document —
  doctype, ``<html>``, ``<head>`` — and it may carry a meta refresh.
* **The matching report is artifact-ready.** Claude Code's Artifact tool wraps the
  file it publishes in its own ``<!doctype html>…<head></head><body>`` skeleton, so
  a page that supplies those tags itself ends up nested inside a second copy of
  them. The matching report therefore emits *body content only*.

That second constraint has a consequence which looks like a bug otherwise: opening
the matching report straight from disk puts the browser in quirks mode. Everything
here is written to survive that — an explicit ``box-sizing`` on every element,
flex and grid for layout, no percentage heights — so the local file and the
published artifact render the same.

Colour lives in tokens defined three times over (bare ``:root``, then
``prefers-color-scheme`` guarded against an explicit light choice, then
``[data-theme="dark"]``) because the Artifact viewer has three theme states, not
two: an explicit choice stamps the root element, and the default "system" setting
stamps nothing at all. A colour whose only definition sits inside a media query
never applies in that un-stamped state, which is how a page ends up rendering one
theme's text on the other theme's ground.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# Google Fonts is the only external host the Artifact CSP admits. Every face still
# names a real fallback stack — on a machine with no network, or inside the local
# file, the page has to stay readable.
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
a:focus-visible, button:focus-visible, input:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 3px;
}
h1, h2, h3, h4, p, ul, ol, figure { margin: 0; }
ul, ol { padding: 0; }

.mono, .eyebrow, .stat-n, .stat-l, .pts, .rank, .total, .funnel-scores,
.cutoff-note, .verdict, .chip, th, td, input, button, .step-n, .step-l,
.step-bar-l, .denom, .filter-note {
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
.standfirst { max-width: 42rem; color: var(--ink-soft); font-size: 1.08rem; }
.standfirst b { color: var(--ink); font-weight: 600; }

h2.section {
  font-size: .74rem; letter-spacing: .14em; text-transform: uppercase;
  font-family: "IBM Plex Mono", monospace; color: var(--ink-faint);
  padding-bottom: .6rem; border-bottom: 1px solid var(--rule);
  margin: 3.25rem 0 1.75rem;
}

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 1px; background: var(--rule); border: 1px solid var(--rule);
  border-radius: 3px; overflow: hidden; margin: 2.5rem 0 1rem;
}
.stat { background: var(--panel); padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .3rem; }
.stat-n { font-size: 1.6rem; font-weight: 600; line-height: 1; }
.stat-l { font-size: .68rem; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); }
.stat.flag .stat-n { color: var(--warn); }
.stat.good .stat-n { color: var(--accent); }

.note {
  border-left: 3px solid var(--warn); background: var(--warn-soft);
  padding: .9rem 1.1rem; border-radius: 0 3px 3px 0; color: var(--ink-soft);
  font-size: .97rem;
}
.note b { color: var(--ink); font-weight: 600; }
.note.calm { border-left-color: var(--accent); background: var(--accent-soft); }

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

/* The funnel, drawn as stacked proportional bars. Used by both reports. */
.funnel { display: flex; flex-direction: column; gap: .1rem; margin-top: 1rem; }
.step {
  display: grid; grid-template-columns: 1fr auto; align-items: baseline;
  gap: 1rem; background: var(--panel); border: 1px solid var(--rule);
  border-radius: 3px; padding: .75rem .95rem;
}
.step-bar-l { font-size: .7rem; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-soft); }
.step-n { font-size: 1.15rem; font-weight: 600; }
.step-track { grid-column: 1 / -1; height: 4px; background: var(--rule); border-radius: 2px; overflow: hidden; }
.step-fill { height: 100%; background: var(--accent); }
.step.drop .step-fill { background: var(--warn); }
.step-note { grid-column: 1 / -1; font-size: .9rem; color: var(--ink-faint); }

.track { height: 4px; background: var(--rule); border-radius: 2px; overflow: hidden; margin: .5rem 0 .8rem; }
.fill { height: 100%; background: var(--accent); }

.scroll-x { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-size: .64rem; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); font-weight: 600; }
td.wide { white-space: normal; min-width: 18rem; }
td.numeric { text-align: right; }

footer {
  margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--rule);
  color: var(--ink-faint); font-size: .88rem;
}
footer code {
  font-family: "IBM Plex Mono", monospace; font-size: .82rem;
  color: var(--ink-soft); overflow-wrap: anywhere;
}
"""


def e(value) -> str:
    """HTML-escape anything, including None and numbers."""
    return html.escape("" if value is None else str(value), quote=True)


def num(value) -> str:
    """Thousands-separated, or an em dash when there is genuinely no number.

    An em dash rather than 0, for the same reason the all-jobs CSV leaves
    ``llm_score`` blank: a printed zero reads as a measured zero.
    """
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


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


def funnel_step(label: str, value: int | None, whole: int | None,
                note: str = "", drop: bool = False) -> str:
    """One proportional bar in the funnel diagram."""
    cls = "step drop" if drop else "step"
    note_html = f'<p class="step-note">{e(note)}</p>' if note else ""
    return (
        f'<div class="{cls}">'
        f'<span class="step-bar-l">{e(label)}</span>'
        f'<span class="step-n">{num(value)}</span>'
        f'<div class="step-track"><div class="step-fill" style="width:{pct(value, whole):.2f}%"></div></div>'
        f"{note_html}</div>"
    )


def document(title: str, body: str, refresh_s: int | None = None) -> str:
    """A complete standalone HTML document — for the dashboard, which is local-only.

    ``refresh_s`` arms a meta refresh, and is passed only while a sweep is actually
    running. A page that keeps reloading after the run has finished burns battery
    and, worse, makes a finished run look like it is still going.
    """
    meta = f'<meta http-equiv="refresh" content="{int(refresh_s)}">\n' if refresh_s else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{meta}<title>{e(title)}</title>\n{FONTS}\n"
        f"<style>{BASE_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def artifact_page(title: str, body: str, extra_css: str = "", tail: str = "") -> str:
    """Body content only — no doctype, no ``<html>``, ``<head>`` or ``<body>``.

    This is what the Artifact tool expects: it supplies that skeleton itself, and a
    page that brings its own ends up nested inside it. The ``<title>`` tag stays,
    because the tool scans the first 8 KB of the file for one to name the artifact
    — and that name is what makes the rolling-URL lookup work across sessions.
    """
    return (
        f"<title>{e(title)}</title>\n{FONTS}\n"
        f"<style>{BASE_CSS}{extra_css}</style>\n{body}\n{tail}\n"
    )
