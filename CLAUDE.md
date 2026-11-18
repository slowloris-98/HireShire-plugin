# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Note: `claude plugin validate` warns that a root CLAUDE.md is not loaded as plugin
> context. That is intended — this file is developer guidance for *this repo*, not
> content shipped to plugin users. User-facing instructions live in `skills/`.

## What this is

A **Claude Code plugin** that repackages the HireShire job-search pipeline so
non-technical users install it with two slash commands and never touch a YAML file
or a terminal.

- **Source repo**: `D:\Atreya\College\Projects\HireShire` — the original five-phase
  pipeline. **Read-only reference: make no changes there.** The engine here was
  *copied*, not forked or submoduled, and has since diverged.
- **Plugin `name` is `hireshire`** — it is the invocation namespace (`/hireshire:setup`).
- `PLAN.md` records the build plan and the decisions behind it.

## Commands

```bash
# Plugin
claude plugin validate . --strict     # before every release
claude --plugin-dir .                 # load this repo as a plugin locally
pytest                                # 899 tests, no network, no model weights
pytest tests/test_budget.py           # single file
pytest tests/test_budget.py::test_only_jobs_reaching_the_cutoff_are_judged
sh scripts/hireshire.sh --paths       # where ROOT and DATA resolve to, right now
sh scripts/hireshire.sh --stop        # kill the sweep's process TREE, clear the pid file
sh scripts/hireshire.sh --approve     # PreToolUse guard; hook payload on stdin

# Engine, from a checkout (falls back to ./data when the plugin env vars are unset)
python scraper.py                     # sweep the enabled boards
python matcher.py                     # gate → rerank → cutoff → score
python orchestrate.py --once          # both, writing a results CSV
python scripts/calibrate_cutoffs.py   # what rerank.min_score should be, from real runs
python scripts/discover_slugs.py      # new ATS slugs from Common Crawl; dry run, --write merges
python scripts/discover_all.py        # slugs + Eightfold + Oracle in turn; dry run, --write merges

# Engine, as the plugin runs it (re-execs into the venv in the data dir)
python scripts/run_engine.py orchestrate.py --once
python scripts/setup_cli.py set matcher --json '{"threshold": 75}'
```

## Where the detail lives

This file holds the rules. The *why* — what was tried, how it broke, what was
measured — lives in `docs/dev/`, moved there verbatim so nothing was lost. A nested
`CLAUDE.md` in each subsystem directory imports the matching file, so it loads when
you work there. **Before changing behaviour a rule below names, read its file.**

| File | Covers |
|---|---|
| `docs/dev/paths-and-state.md` | ROOT/DATA/WORKSPACE, day layout, `run_dir_for`/`make_run_dir` |
| `docs/dev/boards-and-locations.md` | slug lists, discovery tools, Gem/Oracle/Eightfold, direct portals, location gate |
| `docs/dev/runtime.md` | sweep lifetime, `--stop`, launcher, PyTorch/uv, Windows child consoles |
| `docs/dev/setup-and-skills.md` | config writer, setup keys, skill rules, approve guard, packaging |
| `docs/dev/funnel.md` | funnel stages, title keywords, cutoff, clustering, YoE gate, codex judge |
| `docs/dev/reports.md` | results CSV/JSON, dashboards, sections, progress bars, mark-applied |
| `docs/dev/storage.md` | `postings` table, writers, schema v2 migration, VACUUM |
| `docs/dev/applier.md` | apply worker, deferral vs verdict, codex browser session, screenshots |

Keep this file short: add a one-line rule here and the reasoning in `docs/dev/`.

## Architecture in one screen

- **ROOT** (install dir) is replaced on every update — shipped, read-only content
  only. **DATA** (`~/.claude/plugins/data/hireshire-hireshire/`) survives — venv,
  SQLite DB, user config, logs. **WORKSPACE** is the user's folder (resume, and
  `hireshire_run_results/<YYYY-MM-DD>/<stamp>/`), recorded once by setup in
  `scraper.workspace_dir`; empty falls back to `DATA/results`.
- `hireshire/paths.py` is the only place paths are decided; nothing resolves against
  cwd. `hireshire/plugin_dirs.py` derives DATA from ROOT and **outranks
  `CLAUDE_PLUGIN_DATA`**, which differs per interface.
- Two engine phases (scrape, match) streamed over asyncio queues with one `None`
  sentinel each, sent in a `finally`; applying is a queue consumer that launches one
  CLI browser session per job. One SQLite DB (WAL); **one posting is one row** in
  `postings`, keyed `(board_token, job_id)`.
- The funnel, every stage a per-job decision; only the cutoff costs money:

```
location + age      free
exclude keywords    free                          funnel.py:55
bi-encoder          cheap, TITLE only             relevance.py — a recall net
detail hydration    only for DETAIL_SOURCES       detail_fetcher.py:20
cross-encoder 68m   full description → logit      rerank.py
cluster             one call per requisition      cluster.py — needs those logits
min_score cutoff    per job, per batch → LLM      matcher.py:_process_batch
years-of-experience free, regex, per cluster      experience.py — after the cutoff
top_k               a fuse on calls, not a gate   matcher.py:_CallBudget
```

