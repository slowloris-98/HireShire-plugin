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
pytest                                # 360 tests, no network, no model weights
pytest tests/test_budget.py           # single file
pytest tests/test_budget.py::test_only_jobs_reaching_the_cutoff_are_judged
sh scripts/hireshire.sh --paths       # where ROOT and DATA resolve to, right now
sh scripts/hireshire.sh --status      # is a recurring sweep running?
sh scripts/hireshire.sh --stop        # kill the sweep's process TREE, clear status
sh scripts/hireshire.sh --approve     # PreToolUse guard; hook payload on stdin

# Engine, from a checkout (falls back to ./data when the plugin env vars are unset)
python scraper.py                     # sweep the enabled boards
python matcher.py                     # gate → rerank → cutoff → score
python orchestrate.py --once          # both, writing a results CSV
python scripts/verify_bad_slugs.py --prune
python scripts/calibrate_cutoffs.py   # what rerank.min_score should be, from real runs

# Engine, as the plugin runs it (re-execs into the venv in the data dir)
python scripts/run_engine.py orchestrate.py --once
python scripts/setup_cli.py set matcher --json '{"threshold": 75}'
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

  **"Session-scoped" is now enforced, not assumed.** Being a child of the session is
  not enough on Windows: an orphan there is re-parented in silence — no process group,
  no SIGHUP — and a monitor outlived its session more than once, the last time with a
  shortlist in hand and auto-apply on. Two mechanisms end it and **neither is
  sufficient alone**. The `SessionEnd` hook runs `--stop` on an orderly exit, filtered
  on the payload's `reason` so a `/clear` does not kill a live sweep; it cannot fire
  when Claude Code is force-killed or crashes. So `run_orchestration.py`'s heartbeat
  also watches **`CLAUDE_PID`** — Claude Code publishes its own pid there — and exits
  within one interval once it is gone. Absence of that variable means **do not arm**,
  which is what keeps the watchdog inert for a plain terminal and the scheduled route;
  unknown must never mean kill.

  **Never source that pid from the shell.** `--monitor` used to export `$$` and
  `$PPID`, and it killed a healthy sweep 60 seconds in. Git Bash is MSYS and MSYS keeps
  its **own pid namespace** — `ps` reports PID 1684 for a shell Windows calls WINPID
  14072 — while `process_liveness.is_alive` asks Win32 `OpenProcess`, which knows only
  Windows pids. Two meaningless numbers read as dead on the first tick. Walking the
  tree instead is no better: the measured ancestry under the VS Code extension is
  `python → bash → bash → bash → claude.exe → Code.exe`, so no fixed-depth `getppid()`
  rule can be right. Coverage is therefore deliberately partial — a closed CLI and a
  crash, not a killed background Bash task, which waits for `SessionEnd` or `--stop`.
  `tests/test_process_liveness.py` pins a live pid reading as live, which is the
  assertion whose absence let the MSYS pid through.

  **Killing the leaf is enough, because the chain unwinds itself.** Every parent in the
  re-exec chain is blocked in `subprocess.run`, so each exits as soon as its child
  does — verified against a live four-process orphan, where a `taskkill /T` on the
  recorded leaf cleared all four. That is what makes a watchdog in the leaf sufficient,
  with no Job Objects and no `execv` rewrite.

  `--stop` remains the manual backstop and clears the status file either way: a stale
  document claiming "running" is the more harmful of the two failures. On POSIX it must
  signal the recorded pid **itself** — `pkill -P` only ever reaches children, and
  gating the SIGTERM on pkill having *failed* meant the sweeper survived precisely when
  it had a child, which is to say during the apply phase.
- **Setup never shows YAML.** `hireshire/config_writer.py` is a whitelisted,
  ruamel-based writer that preserves comments and CRLF and validates the patched
  document against the phase's pydantic model *before* writing.

### The funnel is the interesting part

Scoring every posting with an LLM is what makes a 15,000-employer sweep accurate,
and also what makes it expensive. The pipeline spends that budget deliberately:

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

**Every stage is a per-job decision, and that is the design.** Selection used to be a
global top-K over the pooled sweep, which meant no job could be judged until every
job had been seen — the queue architecture bought nothing for the part the user waits
on. A cutoff is a statement about one job against one profile, so the whole pipeline
now streams: scrape → gate → hydrate → cluster → rerank → judge, one employer at a
time. Applying stays a separate phase; see the note in `orchestrate.py`.

Five things follow that are easy to break:

- **Only the cutoff costs money.** Both gates before it run locally. Tightening the
  bi-encoder buys CPU seconds and skipped detail fetches, never LLM calls, and pays
  for them in recall at the *title-only* stage. This is the single most common wrong
  instinct about this funnel.
