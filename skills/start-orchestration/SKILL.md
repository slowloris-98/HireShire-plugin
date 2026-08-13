---
name: start-orchestration
description: Keep sweeping the job boards on a schedule for as long as this session stays open, reporting one summary per cycle.
---

# Start orchestration

Invoking this skill starts the `orchestration` monitor, which re-runs the full
pipeline every `poll_interval_hours` (set at setup, default 4) and reports one
line per completed sweep.

## What to do

The monitor starts automatically when this skill is invoked — you do not need to
launch anything. Confirm it started, then tell the user, plainly:

- **How often** it will sweep, read from `<DATA>/config/scraper.yaml`
  (`poll_interval_hours`). Get `<DATA>` from the launcher — never guess it, and
  never substitute the CLAUDE_PLUGIN_DATA placeholder, which resolves to a
  different directory in the Claude desktop app than in the terminal or the VS Code
  extension:

  ```bash
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
  ```
- **That it stops when this session ends.** This is the single most important
  thing to say, because the natural assumption is that it keeps running. It is a
  session watcher, not a background service.
- **How to make it survive** closing Claude Code: the OS scheduler entry that
  `/hireshire:setup` offers. If they did not take it, they can re-run setup.
- **That invoking this twice does nothing.** The monitor is named, so a second
  invocation will not start a duplicate sweep.

If auto-apply is enabled in their config, mention that each sweep will also run
the applier — and that `dry_run` decides whether anything is actually submitted.

## While it runs

Each cycle emits one summary line: how many matches were found, the best score,
and when the next sweep is due. Relay those as they arrive; do not go looking for
more detail unless the user asks.

Failures print a single line pointing at `<DATA>/logs/orchestration.log`. Read that
file if they want to know what went wrong.