- The engine writes the dashboards (`Dashboard_Lifetime.html`,
  `Dashboard_Day_<date>.html`, `Dashboard_<stamp>.html`) on a clock; skills only hand
  over paths. Nothing is published.

## Rules with teeth

Each of these reversed something that shipped and hurt users. Do not undo one
without reading its file in `docs/dev/` first.

**Verdicts and deferrals** (`funnel.md`, `applier.md`)
- A job is retired on a **verdict**, never on a deferral or error. Retryable:
  `llm_call_cap_reached`, launch failures, browser-unavailable sessions, per-company
  cap holds. Verdicts: `rerank_below_cutoff`, `yoe_below_requirement`,
  `duplicate_of_cluster`, `excluded`, `location_mismatch`, ambiguous apply endings.
- **No retry counters**, anywhere. The backlog window closing (`EXPIRED_STATUS`) is
  the only expiry; `apply_deferred_at` is a timestamp and must never become a count.

**Slugs and boards** (`boards-and-locations.md`)
- **No dead-slug skip list**, no seed-plus-delta scheme. A 404 is a deferral.
- Slug/tenant lists grow only via the `discover_*.py` tools, which only add.
- Do not drop Common Crawl's page-completeness check because the status was 200.
- A board that must reach existing installs needs a `config._with_new_defaults` rule.
- Location gate normalises the **job** (`filter_haystack`), never the terms; never a
  raw substring test. `filter_haystack` is not `normalize_location`.
- Direct portals are plain HTTP; no browser path. Portal location codes are a
  checked-in table; anything unresolvable widens, never narrows.
- Company counts live only in `README.md`'s first line and `docs/SPECS.md`'s Scale table.

**Funnel** (`funnel.md`)
- Title keywords match whole words via `title_filter.title_matches`; no substring,
  no stemming. Setup drafts bare single words, profession-checked, all spellings.
- `min_score` is a personal raw logit; tightening the bi-encoder saves no LLM calls.
- Cluster on `(board_token, description)`, **never titles**; cluster after rerank.
- YoE gate is a regex, highest open-ended minimum governs, gates only from below,
  runs after the cutoff. The search profile never reaches the scoring prompt.

**Storage** (`storage.md`)
- **No writer may be `INSERT OR REPLACE`.** Only `insert_jobs` inserts (as an
  `ON CONFLICT … DO UPDATE` that never names verdict columns); everything else is an
  `UPDATE`. `job_json` and `match_json` stay separate names.
- `scored_at IS NOT NULL` is what "has a verdict" means. A schema newer than
  `SCHEMA_VERSION` is refused; migration refuses to run under a live sweep.

**Reports** (`reports.md`)
- Refresh on a clock, never on funnel events; nothing in `reporting` may raise; the
  meta refresh is armed only while the `runs` row is absent.
- Tiles are a cumulative funnel, sections a partition — they do not reconcile, by design.
- A never-scored job renders a blank/em dash, never `0`. Day scope is an enumerated
  list of run ids; `run_ids=[]` must read zero (branch on `is not None`).

**Applier** (`applier.md`)
- One session per job, one at a time. No failover between `claude_code` and `codex`.
- Never answer bot/AI questions on a form; never fabricate beyond the user-chosen
  tool-name exception.
- Codex: tools under bare names (`tool_prefix` stays empty), MCP approval mode
  `approve`, sandbox `workspace-write`, a failed turn is `SUBMIT_UNCONFIRMED`.

**Runtime** (`runtime.md`)
- The sweep is **not session-scoped** and has no time limit. No `SessionEnd` hook,
  no `CLAUDE_PID`, no `--status`. `--stop` is the only deliberate stop.
- Interpreter discovery only in `scripts/hireshire.sh`. PyTorch via
  `uv --torch-backend auto`; never swap torch under a live sweep; fp32 everywhere.
- Every piped child gets its own console on Windows (`own_console_kwargs`); the two
  re-exec launchers must inherit stdio.

**Setup and skills** (`setup-and-skills.md`)
- Setup never shows YAML; `config_writer` quotes YAML-1.1-ambiguous strings and
  validates the PyYAML re-parse. Every setup branch writes its own keys, defaults included.
- Skills never state runtime facts they did not ask for, never write
  `${CLAUDE_PLUGIN_DATA}` (use `hireshire.sh --paths`), and run only fixed-shape
  commands the `approve.py` guard recognises. The guard stays silent on unknown
  input, stdlib-only, and never approves the bare-script form or any browser tool.
- Plugin MCP tools are `mcp__plugin_hireshire_playwright__*` (but `mcp__playwright__*`
  inside the applier's own session).
- `.claude/settings.json` stays gitignored. Explicit `version` in `plugin.json`.
  The clean-machine install is the real acceptance test.
