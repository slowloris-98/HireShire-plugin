---
name: apply
description: Fill out and submit application forms for shortlisted jobs that have not been applied to yet, using a real browser, uploading your resume and answering questions from it.
---

# Apply

Fills and submits applications for shortlisted jobs that have no application record
yet, driving a real browser through Playwright MCP.

**This submits real applications to real employers.** There is no rehearsal mode —
`dry_run` was removed, because a permanent rehearsal is indistinguishable from a
broken applier. `enable_applier` is the only gate, setup leaves it off, and you must
never turn it on for the user.

With `enable_applier` on, sweeps already apply to each job **as it is shortlisted**,
mid-sweep. This skill is the manual catch-up: jobs still pending because an apply
session could not start, or because the user wants to apply now rather than wait for
the next sweep.

Playwright tools are namespaced by the plugin. Use the full names:
`mcp__plugin_hireshire_playwright__browser_navigate`,
`..._browser_snapshot`, `..._browser_type`, `..._browser_click`,
`..._browser_select_option`, `..._browser_file_upload`,
`..._browser_take_screenshot`.

## Step 1 — Build the queue

First find the data directory. Never guess it and never substitute the
CLAUDE_PLUGIN_DATA placeholder: it does not resolve to the same directory in the
Claude desktop app as in the terminal or the VS Code extension, so a guess can make
a configured install look empty. Ask instead, once, and reuse the answer:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
```

It prints `ROOT=<path>` and `DATA=<path>`.

Read `<DATA>/config/applier.yaml` for `enable_applier`, `first_name`, `last_name`,
`email`, `phone`, `resume_path`, `inter_job_delay_s`, `generate_cover_letter` and
`exclude_companies`.

If `enable_applier` is false, stop here and say so. Do not apply to anything, and do
not offer to flip it.

Get the pending jobs — shortlisted, never applied to, cluster duplicates already
removed, best first:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/applied_cli.py pending
```

It prints a JSON list of `job_id`, `company`, `title`, `job_url`, `relevance_score`.
Do not go looking for result files instead; the database is the source of truth, and
it is the same list a sweep's applier works from.

If the plugin venv is not ready, this first launcher call installs it — a one-time
~2 GB download taking 10-15 minutes. Tell the user before you run it, so the wait
is expected rather than a hang.

Queue every pending job whose `company` is not in `exclude_companies` (those sit
behind account logins — the applier cannot get past them; the user applies to those
manually).

**Keep the excluded ones in a separate list as you go.** They are not failures and
must not be silently dropped: a whole shortlist can consist of them — one real run
shortlisted three jobs, all at an excluded employer, and the applier would have
reported nothing to do without ever saying why. You report them in Step 4.

If `settings.resume_path` does not exist on disk, stop and say so — every
application uploads it.

If the queue is empty, say why — nothing pending, or every pending job was at an
excluded employer — and print the "Apply manually" list from Step 4 before stopping.

## Step 2 — Read the rules and the resume once

Read `${CLAUDE_PLUGIN_ROOT}/hireshire/applier/apply_one.md`. It is the per-job
procedure, and the same one a sweep's applier follows, so follow it exactly.

Read the resume PDF with the Read tool. It is the ground truth for every question you
answer later.

## Step 3 — Per job

A sweep may be applying at the same time. Immediately before each job, check it has
not been applied to since you built the queue:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/applied_cli.py list
```

Skip the job if its `job_id` is listed.

Then follow `apply_one.md` for the job. Its inputs are the job row (`job_url`,
`company`, `title`), the applicant's details and `generate_cover_letter` from the
config, and `resume_path`. It ends in one outcome: `submitted`, `error` or
`skipped_location`.

Record `submitted` and `error`; do not record `skipped_location`:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/applied_cli.py record \
  --job-id "<job_id>" --board-token "<company>" --title "<title>" \
  --url "<job_url>" --status "submitted|error" \
  [--screenshot "<path>"] [--error "<message>"]
```

Omit `--screenshot` and `--error` when there is no value.

Wait `inter_job_delay_s` seconds before the next job.

## Step 4 — Summary

A table of Company / Title / Status / Screenshot, then totals for submitted and
error.

Then, **always**, an **Apply manually** section listing the pending jobs you set
aside in Step 1 because their company is in `exclude_companies` — company, title and
the job URL for each. Print the heading even when the list is empty, and say the list
is empty; the whole point is that an excluded job never disappears without a trace.

Introduce it with one line of why: those employers require an account login before
the form appears, so the applier cannot complete them and the user needs to apply
themselves.

## Errors

Any per-job failure: record `status=error` with the message, screenshot if
possible, continue to the next job. Never abort the whole run because one form
misbehaved.
