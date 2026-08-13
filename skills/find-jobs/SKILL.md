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

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" orchestrate.py --once
```

Go through the launcher rather than calling `python` — it resolves a working
interpreter on macOS, Linux and Windows and runs inside the plugin venv.

This takes roughly 20 minutes on the default board set, most of it rate-limited
waiting on the boards themselves. Tell the user that up front. If they enabled
Workday and BambooHR at setup, expect considerably longer.

If the plugin venv is not ready — a fresh install, or one whose setup never
finished — the launcher installs it first, which adds a one-time ~2.5 GB download
and 10-15 minutes before the sweep even starts. **Say so before you launch**, not
after they have watched a still spinner for ten minutes.

What happens inside, in case they ask why it is not instant:

1. Every enabled board is swept for postings newer than their age cutoff.
2. Cheap title gates drop the obvious misses for free.
3. Descriptions are fetched for the survivors that need one.
4. A cross-encoder ranks all of them against their candidate profile.
5. Only the top `funnel.top_k` are sent to the LLM for a real 0-100 score.

Step 5 is why the run is affordable. Everything before it exists to make sure the
budget is spent on the right jobs.

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

If the run reports it could not write the CSV, the file was locked — almost always
open in Excel. The results are safe in the database; tell them to close it and
re-run to get the CSV.

Two things worth surfacing if the numbers warrant it:

- **Nothing shortlisted?** The threshold may be too high, or the target titles
  too narrow. Both are one `/hireshire:setup` answer away. Do not just report
  zero and stop.
- **A lot of jobs over budget?** The run summary reports how many cleared every
  gate but lost the top-K race. Those are recorded with
  `rerank_below_top_k` and stay eligible next run, so raising `top_k` recovers
  them. Mention it when the number is large relative to `top_k`.

If auto-apply is enabled, remind them `/hireshire:apply` is the next step.
