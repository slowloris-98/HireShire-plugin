---
name: apply
description: Fill out application forms for shortlisted jobs using a real browser, uploading your resume and answering questions from it. Respects dry_run.
---

# Apply

Fills and (optionally) submits applications for the jobs shortlisted by the last
run, driving a real browser through Playwright MCP.

**This submits real applications when `dry_run` is false.** Two gates protect
that: `enable_applier` and `dry_run`, both of which setup leaves in the safe
position. Never change either on the user's behalf.

Playwright tools are namespaced by the plugin. Use the full names:
`mcp__plugin_hireshire_playwright__browser_navigate`,
`..._browser_snapshot`, `..._browser_type`, `..._browser_click`,
`..._browser_select_option`, `..._browser_file_upload`,
`..._browser_take_screenshot`.

## Step 1 — Build the queue

Read `${CLAUDE_PLUGIN_DATA}/config/applier.yaml` for `dry_run`, `first_name`,
`last_name`, `email`, `phone`, `resume_path`, `inter_job_delay_s`,
`applied_dir`, `generate_cover_letter` and `exclude_companies`.

Read `${CLAUDE_PLUGIN_DATA}/last_run.json` and open the file its `json` field
points at. That pointer exists because the results root is now a folder the user
chose and can move — do not go looking for the directory yourself.

If `last_run.json` is missing (no run since the plugin was updated), fall back:
take `settings.workspace_dir` from `${CLAUDE_PLUGIN_DATA}/config/scraper.yaml` and
list `<workspace_dir>/hireshire_run_results/`, or `${CLAUDE_PLUGIN_DATA}/results/`
when it is empty; sort by name, take the last, and read the `*_results.json` in it
— named `pipeline_results.json` for runs made before this layout.

Get the already-applied job ids:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/applied_cli.py list
```

If the plugin venv is not ready, this first launcher call installs it — a one-time
~2.5 GB download taking 10-15 minutes. Tell the user before you run it, so the wait
is expected rather than a hang.

Queue a job when **all** of these hold:

- it is not already in the applied list
- its `company` is not in `exclude_companies` (those sit behind account logins —
  the applier cannot get past them; the user applies to those manually)
- `relevance_score` is present, or scoring was skipped for the run

Upload `settings.resume_path` for every job — the user's own resume. Skip any job
whose resume file does not exist on disk, and say so rather than continuing
silently.

If the queue is empty, say why (nothing new, or everything excluded) and stop.

## Step 2 — Read the resume once

Read the resume PDF with the Read tool. It is the ground truth for every question
you answer later. Do not answer from the job description or from memory.

## Step 3 — Per job

### 3a. Navigate and snapshot

Navigate to `job_url`, then snapshot. Identify every visible field: text inputs,
dropdowns, radios, checkboxes, file inputs, textareas — with labels and refs.

If the page redirects or shows a "Sign in to apply" gate instead of a form,
record it as an error and move on.

**Location check.** If the page states a location and it does not
case-insensitively contain any of `united states`, `us`, `remote`, `india`,
`worldwide`, `anywhere`, skip the job with a short message and do not record it.
No location text at all means continue.

### 3b. Identity fields

Fill first name, last name, email, phone from config. No reasoning needed.

### 3c. Resume upload

Find the resume/CV file input and upload `settings.resume_path`.

### 3d. Cover letter

Only if `generate_cover_letter` is true and the form asks for one. Three
paragraphs: why this role and company; two or three genuinely relevant
experiences drawn from the resume; a forward-looking close.

### 3e. Remaining questions

Reason from the resume, the job title and company, and the URL. The rules that
matter:

- **Never fabricate experience or qualifications that are not in the resume.**
  This is the one hard rule — a wrong answer here is a lie told in the user's
  name, on a real application.
- Years of experience: estimate conservatively from the resume's dates.
- Demographic / EEO questions: "Prefer not to answer" or "Decline to
  self-identify", always.
- "How did you hear about us": "Job board".
- Work authorization: yes. Sponsorship required: no. Unless the resume
  contradicts it.

If a required question cannot be answered honestly from the resume, stop that
job, record it as an error explaining which question blocked it, and move on.

Multi-page forms: fill what is visible, click Next/Continue, snapshot, repeat.

### 3f. Screenshot, then decide

Take a screenshot and keep the path.

- **`dry_run: true`** — do not click submit, apply, or send. Status `dry_run`.
- **`dry_run: false`** — click submit, confirm it went through (confirmation text
  or page change). Status `submitted`, or `error` if it did not.

### 3g. Record

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/applied_cli.py record \
  --job-id "<job_id>" --board-token "<company>" --title "<title>" \
  --url "<job_url>" --status "dry_run|submitted|error" \
  --dry-run "true|false" [--screenshot "<path>"] [--error "<message>"]
```

Omit `--screenshot` and `--error` when there is no value.

### 3h. Pause

Wait `inter_job_delay_s` seconds before the next job.

## Step 4 — Summary

A table of Company / Title / Status / Screenshot, then totals for submitted,
dry_run and error.

If everything ran in dry-run, say so explicitly and tell the user where to look
at the screenshots — they should watch it work before turning `dry_run` off.

## Errors

Any per-job failure: record `status=error` with the message, screenshot if
possible, continue to the next job. Never abort the whole run because one form
misbehaved.
