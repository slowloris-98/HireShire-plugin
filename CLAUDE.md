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
pytest                                # 772 tests, no network, no model weights
pytest tests/test_budget.py           # single file
pytest tests/test_budget.py::test_only_jobs_reaching_the_cutoff_are_judged
sh scripts/hireshire.sh --paths       # where ROOT and DATA resolve to, right now
sh scripts/hireshire.sh --stop        # kill the sweep's process TREE, clear the pid file
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
- **The recurring sweep is NOT session-scoped, and nothing may make it so again.**
  `scripts/run_orchestration.py` is an ordinary sleep/sweep loop. `--monitor` runs it
  recurring, `--sweep` runs one cycle (`--once`) and is what the OS scheduler entry
  uses — one program, so a fix to either reaches both. `/hireshire:start-orchestration`
  is the only sweep command; the one-shot `/hireshire:find-jobs` and the manual
  `/hireshire:apply` were removed in 0.11.0, so do not bring either back. Three
  rules survive from the old design: it reads `poll_interval_hours` from the user's
  config itself (`orchestrate.py --interval` defaults to 4 and never looks); every
  stdout line reaches the agent, so it emits one summary line per cycle and logs the
  rest to a file; and nothing may detach the process.

  **Two attempts to tie it to a session both destroyed users' work, and the second is
  why the idea is abandoned rather than refined.** The first passed `$$`/`$PPID` from
  Git Bash to a watchdog — MSYS keeps its own pid namespace (`ps` reports 1684 for a
  shell Windows calls WINPID 14072) while `is_alive` asks Win32 `OpenProcess`, so two
  meaningless numbers read as dead and killed a healthy sweep 60 seconds in. The second
  read `CLAUDE_PID` instead, paired with a `SessionEnd` hook that stopped whatever the
  status file named.

  That hook fired for **every** Claude Code session ending anywhere on the machine —
  including the short-lived `claude -p` sessions the scorer spawns per scoring call. An
  ownership check was added and did not save it: on a fresh Windows install `CLAUDE_PID`
  was simply **absent**, so the sweeper recorded no owner, the check's null-owner
  fallback answered "stop it" for every ending session, and the sweep manufactured its
  own killers. Observed: `session_pid: null`, `heartbeat - started_at` of **60.34 s**
  (one beat, then nothing), exit code 1, no traceback, no `ERROR` line — because
  `taskkill /T /F` unwinds nothing. It hit `--once` too, so the one-shot and scheduled
  routes died the same way.

  The lesson is not "find a better session signal". It is that a sweep must not act on
  host-specific identity it cannot verify, because the absence of that identity is
  indistinguishable from a legitimate reap and the failure direction is destroying work.
  Both layers were first replaced by a 24-hour runtime bound (`_MAX_RUNTIME_S`). That
  bound is gone too, because it stopped sweeps users wanted running. A recurring sweep
  now runs until `--stop` or until its process is killed, so **unattended auto-apply has
  no time limit, by design**. Do not bring back a session tie to bound it, and
  `tests/test_sweep_lifetime.py` fails if the bound reappears. `--stop` is the only
  deliberate stop, and surviving a closed terminal *on purpose* is the OS scheduler
  entry `/hireshire:setup` offers.

  Consequences that should not be re-derived:

  - **There is no `--status`, and the skill must not invent one.** `hireshire/sweep_pid.py`
    records one integer; `hireshire/orchestration_status.py` — heartbeat, `STALE_AFTER_S`,
    the liveness veto, `describe()` — is gone with the teardown it served. The user
    watches a sweep through the overview page or their shell task.
  - **`/hireshire:start-orchestration` must not claim the sweep stops with the session.**
    It no longer does. It ends on `--stop` or on the shell task being killed, and it must
    not promise a time limit either. Saying otherwise is the same class of failure as announcing a sweep that was
    never running, and `tests/test_plugin_shell.py` pins the skill's wording.
  - **`process_liveness.is_alive` survives, for one caller only:** the guard that refuses
    to start a second writer onto the same SQLite database. It was never the bug — it
    answers correctly about a pid the plugin recorded **about itself**. Every failure
    here came from feeding it an identity that had been guessed at. A recorded pid that
    is definitively gone must not block a new sweep, which is why the guard probes rather
    than trusting the file.
  - **Killing the leaf is enough, because the chain unwinds itself.** Every parent in the
    re-exec chain is blocked in `subprocess.run`, so each exits as soon as its child does
    — verified against a live four-process orphan, where a `taskkill /T` on the recorded
    leaf cleared all four. `--stop` on POSIX must signal the recorded pid **itself**:
    `pkill -P` only ever reaches children, and gating the SIGTERM on pkill having *failed*
    meant the sweeper survived precisely when it had a child, which is during the apply
    phase.
  - `tests/test_sweep_lifetime.py` fails the build if `run_orchestration.py` so much as
    names `CLAUDE_PID` in code, and `tests/test_plugin_shell.py` fails it if a
    `SessionEnd` hook reappears.
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