- **The bi-encoder threshold is deliberately low (0.30)** and does not transfer
  between users. Outside tech, titles are branded and generic (Account Manager,
  Client Partner, Growth Partner), so their cosines bunch into a narrow band and a
  threshold tuned on engineering titles passes everything or nothing. Note also that
  max-over-targets rises with the number of anchors, so a longer `targets` list
  loosens the gate further on its own.
- **`min_score` is a raw logit and is personal.** Not a probability, not comparable
  between users, and void the moment `rerank.model` changes. `scripts/calibrate_cutoffs.py`
  derives it from the user's own `matches` rows; the shipped 3.0 is a starting point
  borrowed from this project's own analysis corpus (where it admits ~61 jobs a sweep,
  inside `top_k`), not a value tuned to any particular user. It sits deliberately well
  above 0.0 — the models' own decision boundary, which shipped through 0.2.x and was
  permissive enough that the budget rather than the cutoff usually decided what got
  scored.
- **Clustering still works per batch, and this is load-bearing.** Postings group by
  `board_token` plus description, and the scraper emits one employer per queue item,
  so every member of a cluster is in the same batch by construction. That is what let
  top-K go without 31 copies of a requisition becoming 31 LLM calls. Clustering must
  run *after* the reranker: `cluster.group` anchors each cluster on its best-scoring
  member, so swapping the two lines would let scrape order pick the representative.
- **Greenhouse/Ashby/Lever ship the description in the list response**
  (`greenhouse.py:89`, `ashby.py:54`, `lever.py:59`). Only Workday, BambooHR and the
  direct portals need hydration. That is why full-text reranking is free on the
  default board set, and why the title-only gates exist at all.

The rerank cascade is gone. A 17m model used to read everything and the 68m re-read
the top `refine.depth`, which existed solely to make a global top-K affordable — and
cost a permanent hazard, since the two stages emitted incomparable logit scales that
any naive sort silently mixed. The 68m model now reads every candidate, spread across
the sweep rather than run in one block, and there is one scale. `rerank_score_wide`
survives as a **read-only** column: rows written before the collapse carry it, the
reports render them, and nothing writes it any more. The single-stage reranker that
preceded all of this failed silently for a whole run (correlation with the eventual
LLM score: **+0.16**), which is why `RerankConfig` documents its scale so heavily.

Note also why `max_doc_chars` is generous now: the old 1,200-char cap truncated
**41% of descriptions before their first requirements heading** (median heading
offset: character 1,094), so the cross-encoder was scoring company boilerplate.
Descriptions tokenise at ~5.06 chars/token and the longest measured was 2,693
tokens, so against an 8,192-token window the setting is a cost dial, not a limit.

### Two invariants with teeth

- **A job may be retired on a verdict, never on a deferral or an error**, and the two
  rerank drops sit on opposite sides of that line. `llm_call_cap_reached` is a
  deferral — the run ran out of calls, and the job may be the best thing in a quieter
  sweep — so it stays in `_RETRYABLE_SKIP_REASONS`. `rerank_below_cutoff` is a
  verdict and is deliberately absent: same profile, same model, same description
  means the same logit, so with `max_age_hours: 24` and a 4-hour poll, retrying it
  would re-run one deterministic computation ~6 times a day to reach the same answer,
  and it has nothing to win against. Getting this backwards in either direction is a
  real bug: one wastes the funnel, the other permanently discards a job that was only
  unlucky. `rerank_below_top_k` stays listed as retryable for rows written before the
  split.
