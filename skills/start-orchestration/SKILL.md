---
name: start-orchestration
description: Keep sweeping the job boards on a schedule for as long as this session stays open, reporting one summary per cycle.
---

# Start orchestration

Start the recurring sweep, **confirm it is actually running**, and report only what
you confirmed.

An earlier version of this skill announced that sweeps had begun simply because it had
been invoked. The mechanism behind that claim did not fire on every interface, so users
were told a sweep was live while nothing was running. Never describe the state of this
without asking for it.

## Step 1 — find the data directory

Never guess it, and never substitute the CLAUDE_PLUGIN_DATA placeholder: it does not
resolve to the same directory in the Claude desktop app as in the terminal or the VS
Code extension.

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
```

It prints `ROOT=<path>` and `DATA=<path>`.

## Step 2 — check whether one is already running

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --status
```

If it reports running, **stop here** and relay that line — a second sweeper would
scrape the same boards and write the same database. Say when the next sweep is due.

## Step 3 — start it

Run exactly this, as a **background** task:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --monitor
```

Two rules, both learned from a session that improvised its own command:

- **Never detach it** — no `nohup`, no `disown`, no OS-level backgrounding. You are
  about to tell the user this stops when the session ends, so it has to be a child of
  the session. A detached sweeper keeps running after they close Claude Code, keeps
  scoring against their subscription, and cannot be stopped from here.
- **Never call `orchestrate.py` for a recurring run.** `--monitor` is the only
  entrypoint that reads `poll_interval_hours` from the user's config.
  `orchestrate.py --now` takes `--interval` with a **4-hour default** and never looks
  at their setting, so a user who chose 12 hours would silently get 4.

`--monitor` refuses to start if one is already running, so this is safe to run even if
Step 2 was ambiguous.

## Step 4 — verify, then report

Wait a few seconds and run `--status` again. Report **only what it returns**.

If it says running, tell the user, plainly:

- **How often** it sweeps — take the interval from the status output, not from memory.
- **That it stops when this session ends.** The single most important thing to say,
  because the natural assumption is that it keeps running. It is a session watcher, not
  a background service.
- **How to make it survive** closing Claude Code: the OS scheduler entry
  `/hireshire:setup` offers. If they did not take it, they can re-run setup.
- **That invoking this twice does nothing** — the second start exits rather than
  duplicating the sweep.

If auto-apply is enabled in their config, mention that each sweep will also run the
applier — and that `dry_run` decides whether anything is actually submitted.

If `--status` still says not running, **say so**. Do not report a sweep that is not
there. Read `<DATA>/logs/orchestration.log` for the reason, tell them what it says, and
offer the scheduled task from `/hireshire:setup` as the alternative that does not depend
on this session.

## While it runs

Each cycle emits one summary line: how many matches were found, the best score, and when
the next sweep is due. Relay those as they arrive; do not go looking for more detail
unless the user asks. `--status` answers "is it still going?" at any point.

Two log files, which are easy to confuse:

- `<DATA>/logs/orchestration.log` — the recurring loop. Start here.
- `<DATA>/logs/orchestrate.log` — the engine's own log for one sweep, for detail.