- **Title keywords match whole words, and the boundaries are conditional.** This
  reverses a plain `kw in title_lower` and must not be restored: a `title_excluded`
  drop is a verdict, so `intern` retired Internal Tools Developer *permanently*, `ios`
  retired Kiosk Manager and `mobile` retired Automobile Design Engineer. The rule lives
  once in `title_filter.title_matches`; `funnel.py` and `apply_title_filter` both call
  it. Two things it is easy to get wrong. Keywords are **phrases with punctuation**
  (`"manager, engineering"`, `"sr. "`, `"full-stack"`), so it escapes the keyword
  rather than tokenising — a `\b\w+\b` splitter cannot express them. And the boundary
  is a lookaround applied **only on a side whose character is a word char**: `\b`
  asserts a *transition*, so a trailing one on `"sr."` would demand the very word
  character the keyword stops at, and the match would never fire. Leading/trailing
  whitespace is stripped for the same reason. Nothing is stemmed, in either direction
  — `intern` misses Interns, `internship` misses Intern — which was chosen over a
  suffix allowance because the gate is permanent and a morphology guess is not
  reviewable. A blank keyword matches nothing; unguarded it would empty the sweep.

  Setup's drafting rules were reversed to match, and the reversal should not be undone.
  It now writes **bare single words scoped to the user's profession** — `staff`, not
  `staff engineer` — because a phrase catches the one specialisation the model thought
  of and misses Staff ML Engineer, Staff Data Scientist and the rest. The old rule
  qualified every rung word with a field noun; that was substring damage control, and
  restoring it as a safety measure costs recall and buys nothing. What keeps a bare
  word safe is the **profession check**, not the noun: `staff` is a promotion in
  software and the job itself for a Staff Nurse, so the field decides whether the word
  is drafted at all. Because nothing is stemmed, setup also **expands each term into
  every spelling employers write** (`intern, interns, internship, internships`;
  `senior, sr`; `vice president, vp`) — and `sr.` is never drafted, since bare `sr`
  already covers `Sr.` through the non-word-character rule above.
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
    the overview page says it means, and a wrong `candidate_years` can only touch
    jobs that were about to cost a call. YoE drops are counted into `above_cutoff`
    for the same reason cap drops are.

    **The overview page deliberately disagrees, and both are right.** Its `Relevant
    jobs` tile (`Database._relevant_sql`) excludes `yoe_below_requirement`, because
    that tile answers "what survived every free gate" — a user reading it wants the
    jobs still in the running, and a posting the resume cannot qualify for is not one.
    `stages["above_cutoff"]` answers "what reached the point of costing money", which
    is a question about the funnel, not about the user. So the tile and `matcher.py`'s
    `above cutoff → judged` console line will differ by exactly the YoE count. Do not
    "fix" either to match the other.

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
  copies keep their own location and link in the results CSV. Siblings carry
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

**Two files come out of a run, and they are not interchangeable.**
`<stamp>_results.json` is the shortlist the apply skill consumes via
`last_run.json`'s `json` pointer — **one row per cluster**, because 31 siblings
would otherwise become 31 applications. `<stamp>_results.csv`
(`results_export.py`) is the user's own file: every row in `matches`, best first,
eight columns — `posted_at, company, job_title, link, llm_score, cross_score,
applied, shortlisted`. A budget drop renders a **blank** `llm_score`, not the `0`
that `filtered_result` puts in the model — printing that zero reads as a verdict
and is what hid the broken reranker for an entire run.

Three consequences worth not re-deriving:

- **It sorts in Python, not SQL.** `load_all_matches` orders by `relevance_score
  DESC`, which files that placeholder `0` among the genuine low scores instead of
  at the bottom. `_never_scored` is the only thing that can tell them apart, and it
  is Python. `results_export._never_scored` and `reporting.data._never_scored` are
  two copies of one rule and `tests/test_reporting.py` holds them together.
- **The CSV is written once at the end, and therefore in a `finally`.** Sorting
  needs every row, so it cannot stream — but writing only on success meant a sweep
  that died half-scored left no CSV, no JSON, and a `last_run.json` still naming the
  previous run. `_write_run_outputs` and `_finalise_pipeline` both run in
  `run_pipeline`'s `finally`, in that order, and `complete: false` is how the
  pointer and the `runs` row record a partial sweep. The `runs` row is written
  either way, because it is the pages' only signal for *is this sweep still going* —
  a crashed run without one leaves both of them meta-refreshing forever.
- **It no longer streams, so nothing needs the Excel-lock dance.** `_track_results`
  writes only to the database; `_open_csv_append` and its retry/backoff are gone with
  the append-mode handle they protected.

### The reports are written by the engine

`hireshire/reporting/` renders the overview page, at two scopes, and nothing else.
It exists because the reasoning had nowhere to go — it was written to
`matches.raw_json` and rendered nowhere, so an empty shortlist was indistinguishable
from a broken threshold.

**Nothing is published.** There used to be a `dashboard.html` and a per-run
`<stamp>_matching.html`, the latter published as an Artifact from a fixed
`latest_matching.html`; three pages answered overlapping questions and the overview
is the one that answers *what have I got* at both scopes. With publishing gone,
`render.artifact_page` went too: `document()` is the only envelope, which is what
licenses the meta refresh.

**The engine writes them; a skill only hands over the path.** That is what makes
them appear on unattended monitor sweeps where no agent turn exists, costs no
tokens, and keeps the skills reporting numbers they were handed — the same rule as
`--paths`.

Three consequences that should not be re-derived:

- **Refreshes run on a clock, never on funnel events.** `orchestrate._tick_reports`
  offers a rebuild every `_REPORT_TICK_S` (half `reporting.MIN_INTERVAL_S`, derived
  from it so the two cannot drift); the throttle inside `refresh` still decides. This
  reverses the original event-driven wiring, and the reversal was forced by a live
  run: refreshes came off `on_company_start` and `on_job_score`, and **both stop**.
  The first ends with the scrape; the second fires only on an LLM score, which `top_k`
  caps. On sweep `2026-09-10T21-49-17Z` the tenth and last score landed four minutes
  in, and the reports then sat frozen for **47 minutes** while the matcher recorded
  ~1,470 further rows — the user was reading a page that could not move. It also made
  a missed row permanent: the refresh a callback schedules can read SQLite before that
  row commits, and with no later callback the page showed 9 of 10 scored jobs forever.
  A clock cannot run out, and the next tick self-corrects. Note this was invisible
  under global top-K, where `matches` genuinely stayed empty until the sentinel.
  `_stop_report_ticker` must run **before** the outputs are written and must drain the
  executor as well as cancel the task, or a late rebuild lands after the `final=True`
  write and re-arms the meta refresh — the bug the last bullet below describes.