- **The years-of-experience gate is a regex, and that is not a compromise.**
  `hireshire/funnel/experience.py` reads a stated minimum out of the description with
  no model at all. An encoder cannot do this — a bi-encoder pools to one vector and a
  cross-encoder to one logit, so neither has a span output, and pooled embeddings are
  bad at magnitude anyway ("2+ years" sits near "12+ years"). An LLM can, and
  `analysis/extraction_spike.py` measured Haiku doing it, but the cost was a wash:
  ~20 Sonnet-equivalents to save 18 judge calls. It clears the safety floor that
  licenses it: on sweep `2026-09-09T06-51-12Z` the best judge score among its
  casualties was 44 against a shortlist threshold of 65, and nothing shortlisted was
  killed (`analysis/results/yoe_gate.md`, whose section 1 predates the current
  aggregation — read the banner there).

  Five rules hold it together, and each reverses an instinct:

  - **The `+` is the requirement marker, not the word "experience".** The parser used
    to require experience language within a short window of the number, which threw
    away every posting that writes the domain instead — `7+ years owning financial
    planning`, `10+ years in software engineering`, `5+ years of production support`.
    An **open-ended minimum** (`5+ years`, `5 plus years`, `at least 5 years`,
    `minimum of 5 years`) is a requirement whatever follows it, and company prose
    almost never uses that form. Widening the word list instead was tried and
    rejected: a rule loose enough to admit those lines also read "For 20 years, Acme
    has been building homes" as a 20-year requirement. Three tiers, first non-empty
    wins — open-ended, then range (lower bound), then the old bare-count-plus-
    proximity path, which is retained because dropping it costs 7 of the 104
    reachable labels (91% → 85%) and takes missed requirements from 1 to 10.
  - **Highest open-ended minimum governs.** A posting listing `8+ / 12+ / 3+` compares
    against **12**. This reverses the original lowest-governs rule and the reversal is
    deliberate: a candidate who cannot clear the highest bar a posting names is not
    getting the job, and the lowest reading sent 6 of every 50 paid calls to postings
    needing 5-8 years. The label corpus in `analysis/cache/extraction.json` encodes
    lowest-governs, so the parser now reads *higher* than a label occasionally by
    design — do not "fix" that. What licenses it is the safety floor, not agreement:
    on sweep `2026-09-09T06-51-12Z` the change dropped 13 of 50 paid calls (26%), the
    best judge score among them was 44 against a shortlist threshold of 65, and
    nothing shortlisted was touched. Agreement on the labels rose 81% → 91% anyway.
    Note `analysis/yoe_gate_eval.py` section 1 therefore measures policy divergence,
    not error, and its sample is thin — see the caveats in its docstring.
  - **"Preferred" is treated exactly like "required"**, which is where the spike's
    prompt differed and why it returned null on most preference-phrased postings.
  - **It gates only from below.** `5-10 years` is read as *at least 5*. Ranges are
    matched *before* the open-ended tier and their spans excluded from it, so the
    upper bound cannot look like a second requirement — without that ordering
    `3 to 7+ years` would read as an open-ended 7 rather than a range from 3.
    Nothing is dropped for being over-qualified.
  - **It runs after the `min_score` cutoff, not before it.** Both are LLM-free, so
    the ordering buys two other things: `stages["above_cutoff"]` keeps meaning what
    the matching report says it means, and a wrong `candidate_years` can only touch
    jobs that were about to cost a call. YoE drops are counted into `above_cutoff`
    for the same reason cap drops are.

  `yoe_below_requirement` is a **verdict** and stays out of `_RETRYABLE_SKIP_REASONS`
  — same description, same `candidate_years`, same answer. This reverses
  `analysis/results/extraction_prefilter.md`, which required extraction drops to be
  retryable; that was written about an LLM extractor, where a misparse is transient.
  `yoe_required` is recorded on every reranked row even when the gate is switched
  off, so "what would enabling this cost me" is answerable from a real run.

- **The search profile never reaches the scoring prompt.** It states transferable
  and inferred framing ("React → component-based UI development"). It is the
  reranker's query only. A judge reading it would credit the candidate for skills
  the resume does not evidence. Keep it out of `projects_path`, which *is*
  concatenated into the prompt at `scorer.py`.

- **Duplicate requisitions are grouped, never dropped.** `cluster.py` keys on
  `(board_token, description)` — **titles are deliberately never compared.** One
  representative is scored and the verdict is copied to every sibling, so all 31
  copies keep their own location and link in the all-jobs export. Siblings carry
  `duplicate_of_cluster`, which must stay **out** of `_RETRYABLE_SKIP_REASONS`: they
  have been judged, just by proxy. Members of a cluster that misses the cut inherit
  the representative's drop reason, so their retryability follows the rule above.

  This reverses the original title-based key, and the reversal was forced by data —
  do not "restore" it. An audit of 192,700 real postings found that of 2,578 clusters
  formed by stripping a trailing parenthetical, **1,739 grouped postings whose
  descriptions differed**. SpaceX qualifies titles by programme, so one cluster held
  seven unrelated jobs (`Automation & Controls Engineer` for Facilities, Raptor,
  Starlink, Starship…); the same strip collapsed shifts, employment types and ladder
  levels (`(L1)` with `(L3)`). Six of seven then inherited a non-retryable reason and
  were retired permanently on a verdict about a different job.

  Exact description equality is too strict — employers interpolate pay bands and
  addresses per market — so `dedupe.max_word_diff` (default 10) allows a small drift.
  It is a **multiset symmetric difference, where a substitution costs 2**; every
  calibration is in those units. Two costs are accepted and should not be "fixed"
  without new data: templated employers over-merge (sweetgreen ships identical text
  for `Assistant Coach` and `Assistant Restaurant Manager`), and localised employers
  fragment (~+900 to +1,010 LLM calls per sweep, which land on the retryable cap).

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

