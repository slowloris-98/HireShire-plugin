---
name: mark-applied
description: Record what you did about a job yourself — that you applied to it, or that you are not pursuing it — so it stops sitting under Needs Attention or waiting on the applier.
---

# Mark a job applied, or not pursuing

The sweep applies to most shortlisted jobs by itself. Three of its outcomes hand the job
back to the user instead, and all three land under **Needs Attention** on the dashboard:

- a session that stopped short of submitting (`error`),
- an employer whose portal needs an account login (`excluded`),
- a job no session ever got through for, until the retry window closed (`expired`).

This skill records what the user did about one of those, and about a job still under
**Jobs Shortlisted** that they applied to before the sweep reached it.

Two outcomes, and they are not the same thing:

| The user says | What is recorded |
| --- | --- |
| they applied to it themselves | an application — it moves to **Jobs Applied** and counts in the `Jobs applied` tile |
| they are not pursuing it | **no application** — it moves to **Jobs Filtered**, labelled, and the applier stops offering it |

**Both are one-way.** There is no undo, so do not guess which one the user meant: if their
wording is ambiguous ("I dealt with that one", "drop it"), ask before writing.

## Step 1 — which jobs

If the user's message already names the action and the job — the dashboard's buttons put
`/hireshire:mark-applied applied <job_id>` or `/hireshire:mark-applied declined <job_id>` on
the clipboard for exactly this — skip to Step 2 and use it as given.

Otherwise, list what is waiting:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/jobs_cli.py list
```

It prints JSON with two arrays, covering every sweep this install has done:

- `attention` — jobs with a failed or abandoned application. Each carries `status`,
  `reason` and `at`.
- `shortlisted` — jobs still waiting on the applier, never applied to.

Show them as two numbered lists, `attention` first, one line each: the title, the company,
and for the attention half its `reason`. Then ask which jobs, and which of the two outcomes
applies. Nothing is written until they answer.

Never invent or reconstruct a `job_id`. Use only the ids this command printed or the user
pasted.

## Step 2 — record it

One command per outcome, repeating `--job-id` for as many jobs as that outcome covers:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/jobs_cli.py applied --job-id "<job id>"
```

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/jobs_cli.py declined --job-id "<job id>"
```

If the user chose both outcomes across different jobs, that is two commands — never mix the
two in one, and never pass a job id to the outcome the user did not choose for it.

## Step 3 — say what happened

The command prints a `results` array, one entry per job, and `pages`.

Report each job's `result` as it stands, and nothing more:

- `updated` / `inserted` — recorded as applied.
- `declined` — recorded as not pursuing.
- `unknown` — nothing on record names that job id, so nothing was written. Say so; do not
  retry with a different id.
- `nothing_to_change` — it was already recorded that way.

`pages` holds the two dashboard files the command rewrote, so the change is already on
them. Hand over the `overview` path — that page covers every sweep. If `pages` is empty,
either nothing changed or no sweep has finished yet; say that rather than naming a path.

Do not describe anything else about the sweep's state. Whether one is running now is not
something this skill asked, and it must not be implied.