- **Nothing may raise.** `reporting.refresh` swallows everything and the writers
  return `None` on failure, because it is called from the pipeline's own progress
  callbacks — the same trade `write_results_csv` documents.
- **The meta refresh is armed only while the pipeline's `runs` row is absent**, so
  the final refresh must run *after* `finalise_run`. Refreshing before it leaves a
  finished run reloading itself forever, and skipping `finalise_run` on a crash
  leaves a dead one doing the same.

**`overview.py` ships at two scopes.** `Dashboard_Lifetime.html` at the results root
covers every sweep the install has done; `Dashboard_<stamp>.html` in a run folder
covers that sweep and adds how long it took. Both are complete local documents.
Four numbers — `Jobs in scope`, `Relevant jobs`, `Jobs shortlisted`, `Jobs applied` —
over five `<details>` sections, under a `HireShire` heading and a `Lifetime Dashboard`
/ `Dashboard Run: <stamp>` subtitle. It explains nothing: past one line naming the scope and telling
the reader the sections open and filter, the judge's rationales inside an opened job
are the only sentences on it. **Both scopes are the same markup fed different data**,
and the `Took` tile is the single deliberate exception — how long it took is a fact
about a sweep, not about an install.

It had a second exception, an `Est. cost` tile, and `render.SHOW_COST` now ships
**off**. The figure was the Claude CLI's own client-side estimate at list price, and a
tile on a page whose question is *what have I got* reads as a bill. Nothing upstream
changed: `UsageTally` still meters every judge call, `matcher._log_usage` still prints
`~$X.XX at list price`, and `store.finalise` still writes `usage` into
`runs.stats_json` — so "what did that sweep cost" stays answerable, just not from the
page. The flag, `usd()` and the `if SHOW_COST:` block stay wired so flipping it back is
one edit, and `tests/test_reporting.py::test_the_cost_display_is_one_switch` flips it
**on** to prove the wiring behind it has not rotted.

The filenames are the only place the word "overview" ever reached a user, which is why
the module, the `report_paths` keys (`overview`, `run_overview`) and `last_run.json`'s
pointer fields (`overview_html`, `run_overview_html`) all keep their old names: those
are wiring, and renaming them would break consumers to no one's benefit. Note that
`tests/test_reporting.py` asserts no `dashboard.html` exists (a guard against the
deleted page) and `tests/test_plugin_shell.py` forbids that substring in any skill —
both comparisons are case-sensitive, and `Dashboard_Lifetime.html` clears them.
A bare `Dashboard.html` would not, since Windows paths are case-insensitive.

**Needs Attention sits between Jobs Applied and Jobs Shortlisted, and the `applied`
table feeds both.** `overview_snapshot` splits it on `status`: `submitted` goes to
Jobs Applied, and every other status goes to Needs Attention (`error`, `excluded`,
plus any status nobody has named yet, so a new one cannot disappear). The row's
`error` text is printed as a one-line `.job-sub` (`_attention_reason` clips it to its
first sentence). `apply_one.md` asks the session for exactly that line, and
`worker.EXCLUDED_REASON` is the one the engine writes itself. The `Jobs applied` tile
counts **`submitted` only**. It used to count every attempt, which is how known issue
A4 hid: a form stuck on a question read as a finished application. Both halves stay
in `applied_ids`, so a needs-attention job never also appears under Shortlisted.

**The user can record either outcome by hand, and the two are deliberately
asymmetric.** `/hireshire:mark-applied` over `scripts/jobs_cli.py` writes what the
applier could not: `Database.mark_applied_by_hand` promotes the row to `submitted`,
and `Database.decline_job` **deletes** it and un-shortlists the job with
`DECLINED_BY_USER`. A declined job gets no `applied` row at all, and that is forced
rather than chosen — every status that is not `submitted` renders under Needs
Attention by design, so a "not pursuing" status would sit in the one section the
feature exists to clear. It is the `location_mismatch` shape exactly: a verdict
reached after scoring, so `skipped` stays 0 and the job keeps its LLM score in Jobs
Filtered. Three consequences:

- **The promotion is a narrow `UPDATE`, never `record_applied`.** That writer is
  `INSERT OR REPLACE` on the primary key, so it would blank `board_token`, `title`
  and `absolute_url` — the columns `load_applied_matches` falls back on once a job's
  `matches` rows are pruned, which is exactly the old application being tidied up.
- **A hand-marked application is indistinguishable from an automatic one**, because
  the status written is plain `submitted`. A second "counts as applied" status would
  have to be added to `overview_counts`, `run_progress`, `lifetime_progress` and
  `data.overview_snapshot`, and each omission would be a silent undercount.
  Provenance belongs in a new column, not a new status.
- **The CLI rebuilds both pages itself**, from `last_run.json`. The pages are static
  files only the engine rewrites, and between sweeps nothing rewrites them at all — so
  without that call the user pastes the command, the database changes, and the page in
  front of them does not. The same limit as everywhere else applies: `refresh` writes
  the lifetime page and the newest run's page, so an older sweep's dashboard keeps
  showing what was true when it ran.

The buttons that carry this live in the row **body**, beside `Open posting →`, not in
the `<summary>`: a `<button>` there fights the `<details>` element's own activation,
and the six-column grid has no free cell. A `file://` page cannot write to SQLite, so
the button copies the command rather than pretending to record anything — with an
`execCommand` fallback and then a selectable `<code>`, because a local file is not
reliably a secure context and `navigator.clipboard` can simply be absent.

