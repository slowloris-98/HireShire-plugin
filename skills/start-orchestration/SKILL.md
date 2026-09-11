---
name: start-orchestration
description: Keep sweeping the job boards on a schedule, in a background shell task, until it is stopped or hits its 24-hour limit.
---

# Start orchestration

Start the recurring sweep as a background task and tell the user what is true about it.

Do not describe the sweep's state beyond what you were handed. Two live failures came
from this skill asserting things it had not checked: once announcing that sweeps had
begun when nothing was running, once naming a directory it had guessed. The launcher
answers the directory question (`--paths`); the background task answers the liveness
question by existing in the user's task list and by notifying you when it ends.

## Step 1 — find the data directory

Never guess it, and never substitute the CLAUDE_PLUGIN_DATA placeholder: it does not
resolve to the same directory in the Claude desktop app as in the terminal or the VS
Code extension.

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
```

It prints `ROOT=<path>` and `DATA=<path>`.

## Step 2 — start it

Run exactly this, as a **background** task:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --monitor
```

Two rules, both learned from a session that improvised its own command:

- **Never detach it** — no `nohup`, no `disown`, no OS-level backgrounding. Keep it a
  background task of this session so the user can see it in their task list and stop it
  there, and so you are told when it ends.
- **Never call `orchestrate.py` for a recurring run.** `--monitor` is the only
  entrypoint that reads `poll_interval_hours` from the user's config.
  `orchestrate.py --now` takes `--interval` with a **4-hour default** and never looks
  at their setting, so a user who chose 12 hours would silently get 4.

`--monitor` prints one line on startup naming the interval, and refuses to start if a
sweep is already running. **Relay that line rather than composing your own** — it is the
only thing here that knows the user's actual interval.

## Step 3 — report

Tell the user, plainly:

- **How often** it sweeps — from the startup line, not from memory.
- **How it ends.** It runs until one of three things: they run `--stop`, they kill the
  shell task, or it reaches its **24-hour limit** and stops by itself. Say this
  accurately. It is *not* tied to this session any more — closing Claude Code does not
  reliably stop it, and earlier versions of this skill promised that it did. If they
  want it to keep going beyond a day, the OS scheduler entry `/hireshire:setup` offers
  is the supported way.
- **How to stop it:**
  ```bash
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --stop
  ```
- **Where to watch it** without waiting on you: the dashboard at
  `<results root>/dashboard.html`. It is local, rewrites itself continuously while a
  sweep runs, and costs them nothing. The results root is
  `<workspace_dir>/hireshire_run_results/`, or `<DATA>/results/` when `workspace_dir`
  is empty.

If auto-apply is enabled in their config, say plainly that each sweep will also open a
browser and **submit real applications**, unattended and with no confirmation step.
There is no rehearsal mode; `enable_applier` is the only thing holding it back. This
matters more than it used to: nothing stops the sweep automatically when they walk away,
so the 24-hour bound and `--stop` are the only limits.

## While it runs

Each cycle emits one summary line: how many matches were found, the best score, and when
the next sweep is due. Relay those as they arrive; do not go looking for more detail
unless the user asks.

**Republish the match report on each of those summary lines, and only then.** The
engine writes it to `<results root>/latest_matching.html` on every sweep, and
`<DATA>/last_run.json` carries the exact path as `latest_matching_html` once a sweep has
finished.

Publish it with the **Artifact** tool, always to the same URL: call the tool with
`action: "list"` first, find the artifact titled **HireShire Match Report**, and
pass its `url`. If there is no such artifact yet, the first publish creates it.
One link, updated every cycle — not one per sweep.

Do not tail the engine log for progress here. A recurring sweep runs unattended for
hours, and a report republished on every internal milestone would put several messages
into the session every few hours. One per completed cycle is the right rate.

**When the task ends you will be notified** with its exit code. Say what happened rather
than assuming: a clean end means the 24-hour bound or a `--stop`; a failure means
something broke, and `<DATA>/logs/orchestration.log` says what. Offer to restart it.

Two log files, which are easy to confuse:

- `<DATA>/logs/orchestration.log` — the recurring loop. Start here.
- `<DATA>/logs/orchestrate.log` — the engine's own log for one sweep, for detail.
