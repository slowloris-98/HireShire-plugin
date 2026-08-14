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
pytest                                # 158 tests, no network, no model weights
pytest tests/test_budget.py           # single file
pytest tests/test_budget.py::test_top_k_keeps_the_highest_scoring_jobs
sh scripts/hireshire.sh --paths       # where ROOT and DATA resolve to, right now
sh scripts/hireshire.sh --status      # is a recurring sweep running?

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

**ROOT** is the install dir and is **replaced wholesale on every plugin update** —
shipped, read-only content only: engine code, default YAMLs, company slug lists, the
curated bad-slug seed. **DATA** (`~/.claude/plugins/data/hireshire-hireshire/`)
**survives updates** — venv, SQLite DB, the user's config, generated profile, logs.

**Putting mutable state in ROOT loses it on the next update.** `hireshire/paths.py`
is the single place this is decided; nothing else may resolve a path against the
working directory, because a plugin's cwd is whatever project the user is in.
`paths.resolve_data()` passes absolute paths through (that is how the user's resume,
which lives outside the plugin, is addressed) and anchors relative ones under DATA.

**DATA is derived from ROOT, and the derivation outranks `CLAUDE_PLUGIN_DATA`** —
`hireshire/plugin_dirs.py` owns this. Claude Code resolves that variable from the
plugin *identifier*, which differs by interface: `cli` and `claude-vscode` report
`hireshire@hireshire`, `claude-desktop` reports an inline source and yields
`hireshire-inline`. Following it splits one install's state across two directories.
ROOT does not have the problem, and the derivation drops the version segment, which
is what carries the user's database across an update.

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
- **The recurring sweep is session-scoped, and the skill must verify it.** 0.2.4
  dropped the plugin monitor that used to start it: monitors are experimental and are
  skipped on hosts where the Monitor tool is unavailable, so the skill's claim that
  sweeps had begun was sometimes false with nothing to catch it. `start-orchestration`
  now launches `hireshire.sh --monitor` as a background task and confirms with
  `--status` before saying anything. Three rules survive from that design and still
  bind: `scripts/run_orchestration.py` reads `poll_interval_hours` out of the user's
  config itself (`orchestrate.py --interval` defaults to 4 and never looks); every
  stdout line reaches the agent, so it emits one summary line per cycle and logs the
  rest to a file; and nothing may detach the process, because the user is told it
  stops with the session. `hireshire/orchestration_status.py` is the single source of
  truth for "is it running", by heartbeat freshness rather than PID probing — `os.kill(pid, 0)`
  is not portable to Windows and a recycled PID reads as alive.
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
cross-encoder       full description, 2 stages    rerank.py
  wide   17m        every candidate
  refine 68m        top `refine.depth` only
cluster             one call per requisition      cluster.py
top-K               user-set budget → LLM         matcher.py:_spend_budget
```

Four things follow from this that are easy to break:

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
- **The two rerank stages emit incomparable logit scales.** Never sort, average or
  threshold `rerank_score_wide` against `rerank_score` — they come from different
  models. `RerankScores.sort_key` is the only sanctioned ordering: refined always
  outranks unrefined, ties break within one model's scale. This is sound only
  because `refine.depth >= top_k`, which `FunnelConfig` enforces at load. The
  original single-stage reranker failed silently for a whole run (correlation with
  the eventual LLM score: **+0.16**), so a mis-ranking here is not hypothetical —
  it is the exact bug this design replaced, and it leaves no error behind.

Note also why `max_doc_chars` is generous now: the old 1,200-char cap truncated
**41% of descriptions before their first requirements heading** (median heading
offset: character 1,094), so the cross-encoder was scoring company boilerplate.
Descriptions tokenise at ~5.06 chars/token and the longest measured was 2,693
tokens, so against an 8,192-token window the setting is a cost dial, not a limit.

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

- **Duplicate requisitions are grouped, never dropped.** `cluster.py` keys on
  `(board_token, normalised_title)` — descriptions are deliberately never compared,
  because similarity thresholds are unauditable after the fact. One representative
  is scored and the verdict is copied to every sibling, so all 31 copies keep their
  own location and link in the all-jobs export. Siblings carry
  `duplicate_of_cluster`, which must stay **out** of `_RETRYABLE_SKIP_REASONS`: they
  have been judged, just by proxy. Members of a *losing* cluster keep
  `rerank_below_top_k` and stay retryable. The normaliser is biased toward doing
  nothing — over-merging costs the user a match they never learn existed, while
  under-merging costs only a budget slot.

Note that `MatchStore.finalise` records only summary stats — individual rows reach
the `matches` table via `append_result`. Budget drops and cluster siblings are
appended explicitly so the user can see what the budget cost; title-gate rejections
deliberately are not, since there can be tens of thousands per run.

Two result files come out of a run, and they are not interchangeable.
`<stamp>_results.csv`/`.json` is the shortlist the apply skill consumes via
`last_run.json`'s `json` pointer — **one row per cluster**, because 31 siblings
would otherwise become 31 applications. `<stamp>_results_all_jobs.csv`
(`results_export.py`) is the diagnostic: every row in `matches`, with the four
scores in four separate columns. A budget drop renders a **blank** `llm_score`, not
the `0` that `filtered_result` puts in the model — printing that zero reads as a
verdict and is what hid the broken reranker for an entire run.

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
- **A skill must not state runtime facts it has not asked for.** Both live failures of
  this kind cost a user real trust: one skill announced a running sweep that did not
  exist, another wrote the search profile to a directory it had guessed. The launcher
  answers both questions — `--status` and `--paths` — and the skills are required to
  ask. `tests/test_plugin_shell.py` greps for the regressions.
- **A skill may write `${CLAUDE_PLUGIN_ROOT}`; it must never write
  `${CLAUDE_PLUGIN_DATA}`.** Claude Code expands both inside skill content, but the
  data one resolves differently per interface (see the ROOT/DATA split above), so a
  skill that substitutes it writes where the engine never reads — and nothing fails
  loudly. Skills call `hireshire.sh --paths`, which prints `ROOT=` and `DATA=` and
  works before the venv exists. `tests/test_plugin_shell.py` fails the build if the
  placeholder reappears. This is the same "solve it in one place" argument as
  interpreter discovery, applied to directory discovery.
- **`userConfig` is not used** for anything load-bearing — its enable-time prompt
  has open bugs. The `setup` skill is the source of truth.
- **Set an explicit `version` in `plugin.json`.** Omitting it pushes every commit at
  users. Semver + `CHANGELOG.md`.
- **The clean-machine test is the real acceptance test**: fresh user dir,
  marketplace add → install → setup → find-jobs. Anything needing a terminal, a
  `git clone`, or a YAML file is a bug.