**All five sections read the same way, and the rows stay `<details>` for one
load-bearing reason.** Each section is a filter box over a sticky six-column header
(`# | Title | Company | Location | LLM | Cross`) over a `.scroll-y` box. Three of them
used to be unbounded flat lists beside one that was not, which made a sweep with 300
filtered jobs a wall. A real `<table>` with a JS-toggled detail row was the obvious
way to get columns and was rejected: such a row fires no `toggle` event, so
`_STATE_SCRIPT` could not restore the reader's open rows and the 15-second refresh
would shut the rationale they were halfway through. `<summary>` also gets Enter/Space
for free, and BASE_CSS's `th, td { white-space: nowrap }` would flatten every
rationale inside a `colspan` cell. Four more things about it:

- **The four rendered lists are server markup, never a payload.** `_STATE_SCRIPT`
  reopens by `getElementById` at parse time, so a row a paginating script has not
  built yet cannot be restored — which is the bug that script exists to prevent. They
  are capped at `MAX_JOB_ROWS` and bounded in height, not paginated.
- **`reason_label` and the applied stamp are `.job-sub` lines, not columns.** The
  labels are whole sentences and the columns are nowrap. Jobs Filtered is the section
  whose entire question is *why*, so dropping its reason would be the worst available
  regression.
- **`data-hay` is lowercased server-side**, so filtering is one `indexOf` per row per
  keystroke. Filtering sets `hidden`, which takes a row out of layout *and* the a11y
  tree and leaves an open row open when the filter clears.
- **The scroll box has to restore its own `scrollTop`.** A browser restores the
  document's scroll across a meta refresh but never an `overflow: auto` div's, so the
  box introduced a loss the flat list did not have. `data-keep-scroll` opts in, the
  reopen must happen first (a box inside a closed `<details>` has no layout box, and
  `scrollHeight` is the all-closed one until the bodies expand), and the last section
  deliberately opts out.

**The tiles and the sections count differently on purpose.** The tiles are a
cumulative funnel — every shortlisted job is also a relevant one. The sections are a
**partition**: `data.partition_jobs` puts each job in exactly one, because the page is
the user's only list of what is left to do and a job appearing twice would double it.
So `Relevant jobs` will read higher than the `Jobs Filtered` section below it, and
that is not a bug to reconcile. A second, smaller asymmetry: the tile excludes cluster
siblings (grouped after the rerank, never competed for a slot) while the sections let
a sibling follow its verdict, since it carries a real score copied from its
representative.

**What they must agree on is which row per job they read, and lifetime scope is where
that has teeth.** `matches` is keyed `(run_id, job_id)`, so a job dropped on a
*deferral* — the call cap, a scoring failure — comes back and its later sweep writes a
**second row** beside the first. `Database._canonical_matches_sql` is the one place
that chooses between them: `MAX(m.scored_at)` with bare columns, the same trick and the
same rule `_unapplied` uses for the backlog window. The lifetime sections, the
`Relevant jobs` and `Jobs shortlisted` tiles and the lifetime applier bar all go
through it, so a tile can no longer count a job on a reading the list below has
dropped. Run scope needs none of this — `(run_id, job_id)` is the primary key — and its
queries are deliberately left alone.

Three things about that rule which should not be re-derived:

- **It is "newest", not "the row that has a verdict".** Preferring a judged row looks
  safer and is not: `_judged_sql` is also true of a cluster sibling whose representative
  **failed**, which carries a placeholder 0 and nothing behind it, so the preference
  would bury a genuine `rerank_below_cutoff` written weeks later. Measured on a real
  install, no job's newest row loses a real verdict — the matcher retires a judged job,
  so a verdict is always the last word — and both rules produce identical tiles.
- **The predicates go on the outer select.** `_judged_sql()`, `_relevant_sql()` and
  `_sibling_sql()` read the row the aggregate has already chosen. Inside the aggregate
  they would be answered by rows the group is discarding.
- **The sections used to load in two halves and must not again.** One query for rows
  with a standing verdict and one for the rest, each deduping only *within* itself, put
  381 jobs on the page twice under contradicting labels — and its judged half carried
  its own limit, which silently truncated the scored jobs the user most wants (755 of
  1,233 on the same install). `partition_jobs` keeps a `placed` set anyway: the page's
  one real promise should not rest on the shape of whichever query fed it.

The last section, `Total Jobs Seen`, is the only one that reads the **`jobs` table**
rather than `matches` (`Database.load_unmatched_jobs`). That is what finally puts the
title-gate rejections on a page — `matcher.py` keeps them out of `matches` on purpose,
since there can be tens of thousands a run — and it is why that section alone is
script-built from a JSON payload with a filter box. Its `NOT EXISTS` is deliberately
**not** correlated on `run_id`: a job the `SeenStore` skipped because an earlier sweep
judged it would otherwise be listed here with a blank score, as though nothing had
ever read it. The price, accepted, is that on later sweeps the five sections no longer
sum to the `Jobs in scope` tile. Its payload carries **no LLM key** — a key holding 0
invites a renderer to print it as a verdict — while the renderer still prints an em
dash in that column, so the six columns match the sections above. A printed dash and
an absent key are not the same thing; only the key is dangerous.

Five things about it that are easy to get wrong:

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
  an em dash for it rather than that 0 — same rule as the results CSV's blank
  `llm_score`, and the reason `_job_entry` looks at `skip_reason` rather than trusting
  the score.
