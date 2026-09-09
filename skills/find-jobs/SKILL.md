---
name: find-jobs
description: Sweep the job boards once and score the results against your resume. Produces a ranked CSV of matches. Run /hireshire:setup first.
---

# Find jobs

One sweep, scored, ranked, written to a CSV.

## Preconditions

First find the data directory. Never guess it and never substitute the
CLAUDE_PLUGIN_DATA placeholder: it does not resolve to the same directory in the
Claude desktop app as in the terminal or the VS Code extension, so a guess can make
a configured install look empty. Ask instead, once, and reuse the answer:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
```

It prints `ROOT=<path>` and `DATA=<path>`.

Check that `<DATA>/config/matcher.yaml` exists and has a `resume_path`. If it does
not, the user has not run `/hireshire:setup` — say so and stop rather than running
with defaults that will match nothing.

`settings.workspace_dir` in `<DATA>/config/scraper.yaml` is where results go.
**An empty value is not an error** — installs that predate the setting keep writing
to the plugin's own data directory until the user re-runs setup. Note it and carry
on; only mention it when reporting where the results landed.

## Run it

Start the sweep as a **background** task, so the reports below can be published
while it is still going:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" orchestrate.py --once
```

Go through the launcher rather than calling `python` — it resolves a working
interpreter on macOS, Linux and Windows and runs inside the plugin venv.

This takes roughly 20 minutes on the default board set, most of it rate-limited
waiting on the boards themselves. Tell the user that up front. If they enabled
Workday and BambooHR at setup, expect considerably longer.

**Give them the dashboard path in the same breath**, because it is what makes the
wait legible:

```
<results root>/dashboard.html
```

The engine rewrites it every few seconds and it reloads itself while a sweep is
running, so opening it in a browser shows employers and postings climbing in real
time. The results root is `<workspace_dir>/hireshire_run_results/`, or
`<DATA>/results/` when `workspace_dir` is empty.

If the plugin venv is not ready — a fresh install, or one whose setup never
finished — the launcher installs it first, which adds a one-time ~2 GB download
and 10-15 minutes before the sweep even starts. **Say so before you launch**, not
after they have watched a still spinner for ten minutes.

What happens inside, in case they ask why it is not instant:

1. Every enabled board is swept for postings newer than their age cutoff.
2. Cheap title gates drop the obvious misses for free.
3. Descriptions are fetched for the survivors that need one.
4. A cross-encoder reads each full description against their candidate profile.
5. Only jobs reaching `funnel.rerank.min_score` are sent to the LLM for a real
   0-100 score.

Steps 1-4 run on their own machine and cost nothing, so step 5 is the only part
that spends anything. That is also why results arrive throughout the run rather
than all at once at the end: each employer's jobs go through every stage as soon
as they are scraped.

## While it runs — publish the match report

The engine writes an HTML report of the run to
`<results root>/latest_matching.html` and keeps it current. Publish that file with
the **Artifact** tool, and keep republishing it as the sweep advances.

**Always publish to the same URL.** Before the first publish, call the Artifact
tool with `action: "list"` and look for an artifact titled **HireShire Match
Report**. If it is there, pass its `url` on every publish so this run replaces the
last one. If it is not, publish without a `url` — that first call creates it, and
every later call in this session can just republish the same file path.

Never invent the path. Take it from `latest_matching_html` in `<DATA>/last_run.json`
once the run has written one, or build it from the results root you already
resolved above.

To catch the sweep advancing, watch the engine log with the **Monitor** tool:

```bash
tail -f "<DATA>/logs/orchestrate.log" | grep -E --line-buffered "Sweep progress:|Budget:|Matcher done|Pipeline complete|Pipeline failed|Traceback"
```

Republish the artifact on each line that arrives, and relay the line to the user in
a few words. The filter deliberately covers the failure signatures as well as the
progress ones: a filter that matched only good news would stay silent through a
crash, and silence looks exactly like a sweep still running.

Expect four or five lines in total. Do not add a poll loop of your own on top of
this, and do not republish more often than the lines arrive — the page cannot
change faster than the engine rewrites it.

One thing to say plainly if the user asks why the report shows no scores for most
of the run: **scoring happens at the end**. Top-K is a decision across the whole
sweep, so no job can be scored until every job has been seen. The scrape counts are
live; the reasoning arrives in the last couple of minutes.

## Report back

Results land in the user's own job-search folder:

```
<workspace_dir>/hireshire_run_results/<date>_<time>/<date>_<time>_results.csv
```

The run prints that exact path on its last line (`Results: …`) — read it from
there rather than reconstructing it. `<DATA>/last_run.json` holds the same path if
you need it later. If `workspace_dir` is empty, the run wrote to `<DATA>/results/`
instead; say so and mention that re-running `/hireshire:setup` moves results into a
folder of their own.

Read the CSV and show the shortlisted jobs as a table sorted by score — title,
company, location, score, and the URL. Give them the path too.

Republish the match report one last time now that the run is finished, and give
them the artifact link alongside the CSV path. That page is where the *reasoning*
lives — the four rationales behind every score, and the full list of jobs that
were considered but never scored. It answers "why didn't I see that job?", which
the CSV cannot. Point them at it especially when the shortlist is empty.

If the run reports it could not write the CSV, the file was locked — almost always
open in Excel. The results are safe in the database; tell them to close it and
re-run to get the CSV.

Two things worth surfacing if the numbers warrant it:

- **Nothing shortlisted?** The threshold may be too high, or the target titles
  too narrow. Both are one `/hireshire:setup` answer away. Do not just report
  zero and stop — send them to the match report, which shows exactly how close
  the best jobs came and what the judge held against them.
- **A lot of jobs dropped before the judge?** The run summary and the match report
  break this into two numbers, and they mean opposite things — do not merge them:
  - `rerank_below_cutoff` means the cross-encoder read the whole description and
    said no. That is a verdict, so those jobs are retired and will not come back.
    Lowering `funnel.rerank.min_score` is what lets more through.
  - `llm_call_cap_reached` means the run hit `funnel.top_k` and simply ran out of
    calls. Those stay eligible next run, so raising `top_k` recovers them.
- **Nothing above the cutoff at all?** Read the funnel counts in the match report
  before concluding the market is quiet. A large "reached the reranker" number with
  zero above the cutoff means `min_score` is set wrong for this resume, not that
  there were no jobs. `scripts/calibrate_cutoffs.py` derives the right value from
  their own past runs.

If auto-apply is enabled, remind them `/hireshire:apply` is the next step.
