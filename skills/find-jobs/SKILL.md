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

Start the sweep as a **background** task, so you can relay its progress while it
is still going:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --sweep
```

Go through the launcher rather than calling `python` — it resolves a working
interpreter on macOS, Linux and Windows and runs inside the plugin venv.

**Use `--sweep`, never `orchestrate.py --once`.** They run the same pipeline, but
`--sweep` records its pid, so `--stop` can end it and a second sweep will not start
alongside it. The old form went through a second launcher that recorded nothing,
leaving a find-jobs sweep unreachable by `--stop`.

If a recurring sweep is already running, `--sweep` will decline rather than start a
second writer on the same database. That is correct — say so plainly and tell the
user the running sweep's next cycle covers this.

This takes roughly 20 minutes on the default board set, most of it rate-limited
waiting on the boards themselves. Tell the user that up front. If they enabled
Workday and BambooHR at setup, expect considerably longer.

**Give them the overview page in the same breath**, because it is what makes the
wait legible:

```
<results root>/overview.html
```

The engine rewrites it every few seconds and it reloads itself while a sweep is
running, so opening it in a browser shows the counts climbing and jobs arriving
with the reasoning behind their scores. The results root is
`<workspace_dir>/hireshire_run_results/`, or `<DATA>/results/` when
`workspace_dir` is empty. That page covers every sweep the install has done; this
sweep gets its own copy in its run folder, which `last_run.json` names as
`run_overview_html` once the run has written one.

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

## While it runs — relay the progress

There is nothing to publish. Both overview pages are local files the engine
rewrites on a clock, and they reload themselves while a sweep is running — so the
user watches them directly and you do not stand between them and their own data.

To catch the sweep advancing, watch the engine log with the **Monitor** tool:

```bash
tail -f "<DATA>/logs/orchestrate.log" | grep -E --line-buffered "Sweep progress:|Budget:|Matcher done|Pipeline complete|Pipeline failed|Traceback"
```

Relay each line to the user in a few words. The filter deliberately covers the
failure signatures as well as the progress ones: a filter that matched only good
news would stay silent through a crash, and silence looks exactly like a sweep
still running.

Expect four or five lines in total. Do not add a poll loop of your own on top of
this.

If the user asks when results start appearing: **throughout the run**. Each
employer's jobs go through every stage as soon as they are scraped, so scored jobs
and their reasoning land on the overview page from the first few minutes on. What
arrives at the end is the CSV.

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

That one file holds **every job that reached the funnel**, best first, with eight
columns: `posted_at`, `company`, `job_title`, `link`, `llm_score`, `cross_score`,
`applied`, `shortlisted`. A blank `llm_score` means no judge ever read that job —
it was dropped by a free gate or ran out of the run's call budget — and is not a
score of zero. Read the file and show the shortlisted rows as a table sorted by
score; give them the path too.

Then point them at the run's own overview page (`run_overview_html` in
`<DATA>/last_run.json`). That is where the *reasoning* lives — the rationales
behind every score, and the jobs that were never scored with the reason why. It
answers "why didn't I see that job?", which the CSV cannot. Say so especially when
the shortlist is empty.

If the sweep failed part-way, the CSV is still written, holding everything the run
judged before it stopped — say that rather than implying the work was lost, and
note that `last_run.json` records `"complete": false` for that run.

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