- **Nothing here may re-read what the caller already has.** `overview_snapshot` takes
  both `run` and `records` from `refresh`, which loaded them a few lines earlier. It
  used to re-run `load_all_matches` itself, which mattered
  little when refreshes came off funnel events and stopped early — and matters a great
  deal now that they run on a clock for the whole sweep, where it is the same query
  ~300 times a run for nothing. The `snapshot["candidates"]` guard reaches the overview
  through that same parameter: when the matcher has written no rows yet, `refresh`
  passes `[]` and no loader runs.

**The three progress bars above the tiles (Scraper, Matcher, Applier) read counters,
not rows.** They come from `run_progress`, a table that holds one row per
orchestrated sweep. No table the rest of the page reads can give these numbers:

- The scraper's company total exists only in memory.
- A not-found slug writes no `run_companies` row.
- Title-gate rejections and seen-store skips write no `matches` row.
- An excluded company, a deferral or a location skip writes no `applied` row.

Counting rows instead leaves every bar short of its total. The row itself is created
by `start_progress` in `run_pipeline`, and every write after that is an `UPDATE`.
That is why a phase run standalone records nothing, and why a failed write only logs.

Three rules to keep:

- **The applier bar counts jobs the worker has finished with, not applications
  sent.** Its total is `apply_queued`, which covers representatives only; siblings
  never reach the queue. The backlog is left out, because it belongs to earlier
  sweeps' shortlists.
- **The matcher bar counts the whole batch** once it is finished. Its total is the
  `Jobs in scope` tile.
- **Both pages show their bars all the time, but the lifetime page's are different
  numbers.** The run page keeps that sweep's bars after it ends, as a record of where
  each stage stopped. The lifetime page shows install-wide bars
  (`Database.lifetime_progress`):
  - Scraper and matcher are sums over every sweep that has a `run_progress` row, and
    the matcher's total counts jobs from those sweeps only, so older runs cannot hold
    it short.
  - The lifetime scraper bar **prints unique jobs but fills by companies**. A company
    count summed across sweeps means nothing to a user, and unique jobs
    (`COUNT(DISTINCT job_id)` over every run) has no total to fill against. The
    bar's `count` key is what lets a bar print a figure that is not its fill. The run
    page keeps printing companies, because that is how far a live scrape has got.
  - The applier is **not** a sum. It is every distinct shortlisted representative
    ever, against how many have an `applied` row. A sum of per-sweep counters reads
    ~100% whenever no sweep is running; the backlog keeps meaning something, and it
    covers sweeps made before tracking began.

  These queries group whole tables, so they ride `LIFETIME_INTERVAL_S` like the other
  lifetime reads.

The lifetime page carries its own throttle (`LIFETIME_INTERVAL_S`, 60 s) because its
queries group a table that has no `run_id` filter to narrow them; everything else in
`refresh` is indexed on `run_id` and stays cheap however long the user has been at it —
including the `jobs` read behind `Total Jobs Seen`, which `idx_jobs_run` covers and
whose `NOT EXISTS` rides `idx_matches_job`.

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

Applying is **not** a third engine phase with a `main()`, but it is on the queue. The
work is driving a browser, and that stays on the Claude side of the line: forms differ
per employer and the questions need a model that has read the resume.
`hireshire/applier/worker.py` is only the consumer — for each shortlisted job it
launches one `claude -p` session over `apply_one.md` (the per-job rules), which uses the plugin's Playwright MCP and returns an
`ApplyOutcome` via `--json-schema`; the engine records it. `orchestrate.py` wires the
phases over asyncio queues with exactly one `None` sentinel per queue, always sent in
a `finally`:

```
scraper.main(out_queue=q1) → q1[(board_token, list[Job])] → matcher.main(q1→q2)
  → q2[(MatchResult, Job)] → _collect_results → q3 → _track_results → pipeline_results table
                                                          └→ q4 → run_apply_worker → applied table
  → <workspace>/hireshire_run_results/<stamp>/<stamp>_results.{csv,json}
```

Four things about the applier that are easy to break:

- **One session per job, one at a time.** The old design ran the whole skill once,
  after the sweep, over `last_run.json` — a file that only exists at the end, so it
  could never stream. Do not batch jobs into one session to "save launches".
- **A failed launch is a deferral; an outcome is a verdict.** `submitted`/`error`
  write `applied` and retire the job. A session that never started or exited non-zero
  writes nothing, and `Database.load_pending_applications` (the backlog, bounded by
  `backlog_hours`) retries it next sweep — the only road back, because the matcher
  never streams a judged job twice. Three launch failures in a row stop the applier
  for the sweep.
- **The backlog's window closing is itself recorded, on the window and never on a
  count.** `backlog_hours` is measured against `scored_at`, which never advances — the
  matcher retires a judged job — so a job whose sessions keep failing to launch stops
  being retried after ~18 sweeps at a 4-hour poll. That used to happen silently: the
  row kept `shortlisted = 1` with no `applied` row, so it sat under Jobs Shortlisted
  for good, reading as work the applier would still get to, and the link the user could
  have used by hand was buried among jobs that looked pending. `worker.EXPIRED_STATUS`
  is the terminal record that ends it, written by a pass after the queue drains.

  **A retry counter was rejected and must not be added.** A launch failure is a fact
  about the host, not the job — known issue S2 is a machine where every `claude` launch
  failed at once — so retiring on N failures would discard a whole sweep's shortlist
  for a transient fault, which is precisely what "retire on a verdict, never on a
  deferral" forbids. The time bound was already there; this only makes the moment it
  fires visible, and the three gates on the pass (`include_backlog`, not `blocked`, the
  breaker not tripped) exist so a sweep that could apply to nothing declares nothing
  abandoned.

  Two consequences. `Database._unapplied` tests the age with `HAVING MAX(scored_at)`,
  not a `WHERE` on the row, because a rescored job keeps its stale row — the same fact
  `_canonical_matches_sql` exists for, and the same aggregate rule — and a row-level
  test would expire a job the backlog was still retrying; the two
  halves have to partition the set exactly. And the pass writes **no** `bump_progress`
  and does **not** clear `shortlisted`: these jobs belong to earlier sweeps, so the
  applier bar (total `apply_queued`) must not count them, and the shortlist tile keeps
  them exactly as an `excluded` row does.