### The reports are written by the engine and published by the skills

`hireshire/reporting/` renders two HTML pages per sweep: `<stamp>_matching.html`
in the run folder (the LLM's four rationales per scored job, then every job that
was never scored) and `dashboard.html` at the results root (every run the install
has done). Both exist because the reasoning had nowhere to go — it was written to
`matches.raw_json` and rendered nowhere, so an empty shortlist was indistinguishable
from a broken threshold.

**The engine writes them; a skill only publishes them.** That is what makes them
appear on unattended monitor sweeps where no agent turn exists, costs no tokens,
and keeps the skills reporting numbers they were handed — the same rule as
`--status` and `--paths`.

Four consequences that should not be re-derived:

- **Two envelopes, and mixing them breaks the page.** The Artifact tool wraps what
  it publishes in its own `<!doctype html>…<head></head><body>`, so `matching.py`
  emits **body content only**. `dashboard.py` is local-only and emits a complete
  document. The matching report therefore renders in quirks mode when opened from
  disk, which is why the shared CSS sets `box-sizing` explicitly and avoids
  percentage heights.
- **`matching.TITLE` is stable across runs and must stay that way.** The skills
  find the existing artifact by that title (`Artifact action:"list"`) and
  republish to its URL, which is the entire mechanism behind one rolling link. Put
  the run date in the title and every sweep creates a new artifact.
- **Nothing may raise.** `reporting.refresh` swallows everything and the writers
  return `None` on failure, because it is called from the pipeline's own progress
  callbacks — the same trade `write_all_jobs_csv` documents.
- **The dashboard's meta refresh is armed only while the pipeline's `runs` row is
  absent**, so the final refresh must run *after* `finalise_run`. Refreshing before
  it leaves a finished run reloading itself forever.

**A third page, `overview.py`, is the minimal one, and it ships at two scopes.**
`overview.html` at the results root covers every sweep the install has done;
`<stamp>_overview.html` in a run folder covers that sweep and adds how long it took
and what it cost. Both are complete local documents. Four numbers, two `<details>`
accordions (applied, scored-but-not-applied, and the never-scored tail) under the
dashboard's own `HireShire` / `Control room` header. It explains nothing: past one
line telling the reader the sections open, the judge's rationales inside an opened job
are the only sentences on it. **Both scopes are the same markup fed different data**,
and the two extra tiles are the single deliberate exception — how long it took and
what it cost are facts about a sweep, not about an install. It does not replace the
other two pages, which stay for comparison.

Four things about it that are easy to get wrong:

- **Collapsing by default is what makes the meta refresh expensive.** A live page
  reloads every `REFRESH_S`, and a reload resets every `<details>` — so it used to
  shut the job whose rationale the reader was halfway through, every 15 seconds. Every
  `<details>` therefore carries a stable id (`acc:*`, `j:<job_id>`) and `_STATE_SCRIPT`
  restores the open set from `sessionStorage`, guarded, because a `file://` origin can
  be opaque enough that touching storage throws. Session, not local: reopening the
  file tomorrow should give the resting state. Note the tail's rows are script-built
  and fill while its accordion is still shut, so opening it is instant and the scroll
  listener cannot misfire — a hidden box is never scrolled.

- **A cluster sibling is not identified by `skip_reason`.** Siblings inherit the
  *representative's* reason, so only the lucky ones say `duplicate_of_cluster` and a
  cluster whose representative hit an API error puts `backend_unavailable` on all of
  them. `Database._sibling_sql` therefore reads `cluster_representative` out of
  `raw_json` — via `json_extract`, probed once at connect because JSON1 was opt-in
  before SQLite 3.38 and the interpreter is whatever the launcher found. Testing the
  reason instead dropped six judged jobs into the never-scored table on real data.
  Note `jobs` has a `raw_json` column too, so the predicate takes a table alias.
- **`_judged_sql` is a SQL mirror of `data._never_scored` and the two must agree.**
  The Python one cannot be used across runs (it needs `raw_json` parsed per row) and
  the SQL one cannot be dropped (the lifetime page groups the whole `matches` table).
  `tests/test_overview.py` pins them together against a fixture holding both kinds of
  sibling.
- **"Judged by proxy" is not the same as "has a verdict".** A sibling of a *failed*
  representative inherits a placeholder `relevance_score` of 0, and the page renders
  an em dash for it rather than that 0 — same rule as the all-jobs CSV's blank
  `llm_score`, and the reason `_job_entry` looks at `skip_reason` rather than trusting
  the score.

