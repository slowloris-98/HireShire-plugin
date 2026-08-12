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
pytest                                # 80 tests, no network, no model weights
pytest tests/test_budget.py           # single file
pytest tests/test_budget.py::test_top_k_keeps_the_highest_scoring_jobs

# Engine, from a checkout (falls back to ./data when the plugin env vars are unset)
python scraper.py                     # sweep the enabled boards
python matcher.py                     # gate → rerank → top-K → score
python orchestrate.py --once          # both, writing a results CSV
python scripts/verify_bad_slugs.py --prune

# Engine, as the plugin runs it (re-execs into the venv in the data dir)
python scripts/run_engine.py orchestrate.py --once
```

## Architecture

### The ROOT/DATA/WORKSPACE split governs where every file goes

`${CLAUDE_PLUGIN_ROOT}` is the install dir and is **replaced wholesale on every
plugin update** — shipped, read-only content only: engine code, default YAMLs,
company slug lists, the curated bad-slug seed. `${CLAUDE_PLUGIN_DATA}`
(`~/.claude/plugins/data/hireshire-hireshire/`) **survives updates** — venv, SQLite
DB, the user's config, generated profile, logs.

**Putting mutable state in ROOT loses it on the next update.** `hireshire/paths.py`
is the single place this is decided; nothing else may resolve a path against the
working directory, because a plugin's cwd is whatever project the user is in.
`paths.resolve_data()` passes absolute paths through (that is how the user's resume,
which lives outside the plugin, is addressed) and anchors relative ones under DATA.

The third root belongs to the user, not the plugin. **WORKSPACE** is the folder they
made for their job search — resume in `resume/original/`, one directory per run in
`hireshire_run_results/`. Its absolute path is captured **once** by
`/hireshire:setup` into `scraper.workspace_dir`; `paths.results_root()` is the only
reader. This does not weaken the cwd rule above, it is what makes obeying it
possible: the *skill* knows the working directory and records it, the engine only
ever reads config, so a session launched from another folder still writes to the
folder the user chose. `hireshire/workspace.py` owns creating it and copying the
resume in, and refuses a workspace inside ROOT or DATA. Empty `workspace_dir` falls
back to `DATA/results`, which is where installs predating the setting keep writing —
so every statement about the results path needs that clause.

Consequences already worked out, which should not be re-derived:

- **Seed-plus-delta slug lists.** `bad_slugs.json` is mutated at runtime *and*
  shipped curated. The seed sits in ROOT; `user_bad_slugs.json` and
  `user_recovered_slugs.json` in DATA. Effective set =
  `seed ∪ user_bad − user_recovered`, so a release can add dead slugs without
  erasing local learning, and `verify_bad_slugs.py --prune` writes recoveries as a
  delta rather than editing a file that is about to be replaced.
- **Monitors are session-scoped, not daemons.** They stop when the session ends and
  **cannot reference `${user_config.*}`** — Claude Code rejects the monitor rather
  than substituting — so `scripts/run_orchestration.py` reads `poll_interval_hours`
  out of the user's config itself. Every stdout line becomes a notification, so it
  emits exactly one summary line per cycle and logs everything else to a file.
- **Setup never shows YAML.** `hireshire/config_writer.py` is a whitelisted,
  ruamel-based writer that preserves comments and CRLF and validates the patched
  document against the phase's pydantic model *before* writing.

### The funnel is the interesting part

Scoring every posting with an LLM is what makes a 10,000-employer sweep accurate,
and also what makes it expensive. The pipeline spends that budget deliberately:

```
location + age      free
exclude keywords    free                          funnel.py:54
bi-encoder          cheap, TITLE only             relevance.py — a recall net
detail hydration    only for DETAIL_SOURCES       detail_fetcher.py:20
cross-encoder       full description vs profile   rerank.py
top-K               user-set budget → LLM         matcher.py:_spend_budget
```

Three things follow from this that are easy to break:

- **The bi-encoder threshold is deliberately low (0.25).** It is a recall net, not
  a verdict. Raising it discards exactly the differently-worded jobs the reranker
  exists to catch. Note also that max-over-targets rises with the number of anchors,
  so a longer `targets` list loosens the gate further on its own.
- **Top-K is global, so it cannot be computed per batch.** A queue batch is one
  employer's postings; ranking within it would compare a company against itself.
  Survivors pool for the whole sweep and the budget is spent once the sentinel
  arrives. This defers scoring to the end of the run, which costs almost nothing —
  the sweep is rate-limit-bound at ~20 min, scoring `top_k` jobs is ~1 min.
- **Greenhouse/Ashby/Lever ship the description in the list response**
  (`greenhouse.py:89`, `ashby.py:54`, `lever.py:59`). Only Workday, BambooHR and the
  direct portals need hydration. That is why full-text reranking is free on the
  default board set, and why the title-only gates exist at all.

### Two invariants with teeth

- **Budget drops must not be marked seen.** `matcher.py` writes every result into
  `seen_jobs` except `_RETRYABLE_SKIP_REASONS`. A job that misses the cut in one
  crowded sweep has to stay eligible — `max_age_hours: 24` with a 4-hour poll means
  it resurfaces ~6 times, and it may win later against weaker competition. Marking
  it seen retires it permanently on the strength of one run.
- **The search profile never reaches the scoring prompt.** It states transferable
  and inferred framing ("React → component-based UI development"). It is the
  reranker's query only. A judge reading it would credit the candidate for skills
  the resume does not evidence. Keep it out of `projects_path`, which *is*
  concatenated into the prompt at `scorer.py`.

Note that `MatchStore.finalise` records only summary stats — individual rows reach
the `matches` table via `append_result`. Budget drops are appended explicitly so the
user can see what the budget cost; title-gate rejections deliberately are not, since
there can be tens of thousands per run.

### Layer 2 — the engine

Two phases, each independent: own entrypoint, own `hireshire/<phase>/` subpackage,
own `config/<phase>.yaml`. All tabular data lives in one SQLite DB (WAL); every
phase writes rows keyed by a shared `run_id`. `orchestrate.py` wires them over
asyncio queues with exactly one `None` sentinel per queue, always sent in a
`finally`:

```
scraper.main(out_queue=q1) → q1[(board_token, list[Job])] → matcher.main(q1→q2)
  → q2[(MatchResult, Job)] → _collect_results → q3 → pipeline_results table
  → <workspace>/hireshire_run_results/<stamp>/<stamp>_results.{csv,json}