- **`exclude_companies` is a verdict too, and is the one no session produces.** Those
  portals need an account login, so the answer is the same on every future sweep; the
  worker writes an `excluded` row itself, before the resume and breaker checks, and the
  job appears under Needs Attention with `worker.EXCLUDED_REASON`. It used to write
  nothing, which cost twice over: the job sat under Jobs Shortlisted as though the
  applier would get to it, and it came back through the backlog to be re-dropped every
  sweep for `backlog_hours`, leaving only a log line an unattended user never reads.
  Recording it retires the job, so lifting an exclusion later does **not** bring it
  back — the same open half of known issue A4, accepted for the same reason the
  ambiguous-ending rule accepts it.
- **A location skip is a verdict too, and is the one that retires a job with no
  `applied` row.** The posting page states a location outside the user's list, which
  reads the same on every future sweep, so `Database.mark_not_shortlisted` clears
  `shortlisted` and writes `location_mismatch` — the backlog's `WHERE m.shortlisted = 1`
  is what then stops seeing it. It had the `exclude_companies` bug and worse: with no
  retry counter anywhere, `poll_interval_hours: 2` and `backlog_hours: 72` re-drove one
  job ~36 times, a full `claude -p` and browser session each time to re-read a location
  that cannot change.

  Three things about it that are easy to get wrong:

  - **No `applied` row, and that is the difference from `exclude_companies`.** An
    account-login portal is something the user can go and do by hand, so it belongs
    under Needs Attention; a job in the wrong country is not, so it is un-shortlisted
    into Jobs Filtered instead. This also keeps the progress-bar rule below true as
    written.
  - **`skipped` must stay 0.** It is what `_judged_sql` and both copies of
    `_never_scored` read, and this job *was* judged — it carries a real LLM score.
    Setting it would blank that score in the results CSV and file the row among the
    never-scored ones. For the same reason `overview._job_entry` needs
    `location_mismatch` in `_SCORED_REASONS` but **not** in `judged`: the two look
    interchangeable and drive different things — the score column and the reason label
    — and Jobs Filtered is the section whose entire question is why.
  - **`mark_not_shortlisted` takes no `run_id`.** `matches` is keyed `(run_id, job_id)`
    and a backlog job's row belongs to an earlier sweep, so every row for the job is
    updated; leaving one shortlisted would put it straight back in the backlog and
    render it twice on the lifetime page under contradicting labels. It therefore lowers
    the `Jobs shortlisted` tile and the applier bar's denominator retroactively, at both
    scopes — correct, since the job is no longer waiting on the applier, and not a
    discrepancy to reconcile.

  **There is one location list, and the scraper owns it.** `ApplierSettings.location_filter`
  is *derived*: `load_applier_config` overwrites it from `scraper.location_filter` on
  every load, so a value in `applier.yaml` never wins and setup writes it in one place.
  `apply_one.md` used to hardcode six strings and read no config at all, which is how a
  job scraped as `Arlington, VA, United States` and rendered as `Arlington, VA` was
  skipped against a list the user never wrote. The session **infers** rather than
  string-matches — the list mixes countries, states and cities (107 entries on a real
  install) and a page rarely contains any of them verbatim — and an ambiguous location
  continues with the application, because a missed skip costs one form while a wrong
  skip retires the job for good.
- **Ambiguous endings are recorded, deliberately.** A timeout or an unreadable result
  may come after the submit click, so it is written as an `error` telling the user to
  check. Retrying it would risk a second application to the same employer, which is
  worse than a lost one.
- **The no-fabrication rule has one exception, chosen by the user.** When a form asks
  about a tool the resume does not show, `apply_one.md` answers **yes** and names the
  closest tool the resume *does* show, or **no** when nothing is close. The exception
  covers tools, languages, frameworks and platforms only. Employers, titles, degrees,
  dates, certifications, licences, clearances and background answers are never
  invented. Do not widen the exception, and do not "restore" the strict rule without
  asking. Screening answers (`work_authorized`, `requires_sponsorship`,
  `willing_to_relocate`) come from setup, and `null` means never asked, which is
  different from "no". Essays are written from the resume and the job description,
  never from the search profile, for the same reason the scorer never sees it.
- **`applied_ids` is re-read before every launch**, so a job recorded since the queue
  was built is never applied to twice.
- **The session loads the browser server itself** (`--mcp-config <ROOT>/.mcp.json
  --strict-mcp-config`). A `claude -p` the engine starts is not guaranteed to load the
  plugin — measured, it did not, and the namespaced tools were simply absent. So in
  that session the tools are `mcp__playwright__*`, and `apply_one.md` names them that
  way. Do not "fix" the worker to use the namespaced names.