The lifetime page carries its own throttle (`LIFETIME_INTERVAL_S`, 60 s) because its
queries group a table that has no `run_id` filter to narrow them; everything else in
`refresh` is indexed on `run_id` and stays cheap however long the user has been at it.

All three tables now fill continuously — `run_companies`, `jobs` and `matches` — because
selection is a per-job cutoff and each employer's batch is judged as it arrives. Both
pages' copy was rewritten for that; it used to explain that nothing could be scored
until the sentinel, which was true under top-K and is now a lie the reader would catch.

The per-stage counts (`gated → reranked → above cutoff → judged`) exist for a failure
mode the cutoff introduced: under a ranking, something was always scored, so an empty
shortlist could only mean weak jobs. Under a cutoff it can equally mean `min_score` is
wrong for this resume, and the two are indistinguishable without the counts. A large
"reached the reranker" with zero above the cutoff is the tell.

### Layer 2 — the engine

Two phases, each independent: own entrypoint, own `hireshire/<phase>/` subpackage,
own `config/<phase>.yaml`. All tabular data lives in one SQLite DB (WAL); every
phase writes rows keyed by a shared `run_id`.

Applying is **not** a third engine phase, despite having `config/applier.yaml` and a
`hireshire/applier/` package. That package is only `config.py` and `store.py` — there
is no `main()` and nothing for `orchestrate.py` to wire a queue to, because the work
is driving a browser, which the `apply` skill does through Playwright MCP. The engine
launches that skill as a `claude -p` subprocess (`_launch_skill`) and reads the rows it
records. Anything needing a browser belongs on the skill side of that line. `orchestrate.py` wires them over
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

- **Board defaults.** Workday and BambooHR default **off**, and they are the two
  biggest lists: 24,200 companies held back against 15,868 swept (greenhouse 8,333,
  lever 4,369, ashby 3,163, direct 3), out of 40,068 shipped. The README leads with
  40,000+ but must state plainly that the default sweep is ~15,868. Setup presents it
  as a time trade-off — and **no specific multiplier has been measured yet**, so say
  "considerably longer", not "3x". These counts come from `config/*_companies.json`
  and grow between releases; re-derive them rather than copying this paragraph.
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
- **Every command a skill runs must be a fixed shape, because permission rules match
  the exact command string.** Setup used to run its Python by writing a heredoc to a
  temp file, so no two calls ever matched, no allowlist rule could cover them, and a
  first-time user approved ~15 dialogs before seeing a job. `scripts/setup_cli.py`
  exists to make each action one stable argv; `scripts/approve.py` is a `PreToolUse`
  hook that recognises those shapes and returns `permissionDecision: "allow"`, which
  is the supported way for a plugin to stop asking for its own plumbing.

  Three constraints on that guard, which is now a security boundary — whatever it
  approves runs with no prompt, ever:

  - **Silence is the default.** Unrecognised input produces no output and the user is
    asked exactly as before, so a bug in the guard costs friction, not authority.
  - **It never approves the launcher's bare `<script.py>` form**, which runs an
    arbitrary file. That is why `setup_cli.py` had to come first: nothing legitimate
    needs the escape hatch any more.
  - **It must stay stdlib-only and pre-venv**, like `bootstrap.py` — the first command
    it approves is the install that creates the venv.

  `approve.sh` wraps it with a substring pre-filter so unrelated Bash calls do not pay
  for the interpreter probe. Note that Windows needs three path spellings compared
  (`/d/...` from Git Bash, `C:/...`, `C:\...`) or the guard silently never matches.
  On the `apply` side only `browser_navigate`, `browser_snapshot` and
  `browser_take_screenshot` are approved: the prompt on a click or an upload is the
  last checkpoint before a real application reaches an employer. That became load-
  bearing when `dry_run` was removed — `enable_applier` and `exclude_companies` are
  now the only other things in the way, so this set must not be widened. Note the
  monitor's own apply phase runs `claude -p --permission-mode auto` and therefore
  bypasses it entirely: unattended auto-apply has no human checkpoint by design.
- **`userConfig` is not used** for anything load-bearing — its enable-time prompt
  has open bugs. The `setup` skill is the source of truth.
- **Set an explicit `version` in `plugin.json`.** Omitting it pushes every commit at
  users. Semver + `CHANGELOG.md`.
- **The clean-machine test is the real acceptance test**: fresh user dir,
  marketplace add → install → setup → find-jobs. Anything needing a terminal, a
  `git clone`, or a YAML file is a bug.