```

Each `main()` takes optional `in_queue` / `out_queue` / `quiet`. `quiet=True`
suppresses Rich in favour of `logging` — required under the monitor.

## Things that are easy to get wrong

- **Board defaults.** Workday and BambooHR default **off**: 9,974 companies vs
  24,754. The README leads with 24,754 but must state plainly that the default
  sweep is ~10,000. Setup presents it as a time trade-off — and **no specific
  multiplier has been measured yet**, so say "considerably longer", not "3x".
- **Interpreter discovery lives in exactly one place: `scripts/hireshire.sh`.**
  Two traps make this worth centralising. macOS has no bare `python` — Apple
  removed `/usr/bin/python` in 12.3 and Homebrew installs `python3` only. And
  Windows ships a Microsoft Store App Execution Alias named `python3.exe` that
  *exists on PATH*, prints an ad and exits 49, so `command -v python3` selects the
  broken one while the real `python` sits beside it. The launcher therefore
  **runs** each candidate and keeps the first reporting Python ≥ 3.10. Hooks,
  monitors and all three shell-using skills go through it; nothing else may name
  an interpreter. Windows needs Git Bash so `sh` exists.
- Downstream of the launcher, `scripts/run_engine.py` re-execs into the venv and
  addresses its interpreter by absolute path — hook exec form cannot spawn the
  `.cmd`/`.bat` shims Windows installs.
- **Plugin-bundled MCP tools are namespaced** `mcp__plugin_hireshire_playwright__*`,
  not `mcp__playwright__*`. A rule written against the bare server key never fires.
- **`userConfig` is not used** for anything load-bearing — its enable-time prompt
  has open bugs. The `setup` skill is the source of truth.
- **Set an explicit `version` in `plugin.json`.** Omitting it pushes every commit at
  users. Semver + `CHANGELOG.md`.
- **The clean-machine test is the real acceptance test**: fresh user dir,
  marketplace add → install → setup → find-jobs. Anything needing a terminal, a
  `git clone`, or a YAML file is a bug.