- **Screenshots go to the run's own folder; the session runs in the WORKSPACE.** Those
  are two different questions and `worker.session_dirs` answers them separately, from
  the `run_dir` `orchestrate` hands the worker — the same `results_dir` the CSV, JSON
  and dashboard are written to, so the applier's output cannot land somewhere the rest
  of the sweep's did not. `out_dir` is `<run dir>/applied/`, reached through an absolute
  `screenshot_path`. It was one folder shared by every sweep, which left the user's only
  record of each submitted form in a pile with nothing saying which run it came from.
  There is no setting for it: `applied_dir` is gone, and the fallback needs none because
  `make_run_dir` already falls back to `DATA/results/<stamp>`.

  cwd is the other question. Playwright MCP uploads and writes only inside the client's
  roots, which Claude Code sets to the cwd, and the resume is in the workspace — running
  under DATA refused 5 of 8 uploads on one sweep with nothing submitted. So cwd is the
  workspace **when it actually contains `out_dir`**, which is the real invariant, and
  `out_dir` itself otherwise: with a workspace configured, `make_run_dir` can still fall
  back to DATA (folder gone, unwritable), and a cwd that no longer contains the run
  folder would lose every screenshot. A resume outside cwd is copied in. An explicit
  filename resolves against the root and ignores `--output-dir`, which only covers files
  the server names itself. The scorer stays in ROOT: `--safe-mode --tools ""` touches no
  files.

  **`--output-dir` is a per-job scratch dir, `<run dir>/applied/.browser/<job_id>/`,
  never the `applied/` folder itself.** Playwright MCP writes a `page-*.yml` for every
  snapshot and a `console-*.log` there unasked: one sweep left 204 and 20 of them beside
  7 screenshots. The session may read those files mid-form, so `run_apply_worker` deletes
  the scratch dir in a `finally` only *after* the outcome is recorded, whatever the
  ending. `session_dirs` clears the same things from `out_dir` on the way in, which now
  reaches only this run's folder — a killed sweep's leftovers stay in that sweep's
  folder, beside its screenshots, and nothing migrates the shared folder older installs
  filled. Only `page-*.yml`, `console-*.log` and `.browser/` are touched; screenshots are
  the user's record of each form.

Each `main()` takes optional `in_queue` / `out_queue` / `quiet`. `quiet=True`
suppresses Rich in favour of `logging` — required under the monitor.

## Things that are easy to get wrong

- **Board defaults.** Workday and BambooHR default **off**, and they are the two
  biggest lists: 24,200 companies held back against 15,871 swept (greenhouse 8,333,
  lever 4,369, ashby 3,163, direct 6), out of 40,071 shipped. `docs/SPECS.md` leads with
  40,000+ but must state plainly that the default sweep is ~15,871. Setup presents it
  as a time trade-off — and **no specific multiplier has been measured yet**, so say
  "considerably longer", not "3x". These counts come from `config/*_companies.json`
  and grow between releases; re-derive them rather than copying this paragraph.
- **The direct portals are searched for the user's countries, derived from
  `scraper.location_filter`.** `hireshire/direct/scope.py` resolves each term (country,
  state, city, `remote - us`) against `portal_locations.COUNTRIES`, and each handler
  builds its list URL from the result. Three rules, each learned from the portals:
  - **The portal codes are a checked-in table, never looked up at sweep time.** Apple and
    Google answer an unknown location with **zero results and a 200**, so a bad code
    empties the board silently every sweep. Apple's codes are irregular (`USA`, `GBR`
    but `INDC`, `CANC`, `AUSC`) and its lookup answers `georgia` with the Republic of
    Georgia. `scripts/refresh_direct_locations.py` re-derives the columns by hand.
    Intuit's free-text `Location=` is ignored outright; it scopes by a GeoNames country
    facet, which exists only where Intuit has openings, so its column is sparse.
  - **Anything unresolvable widens to everywhere; nothing narrows.** One unknown term,
    a bare `remote`, or a country missing from one portal's column leaves that portal
    unscoped. A narrowed scope hides jobs with no sign; a wide one only spends pages.
  - **A job whose list entry names no place carries `location_is_placeholder`**, and
    `scraper._matches_location` passes it. Google's list has no locations and Intuit's
    says "Multiple Locations"; a filter of cities or states never contains the country
    placeholder, which is how Google's whole board was once dropped and every Intuit
    multi-city job with it. The price, accepted: such a job reaches scoring unchecked
    until the applier's location verdict (`mark_not_shortlisted`) retires it.

  **Every direct portal is plain HTTP, including the two once thought browser-only.**
  Do not bring back a browser path for either:
  - **Microsoft** is `/api/pcsx/search`. The 403 "Not authorized for PCSX" comes from
    `/api/apply/v2/jobs` only. Its page is fixed at 10 and `location=` takes one
    country (a second is silently ignored), so it walks one series per country.
  - **Meta** answers 400 until a request carries browser fetch metadata (`Sec-Fetch-*`,
    `Origin`, `Referer`). With it, one GraphQL POST returns the whole board (~1,000
    jobs), so Meta has **no scope column** and `scraper.py` filters afterwards. Its
    `offices` filter wants exact names and empties the board on a wrong one.
    `meta.DOC_ID` is **checked in**: it is in neither the page nor its eager bundles.
    Meta rotates it, and a stale one answers 404, an error row rather than an empty
    board. The module docstring says how to refresh it. Its list has no date, so the
    first sweep takes in Meta's whole backlog once and `seen_jobs` handles the rest.
- **Interpreter discovery lives in exactly one place: `scripts/hireshire.sh`.**
  Two traps make this worth centralising. macOS has no bare `python` — Apple
  removed `/usr/bin/python` in 12.3 and Homebrew installs `python3` only. And
  Windows ships a Microsoft Store App Execution Alias named `python3.exe` that
  *exists on PATH*, prints an ad and exits 49, so `command -v python3` selects the
  broken one while the real `python` sits beside it. The launcher therefore
  **runs** each candidate and keeps the first reporting Python ≥ 3.10. Hooks,
  monitors and both skills go through it; nothing else may name
  an interpreter. Windows needs Git Bash so `sh` exists.
- **PyTorch comes from `uv pip install --torch-backend auto`, never a hand-kept tag
  list.** Off macOS, `bootstrap._install_with_uv` pip-installs uv (unpinned, so it
  knows the current PyTorch index tags, which change every release) and installs the
  whole requirements file through it. sentence-transformers then picks cuda → mps →
  cpu on its own; there is no `device` setting. Four rules keep a GPU upgrade from
  costing a working venv:
  - **Only an architecture mismatch downgrades to CPU.** uv reads the driver version,
    not compute capability (astral-sh/uv#14742), so the smoke test runs a matmul on the
    device. `bad_kernels` reinstalls CPU and writes the `_ARCH_FALLBACK` marker to
    `DATA/torch_variant`, which pins `cpu` from then on. `no_gpu` (a CUDA build, CUDA
    unavailable) is **kept**: it runs on CPU anyway, and treating a transient fault as a
    verdict would pin the install to CPU for good. The marker is only cleared by hand
    or by `HIRESHIRE_TORCH`.
  - **Never swap torch under a live sweep.** Bootstrap runs *before*
    `run_orchestration`'s duplicate guard and before every `run_engine` call, and on
    Windows replacing torch's DLLs under a running process half-removes it. So
    `main()` checks `sweep_pid` + `is_alive` first and defers.
  - **A failed download removes nothing, and only-the-torch-line-changed soft-fails.**
    uv downloads before replacing. If the requirements are otherwise unchanged and
    torch still imports, `main()` returns 0 with the lock unwritten, so an offline
    machine keeps sweeping and retries next start.
  - **The lock records the backend *requested*, not what uv resolved**, so
    `is_current()` never probes a GPU on SessionStart. A CPU `torch` is named for
    `--reinstall-package` only when `nvidia-smi`/`rocm-smi` exists, or every GPU-less
    install would re-download it once.

  Precision stays fp32 on every device, and that is load-bearing: `min_score` is a raw
  logit, and half precision would shift it. Out of memory in `Reranker._score` halves
  the batch (and keeps it halved), then swaps to a separately cached CPU copy — never
  `model.to()` on the shared one.
- Downstream of the launcher, `scripts/run_engine.py` re-execs into the venv and
  addresses its interpreter by absolute path — hook exec form cannot spawn the
  `.cmd`/`.bat` shims Windows installs.
- **Plugin-bundled MCP tools are namespaced** `mcp__plugin_hireshire_playwright__*`,
  not `mcp__playwright__*`. A rule written against the bare server key never fires.
- **A skill must not state runtime facts it has not asked for.** Three live failures of
  this kind cost a user real trust: one skill announced a running sweep that did not
  exist, another wrote the search profile to a directory it had guessed, and
  `start-orchestration` promised sweeps stopped with the session long after that had
  stopped being true. `--paths` answers the directory question and the skills are
  required to ask; the liveness question has no launcher answer any more, so a skill
  reports what the background task and its startup line told it and nothing further.
  `tests/test_plugin_shell.py` greps for all three regressions.
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
  It approves **no** browser tool. The read-only three it used to approve served
  `/hireshire:apply`, removed with that skill along with its Playwright hook matcher;
  `tests/test_plugin_shell.py` fails if the matcher reappears. Note the
  sweep's apply worker runs each per-job session as `claude -p --permission-mode auto`
  and therefore bypasses it entirely: unattended auto-apply has no human checkpoint by
  design.
- **The `codex` judge is `codex exec`, and five things about it were learned by
  probing, not read.** `hireshire/codex_cli.py` holds the rules; `CodexBackend` in
  `scorer.py` applies them. Each fails silently if undone:
  - `--output-schema` takes a **path**, and the schema must be OpenAI's strict
    dialect (`codex_cli.strict_schema`) or every call is an HTTP 400.
  - The answer is the **last** `agent_message` (openai/codex#19816). An
    `item.completed` of type `error` is routine and is not a failure.
  - The judge is **not an agent**. Tools, skills, sub-agents and environment context
    are stripped, which took one call from 11,207 input tokens to 1,769, and tools
    left on can make Codex drop the schema (#15451). `--disable` names are filtered
    through `codex features list`, since an unknown one fails the call.
  - **No caching between calls, by design of the CLI.** `prompt_cache_key` comes
    from the thread id and every exec is a new thread (#21796, open). Do not resume
    one thread to "fix" it — the context would grow by a posting per job. The
    tally's `caches=False` keeps the no-cache warning from blaming the prompt.
  - OpenAI counts cached tokens *inside* `input_tokens`, and there is no price, so
    `cost_usd` is None rather than 0.
  `model` has no Codex default: setup pins one from `setup_cli.py codex-check`, and
  the backend refuses a Claude name, because `provider` alone can be switched.
- **`userConfig` is not used** for anything load-bearing — its enable-time prompt
  has open bugs. The `setup` skill is the source of truth.
- **`.claude/settings.json` is gitignored, and must stay that way.** Same argument as
  the root `CLAUDE.md`: an install is a copy of this tree, so anything here ships. It
  once did — 51 dev allow rules (`sed -i`, `git mv`, scratchpad paths) plus
  `additionalDirectories` naming a developer's drive. The rules never fired, because a
  plugin directory is never trusted, but the refusal printed **645 characters to stderr
  on every judge call**, which is what let a real scoring failure be misread as a trust
  warning. Permissions a user needs are granted by `scripts/approve.py`, one recognised
  command at a time; a settings file is the opposite of that boundary. Dev permissions
  belong in `.claude/settings.local.json`, ignored alongside it.
- **Set an explicit `version` in `plugin.json`.** Omitting it pushes every commit at
  users. Semver + `CHANGELOG.md`.
- **The clean-machine test is the real acceptance test**: fresh user dir,
  marketplace add → install → setup → start-orchestration. Anything needing a terminal, a
  `git clone`, or a YAML file is a bug.
