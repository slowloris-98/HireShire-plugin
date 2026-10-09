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

# Engine, as the plugin runs it (re-execs into the venv in the data dir)
python scripts/run_engine.py orchestrate.py --once
python scripts/setup_cli.py set matcher --json '{"threshold": 75}'
```

## Architecture

### The ROOT/DATA/WORKSPACE split governs where every file goes

**ROOT** is the install dir and is **replaced wholesale on every plugin update** —
shipped, read-only content only: engine code, default YAMLs, company slug lists.
**DATA** (`~/.claude/plugins/data/hireshire-hireshire/`)
**survives updates** — venv, SQLite DB, the user's config, generated profile, logs.

**Putting mutable state in ROOT loses it on the next update.** `hireshire/paths.py`
is the single place this is decided; nothing else may resolve a path against the
working directory, because a plugin's cwd is whatever project the user is in.

**One namer, one creator, for the day layout.** `paths.run_dir_for(stamp)` decides
where a run folder goes and creates nothing; `make_run_dir` creates what it names, and
`orchestrate.finalise_abandoned_runs` uses it to find a killed sweep's folder again. A
flat `<root>/<stamp>` that **already exists wins**, which is what keeps run folders made
before the day layout exactly where the user left them — there is no migration, nothing
moves inside the user's own workspace, and no `file://` bookmark breaks. A stamp is
second-resolution, so that probe can only ever match a genuinely older run. The day
comes from `run_ids.day_of(stamp)`, which **validates** rather than slicing: an
unparseable run id makes `finalise_abandoned_runs` fall back to `stamp = run_id`, and
`""` is the signal to use the flat layout rather than build `<root>/""/<stamp>`.
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
made for their job search — resume in `resume/original/`, and inside
`hireshire_run_results/` one directory per **calendar day**, each holding that day's
run directories (`<YYYY-MM-DD>/<stamp>/`). Its absolute path is captured **once** by
`/hireshire:setup` into `scraper.workspace_dir`; `paths.results_root()` is the only
reader. This does not weaken the cwd rule above, it is what makes obeying it
possible: the *skill* knows the working directory and records it, the engine only
ever reads config, so a session launched from another folder still writes to the
folder the user chose. `hireshire/workspace.py` owns creating it and copying the
resume in, and refuses a workspace inside ROOT or DATA. Empty `workspace_dir` falls
back to `DATA/results`, which is where installs predating the setting keep writing —
so every statement about the results path needs that clause.

Consequences already worked out, which should not be re-derived:

- **There is no dead-slug skip list, and the seed-plus-delta scheme that used to
  solve it must not come back.** Every slug in an enabled board's file is tried on
  every run. A 404 is recorded as a `not_found` row in `run_companies` for that run
  and changes nothing for the next one.

  What was removed: a curated `config/bad_slugs.json` in ROOT (15,584 slugs), plus
  `user_bad_slugs.json` and `user_recovered_slugs.json` deltas in DATA, combined as
  `seed ∪ user_bad − user_recovered`, with `scripts/verify_bad_slugs.py --prune`
  writing recoveries as a delta rather than editing a file about to be replaced. All
  three files and that script are gone.

  **The reason is the failure direction, not the bookkeeping** — the ROOT/DATA
  layering was correct and is what makes this tempting to rebuild. The list was read
  once before a sweep and never re-checked during one, so it could only grow: a slug
  that 404'd through a transient outage, or a company that moved boards and came
  back, was skipped on every future sweep. The only road back was a terminal command,
  in a plugin whose premise is that users never open a terminal. So a wrong entry was
  permanent and invisible, and it landed on exactly the employers a user would most
  want re-checked. That is the same rule as `_RETRYABLE_SKIP_REASONS` below: a 404 on
  one sweep is a deferral, not a verdict, and the two must not be confused.

  The price, accepted: a default sweep goes from ~9,805 companies to all 15,871, and
  ~6,066 of those requests get a 404. It is paid in a phase that is already I/O-bound
  and rate-limited per board. `docs/SPECS.md`'s default-sweep figure was always the
  unfiltered one, so it needed no correction — it is simply true now.
- **The Greenhouse, Lever and Ashby lists grow only through
  `scripts/discover_slugs.py`, and it only ever adds.** With no skip list every
  shipped slug is a request on every sweep for every user, so a slug is admitted only
  when the board's own API (the scraper's `BASE_URL`) answers with at least one
  posting. Removal is deliberately absent: it would be the 404-as-verdict the bullet
  above forbids, just made by hand. `tests/test_discover_slugs.py` fails if a removal
  path appears.

  Candidates come from Common Crawl's URL index, which lists URLs its crawler fetched
  and never guesses. So coverage is whatever the crawler reached: one crawl found
  ~1,100 new live Greenhouse and Ashby boards, and **Lever almost nothing**, because
  the crawler barely visits `jobs.lever.co` (its robots.txt allows it). `--extra
  lever=<list>` runs any other list through the same check, and is Lever's source.

  **The index server lies with a 200.** It can cut a page off mid-URL and still
  answer 200; measured, 8,105 lines came back as 2,232, and the first version of this
  tool cached that and undercounted Ashby by half. `page_is_complete` rejects a page
  that does not end on a whole record, pages are one index block (`pageSize=1`, ~4 s)
  rather than the server's 5, and nothing is cached until whole. It also answers
  502/504 often and the occasional transient 400, so a failed page is reported and
  skipped, and a rerun fetches only that page. Do not "simplify" the completeness
  check away because the status code looks fine.
- **The recurring sweep is NOT session-scoped, and nothing may make it so again.**
  `scripts/run_orchestration.py` is an ordinary sleep/sweep loop. `--monitor` runs it
  recurring, `--sweep` runs one cycle (`--once`) and is what the OS scheduler entry
  uses — one program, so a fix to either reaches both. `/hireshire:start-orchestration`
  is the only sweep command; the one-shot `/hireshire:find-jobs` and the manual
  `/hireshire:apply` were removed in 0.11.0, so do not bring either back. Three
  rules survive from the old design: it reads `poll_interval_hours` from the user's
  config itself (`orchestrate.py --interval` defaults to 4 and never looks); every
  stdout line reaches the agent, so it emits one summary line per cycle and logs the
  rest to a file — which is also why the re-exec hop must keep inheriting stdio; see the
  console bullet below; and nothing may detach the process.

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
  document against the phase's pydantic model *before* writing. **The writer is
  YAML 1.2 and every reader is PyYAML, which is YAML 1.1**, so a string like `no`,
  `on` or `off` is written bare and read back as a bool. That shipped in 0.15.0:
  `disability: no` failed `ApplierSettings`, and `run_orchestration` turned auto-apply
  off for the whole sweep. So `_quote_ambiguous` asks PyYAML about every string it
  writes, and validation runs on the **PyYAML re-parse of the rendered text**, never
  on ruamel's tree — the tree is what passed that file. Do not "simplify" either back.
- **Setup edits what the last run left behind, so every branch must write its own keys
  — including the ones whose value is the default.** The config lives in DATA and
  survives updates and re-runs, so a key setup does not write keeps its old value. That
  is invisible on a fresh install, where every default is already correct, and it breaks
  the moment a user *changes* an answer. It shipped that way on all four provider
  branches: question 10's `claude_code` and API-key options and question 11's
  `claude_code` option described a provider and never wrote one, so choosing Claude
  after using Codex left `provider: codex` in the file and the sweep carried on using
  Codex while the user had just been told otherwise.

  The scoring half also has to write `model`, and that is the sharper edge:
  `ClaudeCodeBackend` passes `matcher.model` to `claude -p` and validates nothing (the
  `codex_cli.is_claude_model` guard runs only in the Codex direction), so a Codex model
  left behind fails every scoring call — the breaker trips and the remainder is
  `backend_unavailable`, which is at least retryable and printed. **No guard was added
  for that direction**: a name the regex does not match can still be legitimate, a
  Bedrock `us.anthropic.claude-*` among them, and the console already prints
  `provider/model` on every sweep. `applier.model` is genuinely inert for the Claude
  session, which passes no `--model`, so a leftover there is harmless — and
  `tests/test_apply_worker.py` pins that, so restoring the flag "for symmetry" cannot
  break switching back quietly.

  `tests/test_plugin_shell.py::test_every_provider_branch_writes_its_own_provider_key`
  checks this over the skill's `bash` blocks rather than its prose, because a branch that
  only *describes* its write is exactly the bug. Same family as the "a shipped default
  never reaches an existing install" rule behind `exclude_companies`/`amazon`.

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
  derives it from the user's own scored postings; the shipped 3.0 is a starting point
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

Note that `MatchStore.finalise` records only summary stats — individual verdicts reach
the posting via `append_result`. Budget drops and cluster siblings are written
explicitly so the user can see what the budget cost; title-gate rejections deliberately
are not, since there can be tens of thousands per run. They are not unrecorded, though:
`matcher._record_gate_reasons` writes the gate's verdict onto `postings.gate_reason`,
which is what the overview's last section prints as its `Reason` column. A column the
reports do not group is not the same as a scored row, and that distinction is the whole
basis for this split — `match_json IS NULL` is what the last section reads to find them.

`append_result` goes through `upsert_match`, which is an **`UPDATE`** onto the row the
scraper wrote and never inserts: `RunStore.save_company` writes a company's postings
before it queues the batch, so the row is guaranteed. A rowcount of 0 therefore means
something is wrong upstream rather than that a row needs making, and it is **logged**
rather than raised — one judged job lost is worth a warning, aborting a sweep over it
is not, and silence is the one option that is not acceptable, since the symptom would
be a job that was paid for and then vanished from every page.

**Two files come out of a run, and they are not interchangeable.**
`<stamp>_results.json` is the shortlist the apply skill consumes via
`last_run.json`'s `json` pointer — **one row per cluster**, because 31 siblings
would otherwise become 31 applications. `<stamp>_results.csv`
(`results_export.py`) is the user's own file: every posting this sweep judged, best
first, eight columns — `posted_at, company, job_title, link, llm_score, cross_score,
applied, shortlisted`. Its `applied` column reads a set of **`(board_token, job_id)`
pairs** from `Database.applied_ids`, not bare ids: an id alone is not unique across
boards, so an id-keyed set would mark one employer's posting applied on the strength
of another's. A budget drop renders a **blank** `llm_score`, not the `0`
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

`hireshire/reporting/` renders the overview page, at three scopes, and nothing else.
It exists because the reasoning had nowhere to go — it was written to
`matches.raw_json` and rendered nowhere, so an empty shortlist was indistinguishable
from a broken threshold.

**Nothing is published.** There used to be a `dashboard.html` and a per-run
`<stamp>_matching.html`, the latter published as an Artifact from a fixed
`latest_matching.html`; three pages answered overlapping questions and the overview
is the one that answers *what have I got* at every scope. With publishing gone,
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

  A forced kill skips it too — `--stop` is `taskkill /F`, and a killed shell task
  runs no `finally` either. So `orchestrate.finalise_abandoned_runs` writes that row
  from outside (`completed: false, stopped: true`, which renders a `stopped` chip)
  for every `run_progress` row lacking one. `--stop` runs it right after the kill via
  `scripts/finalise_stopped.py`, and `run_orchestration` runs it at start-up for every
  other kind of kill. Only the newest orphan gets `_write_run_outputs`, since that
  repoints `last_run.json`. Its folder comes from `DATA/current_run.json`, written as
  a run starts, because `make_run_dir` may have fallen back. **It must only run when
  no sweep is alive**: the in-flight run matches the same query.

**`overview.py` ships at three scopes.** `Dashboard_Lifetime.html` at the results root
covers every sweep the install has done; `Dashboard_Day_<YYYY-MM-DD>.html` in a day
folder covers that day's sweeps; `Dashboard_<stamp>.html` in a run folder covers that
sweep and adds how long it took. All are complete local documents.
Four numbers — `Jobs in scope`, `Relevant jobs`, `Jobs shortlisted`, `Jobs applied` —
over five `<details>` sections, under a `HireShire` heading and a `Lifetime Dashboard`
/ `Dashboard Day: <YYYY-MM-DD>` / `Dashboard Run: <stamp>` subtitle. It explains
nothing: past one line naming the scope and telling
the reader the sections open and filter, the judge's rationales inside an opened job
are the only sentences on it. **Every scope is the same markup fed different data**,
and the `Took` tile is the single deliberate exception — how long it took is a fact
about a sweep, not about a day or an install. The day page therefore substitutes the
two questions that *are* about a day, `Sweeps` and `Avg per sweep`, and the lifetime
page has neither, because a mean over months is not a number anyone acts on. `Sweeps`
is `len(run_ids)` — the scope itself, so nothing can disagree with the page it labels —
while `data.day_summary` averages only the sweeps with **both** timestamps. A sweep in
flight has no pipeline `runs` row, so it counts in `Sweeps` and not in the average: the
two tiles deliberately do not multiply out to the day, and `measured` is what lets the
tooltip say so. An unmeasured average is an em dash and **zero is `0s`** — a sweep that
finished inside a second was measured — which is why `render.humanise_seconds` takes
`float | None` and must never test the value for truthiness.

**The day is the date in the run's *stamp*, which is local, and it is never a range.**
`hireshire/run_ids.py` owns this. A UTC prefix or a UTC window over `run_id` would file
a 9pm sweep under a date the user never sees, and a window recomputed at render time can
silently exclude the very sweeps in the folder it names once the machine's offset
changes — leaving a page that disagrees with its own directory and nothing saying why.
So a day's scope is an **enumerated list of run ids**, bucketed by
`run_ids.day_of_run_id`, which is by construction the same composition that produced the
folder name. `Database.known_run_ids` is where that list comes from, and the union of
`run_progress` and `runs` is not belt-and-braces: a sweep in flight is only in the
first, a standalone phase run only in the second.

One thing about the day scope in SQL that must not be reversed: `run_ids=[]` means
"a day with no sweeps" and must read **zero**. `IN ()` is a SQLite syntax error and a
truthiness test (`if run_ids:`) falls through to *lifetime* scope, so the whole
install's numbers would render under a heading naming one empty day. Every reader
branches on `is not None`, and `refresh` skips the page as well.

There used to be a second rule here — the day filter went *inside*
`_canonical_matches_sql`, mirroring the rule that state predicates go outside — and it
is gone with that method. It existed because `matches` held one row per sweep per job:
inside, `MAX(m.scored_at)` picked each job's newest row *among the day's sweeps*, while
outside the aggregate chose the all-time newest row and the outer `WHERE` then
discarded the job **entirely** whenever that row belonged to another day. One row per
posting removes the choice. **It also removes the old answer**, and that is the
user-visible half: a day's page now shows the verdicts that day's sweeps *reached*
(`scored_run_id`), so a job a later sweep re-judged has moved to the later page. See
the `postings` note below.

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
the module, the `report_paths` keys (`overview`, `day_overview`, `run_overview`) and
`last_run.json`'s pointer fields (`overview_html`, `day_overview_html`,
`run_overview_html`) all keep that name: those
are wiring, and renaming them would break consumers to no one's benefit.

**`day_overview` is taken off `results_dir.parent`, not resolved from `paths`, and the
key is absent when that folder is not the run's day.** `run_dir_for(stamp).parent` would
name the *configured* workspace even when `make_run_dir` had already fallen back to
`DATA/results/` because the drive is unplugged — and `overview.write` swallows the
`OSError` and returns `None`, so the page would be lost with only a warning. The absent
key is also how a run folder predating the day layout says it has no day page: an
unguarded `.parent` would drop one beside `Dashboard_Lifetime.html`, duplicating the one
a new-layout sweep writes inside the day folder the same day with neither authoritative.
`last_run.json` records `""` for it, the same blank-not-`None` rule the `csv` field uses. Note that
`tests/test_reporting.py` asserts no `dashboard.html` exists (a guard against the
deleted page) and `tests/test_plugin_shell.py` forbids that substring in any skill —
both comparisons are case-sensitive, and `Dashboard_Lifetime.html` clears them.
A bare `Dashboard.html` would not, since Windows paths are case-insensitive.

**Needs Attention sits between Jobs Applied and Jobs Shortlisted, and the `applied`
table feeds both.** `overview_snapshot` splits it on `status`: `submitted` goes to
Jobs Applied, and every other status goes to Needs Attention (`error`, `excluded`,
plus any status nobody has named yet, so a new one cannot disappear). The row is
printed as a one-line `.job-sub` holding a **fixed label per cause**
(`Requires human verification`, `Required question: <topic>`, `Posting closed`…).
`hireshire/applier/reasons.py` is the only place those labels are spelled. The engine
writes them (`worker.EXCLUDED_REASON`, the ambiguous endings, the expiry), and
`apply_one.md` tells the session to copy them exactly. `tests/test_apply_worker.py`
fails if the prompt and the module drift apart. The session used to write free text,
which put one cause on the page in six wordings.

`_attention_reason` maps the **stored** text onto a label with `reasons.short_label`,
at render time. That is what shortens rows written before the labels existed, and
what absorbs a model that ignores the instruction. It never rewrites the row: the
stored text stays whole as the `title=` tooltip, which is the only place its detail
(which question, which employer) survives. Text no rule accounts for gets `None` and
is word-clipped as before, because a label must never stand in for a message it cannot
explain. The regexes are ordered and first-match wins: a verification code is also
"required", so the specific causes come before the `Required question` fallback. The
`Jobs applied` tile
counts **`submitted` only**. It used to count every attempt, which is how known issue
A4 hid: a form stuck on a question read as a finished application. Both halves stay
in `applied_ids`, so a needs-attention job never also appears under Shortlisted.

**The user can record either outcome by hand, and the two are deliberately
asymmetric.** `/hireshire:mark-applied` over `scripts/jobs_cli.py` writes what the
applier could not: `Database.mark_applied_by_hand` sets `apply_status` to `submitted`,
and `Database.decline_job` **clears the application record** and un-shortlists the job
with `DECLINED_BY_USER`. A declined job keeps `apply_status` NULL, and that is forced
rather than chosen — every status that is not `submitted` renders under Needs
Attention by design, so a "not pursuing" status would sit in the one section the
feature exists to clear. **A NULL there therefore has to keep meaning "no application
record at all"**, which is what an absent `applied` row used to mean. It is the
`location_mismatch` shape exactly: a verdict reached after scoring, so `skipped` stays
0 and the job keeps its LLM score in Jobs Filtered. Four consequences:

- **Both are narrow `UPDATE`s, and `record_applied` is one too now.** That writer was
  `INSERT OR REPLACE` on its own table's primary key, so re-recording through it would
  blank `board_token`, `title` and `absolute_url` — the columns an application fell
  back on once a job's `matches` rows were pruned. Those columns belong to the scraper
  and live on the posting, so the hazard is gone at the root; what is unchanged is that
  this is a separate writer, and why.
- **Both are keyed on the bare `job_id`, which is the one place the composite key shows
  through to a user.** The page's buttons and the CLI have nothing else, and an id can
  name two postings when two boards mint the same one. The two outcomes make opposite
  trades, deliberately: `decline_job` and `mark_not_shortlisted` act on every match,
  because un-shortlisting the wrong posting is recoverable (it keeps its score and its
  place in Jobs Filtered), while `mark_applied_by_hand` returns **`ambiguous`** and
  writes nothing, because claiming an application that was never made is not. Widening
  the command to carry a board token was rejected: `scripts/approve.py` matches the
  exact command string, so it would ripple into the guard, the `mark-applied` skill and
  the button's copied command, for a case that is vanishingly rare.
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

**Four of the five sections carry buttons, and which buttons is a statement about the
row, not a style choice.** Needs Attention and Jobs Shortlisted get both outcomes.
Jobs Filtered and `Total Jobs Seen` get **`applied` only** (`_APPLIED_ONLY`): those
jobs are already un-shortlisted or were never shortlisted and have no application
record, so `decline_job` would clear nothing and un-shortlist nothing, and offering it
would hand the user a command that answers `nothing_to_change`. Jobs Applied gets
neither — the outcome is recorded. Do not "restore" the pair for symmetry.

Putting the button on those two sections used to force a **third** source of identity
in `Database.mark_applied_by_hand`: an `applied` row, then the canonical `matches` row,
then `jobs`. A title-gate rejection had neither of the first two — that is why it is in
`Total Jobs Seen` and nowhere else — so without that branch the button copied a command
that answered `unknown` and wrote nothing, for the largest section on the page. One row
per posting leaves one lookup, so the three sources and the `"inserted"` return value
are gone; `"unknown"` still means what it said, a `job_id` the database has never seen,
and `"ambiguous"` is the new answer for an id two boards both mint.

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

**What they must agree on is which row per posting they read, and the answer is now
"there is one".** `postings` is keyed `(board_token, job_id)` and holds the posting's
*current state*, so every scope reads the same row and a tile can no longer count a job
on a reading the list below has dropped.

That is the one thing `Database._canonical_matches_sql` existed to buy, and it is gone
with it. It wrapped every lifetime read in `MAX(m.scored_at) … GROUP BY m.job_id`,
because `matches` was keyed `(run_id, job_id)`: a job dropped on a *deferral* — the call
cap, a scoring failure — came back and a later sweep wrote a **second row** beside the
first, so each reader had to choose. Choosing wrong was expensive: counting "any row
that ever said so" listed 381 jobs twice on the lifetime page and left 8 more in the
`Relevant jobs` tile on a reading the cross-encoder had since overturned.

Three things about it that should not be re-derived or reinvented:

- **The rule survives, made once at write time instead of on every read: newest wins.**
  The matcher retires a judged job, so a verdict is always the last word and only a
  later sweep can supersede a deferral. The refinement that looks safer — prefer a row
  with a standing verdict — stays rejected and is now unrepresentable: `_judged_sql` is
  also true of a cluster sibling whose representative **failed**, which carries a
  placeholder 0 and nothing behind it, so the preference would bury a genuine
  `rerank_below_cutoff` written weeks later.
- **Its two placement rules are gone rather than relocated**, and neither should come
  back when something looks asymmetric: state predicates on the outer select, scope
  filters inside the aggregate. There is no aggregate.
- **The sections used to load in two halves and must not again.** One query for rows
  with a standing verdict and one for the rest, each deduping only *within* itself, put
  381 jobs on the page twice under contradicting labels — and its judged half carried
  its own limit, which silently truncated the scored jobs the user most wants (755 of
  1,233 on the same install). `partition_jobs` keeps a `placed` set anyway: the page's
  one real promise should not rest on the shape of whichever query fed it.

**A run page is a record of what that sweep did, not a ledger entry per sweep**, and
that is the most visible consequence of the collapse. The verdict figures filter on
`scored_run_id`, the sweep that reached the verdict the row carries, so a job sweep #1
deferred on the call cap and sweep #5 judged appears on **#5's** page and not on #1's.
#1 still counts it under `Jobs in scope`, because #1 first saw it — which is why that
tile is documented as wider than the sections beneath it. Day and lifetime scopes are
unaffected: they ask the same question of the same row.

The last section, `Total Jobs Seen`, is the one that lists postings **nothing ever
scored** (`Database.load_unmatched_jobs`, `match_json IS NULL`). That is what puts the
title-gate rejections on a page — `matcher.py` never scores them on purpose, since
there can be tens of thousands a run — and it is why that section alone is script-built
from a JSON payload with a filter box. Two halves, and they only read as a
contradiction:

- **`match_json IS NULL` is not scoped to a run.** The question is *has anything ever
  scored this posting*, and a job an earlier sweep judged — which the matcher has since
  retired — must not be listed here with a blank score as though nothing had.
- **`first_run_id` is: a posting belongs to the sweep that first saw it, and to no
  other.** Without it the seventh sweep's page re-lists everything the first sweep's
  title gate threw out, as though it had just found it — on a mature install, most of
  the section. It is a stored column; it replaced an anti-join
  (`NOT EXISTS (… e.run_id < j.run_id)`) that derived the same fact from 7.7 rows per
  posting, plus the covering index that anti-join made mandatory. Because it names one
  sweep, a posting a day's 9am sweep found and its 1pm sweep found again appears exactly
  once on the day page, with no extra rule.

Note "first seen" means *the first sighting still on record*, and that is now a property
of **`prune_runs`** rather than of a query: when it prunes the sweep a surviving posting
was filed under, it rewrites `first_run_id` to the oldest sweep that is left. Without
that rewrite such a posting would appear on no run or day page at all and only at
lifetime scope — a third behaviour nobody has reasoned about. Self-correcting, and
honest, but the two have to be read together.

The price, accepted twice over now, is that on later sweeps the five sections no longer
sum to the `Jobs in scope` tile — that tile is deliberately **wider**, counting every
posting the scope did work on, which includes one an earlier sweep first saw and this one
re-judged after a deferral. Do not reconcile them. Its payload carries **no LLM key** — a key holding 0
invites a renderer to print it as a verdict — while the renderer still prints an em
dash in that column, so the six shared columns match the sections above. A printed
dash and an absent key are not the same thing; only the key is dangerous.

**It is the one section with a `Reason` column, and the only reason it can have one is
that the verdict is now persisted.** `postings.gate_reason` holds it, written by
`matcher._record_gate_reasons` in one batched `UPDATE` per company batch. The title
gate rejects tens of thousands of jobs a sweep and `matcher.py` deliberately never
scores them for that reason — which left the verdict recorded nowhere, and thousands of
rows on a page with no way to say why any of them was there. A column on a row the
scraper had already made costs an UPDATE, not a row in a table the reports group. Four
consequences:

- **The scraper must not write the column, and `insert_jobs` had to stop being
  `INSERT OR REPLACE` for that to hold.** `OR REPLACE` *deletes the row and inserts a
  new one*, so a column the statement does not name comes back as its default —
  leaving it out was not enough, it has to be left out of an `ON CONFLICT … DO UPDATE`.
  `matcher._persist_hydrated_details` re-inserts a job to attach its description, so
  under the old writer that second call silently erased the gate's verdict.

  **The merge multiplied that hazard by twenty and added two more writers to it.** One
  row now carries `retired_at`, every score, `shortlisted`, `skip_reason`, `match_json`
  and all five apply columns — everything the matcher, the gates and the applier own —
  so a re-sighting must touch none of them. And `upsert_match` and `record_applied`
  were `INSERT OR REPLACE` against tables of their own; against the merged row either
  would have destroyed the scrape and put back defaults. All three are named-column
  statements now, the two verdict writers are `UPDATE`s that never insert, and that is
  the single most important invariant in `db.py`.
- **`gate_reason` is a bare column, and the rule it used to need a cross-run lookup for
  is now structural.** The rule is *any sweep that recorded a reason wins*. It was a
  `MAX()` over a job's rows, then — once the first-sighting anti-join chose one row — a
  `COALESCE` onto a correlated lookup, because **the reason was not always on the
  earliest row**: a sweep killed outright (`--stop` is `taskkill /F`, which runs no
  `finally`) left `jobs` rows the matcher never gated, so the *next* sweep gated the
  job and held the reason. Both sweeps write the same row now. What is left of the rule
  is a filter in the writer: `matcher._record_gate_reasons` passes `skip_reason or ""`,
  and `record_gate_reasons` **skips blanks**, so a later sweep cannot overwrite a real
  `title_excluded` with an empty string. That filter is the whole of the protection, and
  `tests/test_db.py::test_a_reason_recorded_on_a_later_sweep_still_reaches_the_page`
  is the test that notices if it goes.
- **The column is fed by two different columns and `_tail_payload` normalises them**:
  `skip_reason` for the jobs the cutoff and the YoE gate dropped, `gate_reason` for the
  title gate's own three. That is done there rather than in either loader because it is
  the one place both halves have already been concatenated. `data.short_reason` is a **second** label table, not a truncation of
  `REASON_LABELS`: the short form is a different phrase, and the two disagree about
  `""` on purpose — "scored by the LLM" to the long one, an em dash to the short one,
  since a judged job never reaches this section.
- **Three columns are now bounded, not one, and all three take the widths the grid
  above them already uses** — company `8rem`, location `11rem` (the ceiling of its
  `minmax(6rem, 11rem)`) and a `12rem` *floor* on `td.wide` overriding BASE_CSS's
  18rem. Reason plus the action cell take ~230px the six-column table did not, and on
  real data one cap was no longer enough: measured on a live install the row ran 1,131px
  in a 951px box and the "I applied" button, the last column, was off the edge. The
  location cap is `td:nth-child(5)`, since Reason sits ahead of Location — a stale
  index truncates a two-word verdict and lets the locations run off instead, the exact
  regression the rule exists to stop. Title is the one cell that wraps, so a floor
  rather than a ceiling lets it absorb whatever the bounded columns leave.

The trailing cell holding the `I applied` button is the second thing this section has
that the others do not, and it is a cell rather than a row body because these rows are
built by a script from a payload: `_STATE_SCRIPT` can only reopen a row already in the
document, which is why the tail is not `<details>` in the first place. The payload's
`j` key is the job id the button needs; a row without one gets an empty cell, never a
button that cannot name anything.

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
  Note it names `match_json` outright and **takes no alias**: it used to need one,
  because `jobs` and `matches` each had a `raw_json` column and the predicate was
  ambiguous in any query joining the two. Giving the two blobs their own names removed
  the ambiguity rather than parameterising around it.
- **`_judged_sql` is a SQL mirror of `data._never_scored` and the two must agree.**
  The Python one cannot be used at lifetime scope (it needs the blob parsed per row)
  and the SQL one cannot be dropped (the lifetime page groups the whole table).
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
- Title-gate rejections and retired-posting skips reach no scoring column.
- An excluded company, a deferral or a location skip writes no application record.

Counting rows instead leaves every bar short of its total. The row itself is created
by `start_progress` in `run_pipeline`, and every write after that is an `UPDATE`.
That is why a phase run standalone records nothing, and why a failed write only logs.

Three rules to keep:

- **The applier bar counts jobs the worker has finished with, not applications
  sent.** Its total is `apply_queued`, which covers representatives only; siblings
  never reach the queue. The backlog is left out, because it belongs to earlier
  sweeps' shortlists.
- **The matcher bar counts what the sweep had work to do on**, not the whole batch, and
  its two halves are defined against each other. `matcher.py` bumps `jobs_processed`
  with `unseen` — the batch minus the postings an earlier sweep already retired — and
  the total is the `Jobs in scope` tile, which is the same `Database._new_work_sql`
  call. That rule is *first sighting **or** a verdict this scope reached*, and both are
  columns now (`first_run_id`, `scored_run_id`): a title-gate rejection reaches no
  scoring column but is work this sweep did, while a cap drop is not retired and so
  legitimately returns as work later. Change one half without the other and the bar
  reads a clamped 100% from the first batch of every repeat sweep. It is **not** plain
  first-sighting for the same reason the tile is not: a posting the cap deferred and a
  later sweep judged is real work that sweep did, and it is listed in that sweep's
  Relevant and Jobs Filtered sections.

  **The two halves must stay a top-level `OR`.** Under the old shape the scope filter
  restricted the whole predicate, because the verdict half needed a `jobs` row in that
  same run to hang off; written as a conjunct against one row it collapses to
  `first_run_id = ?` and the second half disappears without a word, which puts the top
  of the funnel *below* the lists beneath it.
- **Both pages show their bars all the time, but the lifetime page's are different
  numbers.** The run page keeps that sweep's bars after it ends, as a record of where
  each stage stopped. The lifetime page shows install-wide bars
  (`Database.lifetime_progress`):
  - Scraper and matcher are sums over every sweep that has a `run_progress` row, and
    the matcher's total counts jobs from those sweeps only, so older runs cannot hold
    it short.
  - The lifetime scraper bar **prints unique jobs but fills by companies**. A company
    count summed across sweeps means nothing to a user, and unique jobs (one posting
    however many sweeps found it, via `_new_work_sql` over every run) has no total to
    fill against. The bar's `count` key is what lets a bar print a figure that is not
    its fill. The run page keeps printing companies, because that is how far a live
    scrape has got.
  - **Both `jobs` figures count postings, never rows**, and they keep their two
    different scopes: the matcher's total over the tracked sweeps only, `unique_jobs`
    over every run. At *day* scope those scopes coincide and the two collapse to one
    number — which is the per-sweep double count going away, not a bug to pull apart.
  - The applier is **not** a sum. It is every distinct shortlisted representative
    ever, against how many have an application record. A sum of per-sweep counters
    reads ~100% whenever no sweep is running; the backlog keeps meaning something, and
    it covers sweeps made before tracking began.

  These queries group whole tables, so they ride `LIFETIME_INTERVAL_S` like the other
  lifetime reads.

The day page rides the lifetime throttle and shares `_last_lifetime` with it, but for
a different reason, and the difference matters: the day queries are
`run_id IN (<a day's worth>)` and index-backed, so they are cheap. What costs is the
**render** — a third full `build()` of the same rows. Verifying that the SQL is cheap is
therefore not a reason to move it onto the fast tick.

The lifetime page carries its own throttle (`LIFETIME_INTERVAL_S`, 60 s) because its
queries group a whole table with no scope filter to narrow them; everything else in
`refresh` is index-backed and stays cheap however long the user has been at it. The
two that matter are `idx_postings_first_run` (the `Total Jobs Seen` section, and half
of `_new_work_sql`) and `idx_postings_scored_run` (the verdict figures, and the other
half).

**The index that used to be mandatory is gone with the thing it served.**
`idx_jobs_job(job_id, run_id, gate_reason)` existed because the first-sighting
anti-join and the cross-run `gate_reason` lookup both had to be answered without
touching 580,727 rows — and without it one run-scope read measured **31.8 s** against
7 ms. Both are single-row lookups on a primary key now, so that hazard cannot return.
The 39.6 s `COUNT(*) FROM jobs` behind the scraper note is gone too: `scrape_counts`
sums `run_companies.job_count`, which is written per employer per sweep anyway.

**`postings` grows only by new postings**, which is the user-visible shape of the
collapse: the scraper upserts a posting it has seen before instead of writing a fresh
copy of its description. On the install this was measured against that was 580,727 rows
for 75,732 postings, 3.81 GB of it re-sightings. `run_companies` and `pipeline_results`
still fill per sweep, and every one of them fills *continuously*, because selection is
a per-job cutoff and each employer's batch is judged as it arrives. Both pages' copy was
rewritten for that; it used to explain that nothing could be scored until the sentinel,
which was true under top-K and is now a lie the reader would catch.

The per-stage counts (`gated → reranked → above cutoff → judged`) exist for a failure
mode the cutoff introduced: under a ranking, something was always scored, so an empty
shortlist could only mean weak jobs. Under a cutoff it can equally mean `min_score` is
wrong for this resume, and the two are indistinguishable without the counts. A large
"reached the reranker" with zero above the cutoff is the tell.

### Layer 2 — the engine

Two phases, each independent: own entrypoint, own `hireshire/<phase>/` subpackage,
own `config/<phase>.yaml`. All tabular data lives in one SQLite DB (WAL).

**One posting is one row**, in `postings`, keyed `(board_token, job_id)`: the scraper
upserts it, the matcher writes its scores and the judge's verdict into the same row,
and the applier reads scored rows from it and writes the outcome back. It replaced four
tables — `jobs`, `matches`, `applied` and `seen_jobs` — and the four things that are
still per-run are per-run by nature: `runs` (phase spans and stats), `run_companies`
(which employers a sweep reached), `run_progress` (the dashboards' three bars) and
`pipeline_results` (a sweep's export rows). `meta` is per install.

Six things about that table that are easy to break, each of which has its own note
elsewhere in this file:

- **No writer may be `INSERT OR REPLACE`.** `OR REPLACE` deletes the row and reinserts
  it, so a column the statement does not name comes back as its default — and one row
  now carries the scrape, the gates' verdicts, the scores and the application. Only
  `insert_jobs` inserts; `upsert_match`, `record_applied`, `record_gate_reasons`,
  `mark_seen` and the rest are `UPDATE`s against the row it made.
- **`job_json` and `match_json` must never collapse into one name.** One is the scraped
  `Job` dump, the other the `MatchResult`; they overlap on `job_id`, `board_token`,
  `title` and `absolute_url`, and `location` has a different *shape* in each. A single
  `raw_json` is what forced `_sibling_sql` to take a table alias, and merging them would
  make every blob read silently ambiguous.
- **Both key columns are declared `NOT NULL` explicitly.** In SQLite a composite
  `PRIMARY KEY` on a rowid table does not imply it, and two NULLs compare distinct — so
  without them the key admits the duplicate rows it exists to prevent.
- **`board_token` leads the key**, for write locality: the scraper upserts one
  employer's batch at a time across ~15,871 employers. The lookups that have only the
  bare id — the overview's buttons, `applied_ids`, `mark_applied_by_hand` — take
  `idx_postings_job`.
- **The wide columns come last in the row.** SQLite reads columns in order and spills
  long values to overflow pages, so a dashboard query that stops before `job_json`,
  `match_json` and `content_text` never traverses them.
- **`scored_at IS NOT NULL` (`Database._SCORED`) is what "this posting has a verdict"
  means.** The existence of a `matches` row used to imply it; a posting always has a
  row now, so every reader of the scoring columns has to say so or it counts the whole
  scrape as judged.

**The schema version is load-bearing for the first time.** `SCHEMA_VERSION = 2`, and a
file stamped higher is *refused* rather than read with the wrong shape — a downgrade is
the one case where carrying on is worse than stopping. The merge itself is gated on the
shape it finds (`PRAGMA table_info(jobs)`) rather than on the version, which is what
avoids having to answer "does an absent version mean v1, or a new file?". Three
operational consequences:

- **Stop a running sweep before updating.** A sweeper started on the old code is
  executing old SQL against its file; migrating underneath it would fail every
  statement it issues, loudly and repeatedly, mid-sweep. So `_migrate_to_postings`
  asks `sweep_pid` + `process_liveness.is_alive` first and raises
  `SchemaMigrationBlocked` with one actionable sentence. Deferring silently is not
  available, because the new readers have no old SQL to fall back on. No ancestor walk
  is needed: `run_orchestration._loop` proves no sweeper is alive, connects, and only
  *then* records its own pid, so a new sweeper always migrates with the pid file empty
  or stale.
- **The merge is one transaction, driven by hand.** Python's `sqlite3` at the default
  `isolation_level=""` opens a transaction for DML only, so `with self._conn:` would
  leave every `CREATE`/`DROP`/`ALTER` in autocommit — non-atomic while *looking*
  atomic. For the same reason it cannot use `executescript`, which issues an implicit
  COMMIT when a transaction is pending; `_statements` splits the DDL instead, with
  `sqlite3.complete_statement` rather than `str.split(";")`, because the comments in
  `_POSTINGS_DDL` contain semicolons of their own.
- **`VACUUM` is a separate, deliberate step.** `DROP TABLE` only frees pages to the
  freelist, so the file keeps its old size — about 3.8 GB of a 5.3 GB file on the
  install this was measured against. The merge sets `meta.vacuum_pending` and
  `scripts/jobs_cli.py compact` reclaims it, because `VACUUM` wants another copy's
  worth of temp space and minutes of work while `Database.__init__` is on the path of
  every process, including the report refresh and `--stop`. That subcommand is also the
  entry point `prune_runs` never had.

Applying is **not** a third engine phase with a `main()`, but it is on the queue. The
work is driving a browser, and that stays on the agent side of the line: forms differ
per employer and the questions need a model that has read the resume.
`hireshire/applier/worker.py` is only the consumer — for each shortlisted job it
launches one CLI session over `apply_one.md` (the per-job rules), which uses the plugin's Playwright MCP and returns an
`ApplyOutcome` as a structured result; `applier.provider` decides whether that is
`claude -p` or `codex exec`, and the engine records what comes back. `orchestrate.py` wires the
phases over asyncio queues with exactly one `None` sentinel per queue, always sent in
a `finally`:

```
scraper.main(out_queue=q1) → q1[(board_token, list[Job])] → matcher.main(q1→q2)
  → q2[(MatchResult, Job)] → _collect_results → q3 → _track_results → pipeline_results table
                                                          └→ q4 → run_apply_worker → applied table
  → <workspace>/hireshire_run_results/<YYYY-MM-DD>/<stamp>/<stamp>_results.{csv,json}
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

  **That a job came off the backlog is stored, not derived.** `record_applied` takes a
  keyword-only `from_backlog` and `postings.from_backlog` holds it, so the overview's
  Jobs Applied row can add `reasons.FROM_BACKLOG` to its `submitted <time>` sub-line.
  It has to be written at the moment it is known: an application carries no `run_id`,
  so nothing downstream can tell which sweep did the applying, and `applied_at` against
  `scored_at` is a guess. Only the verdict writer passes it — the `excluded` row never reaches Jobs
  Applied and the expiry pass is backlog-only by definition — and Needs Attention rows
  deliberately do not show it, because that section's question is what the user must do
  now. Rows written before the column read `False`, which is the honest answer rather
  than a reconstruction.
- **The backlog's window closing is itself recorded, on the window and never on a
  count.** `backlog_hours` is measured against `scored_at`, which never advances — the
  matcher retires a judged job — so a job whose sessions keep failing to launch stops
  being retried after ~18 sweeps at a 4-hour poll. That used to happen silently: the
  row kept `shortlisted = 1` with no application record, so it sat under Jobs Shortlisted
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

  **Every direct portal is unioned in by `load_applier_config`, not listed in a
  default.** It reads ROOT's `direct_companies.json`, because a user's `applier.yaml`
  sits in DATA and never receives a shipped default: an install set up before `amazon`
  was listed kept driving Amazon's login wall. So removing a portal from the YAML does
  not opt it back in, by design.
- **A location skip is a verdict too, and is the one that retires a job with no
  application record.** The posting page states a location outside the user's list,
  which reads the same on every future sweep, so `Database.mark_not_shortlisted` clears
  `shortlisted` and writes `location_mismatch` — the backlog's `WHERE shortlisted = 1`
  is what then stops seeing it. It had the `exclude_companies` bug and worse: with no
  retry counter anywhere, `poll_interval_hours: 2` and `backlog_hours: 72` re-drove one
  job ~36 times, a full `claude -p` and browser session each time to re-read a location
  that cannot change.

  Three things about it that are easy to get wrong:

  - **No application record, and that is the difference from `exclude_companies`.** An
    account-login portal is something the user can go and do by hand, so it belongs
    under Needs Attention; a job in the wrong country is not, so it is un-shortlisted
    into Jobs Filtered instead. `apply_status` stays NULL, which is the same thing a
    declined job relies on. This also keeps the progress-bar rule below true as
    written.
  - **`skipped` must stay 0.** It is what `_judged_sql` and both copies of
    `_never_scored` read, and this job *was* judged — it carries a real LLM score.
    Setting it would blank that score in the results CSV and file the row among the
    never-scored ones. For the same reason `overview._job_entry` needs
    `location_mismatch` in `_SCORED_REASONS` but **not** in `judged`: the two look
    interchangeable and drive different things — the score column and the reason label
    — and Jobs Filtered is the section whose entire question is why.
  - **`mark_not_shortlisted` takes no `run_id`, and a backlog job is why.** The job
    was judged by an *earlier* sweep, so a writer scoped to the current one would
    retire nothing and the job would go straight back into the backlog — one browser
    session per sweep, to re-read a location that cannot change. (It used to have to
    update a row per sweep for the same reason, or the lifetime page rendered the job
    twice under contradicting labels.) It therefore lowers the `Jobs shortlisted` tile
    and the applier bar's denominator retroactively, at both scopes — correct, since the
    job is no longer waiting on the applier, and not a discrepancy to reconcile.

    It is keyed on the bare `job_id` rather than the posting's full key, because the
    overview's decline button has nothing else. That is the conservative half of the
    trade `mark_applied_by_hand` makes the other way: un-shortlisting a second posting
    that happens to share an id keeps its score and its place in Jobs Filtered, while
    marking it applied would claim an application that was never made.

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
- **The per-company cap is a deferral, and the page predicts it rather than records
  it.** `hireshire/applier/limits.py` holds the rule: at most `max_per_company` (2)
  `submitted` rows per `board_token` per `company_window_hours` (72), re-read from
  SQLite before every launch like `applied_ids`. A held job writes **nothing** — "this
  employer had two applications this week" changes with time, so recording it would
  retire a job on a deferral. It stays shortlisted and the backlog hands it back every
  sweep. Only `submitted` counts, which includes hand-marked jobs; `error` does not,
  including the ambiguous endings below — the user chose that.

  Two consequences. **The backlog window is widened** to
  `max(backlog_hours, company_window_hours + 24)` while the cap is on
  (`limits.backlog_window_hours`), and both loaders use it so they still partition.
  Without it, siblings of one employer's batch are scored together and applied minutes
  later, so the third one's slot frees just *after* its `scored_at + 72h` and it
  expired a moment before it could have gone. **The shortlisted row's
  `Company limit reached · retries after …` line is computed at render**
  (`data.mark_holds`, same rule, same table) and never stored, because a stored marker
  would need clearing when the slot frees. The report reads the cap out of
  `applier.yaml` directly, like `_matcher_settings`, falling back on the defaults in
  `limits.py`; the pydantic model takes its defaults from there too, so the two cannot
  drift.
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
  asking. Graduation dates are answerable because the user states them:
  setup reads each degree off the resume, the user confirms the month, and it is stored
  in `education` as `YYYY-MM`, so a degree with no confirmed date is omitted, never
  guessed. `postal_code` is asked the same way and stays a string. Screening answers (`work_authorized`, `requires_sponsorship`,
  `willing_to_relocate`) come from setup, and `null` means never asked, which is
  different from "no". The EEO self-identification answers (`gender`,
  `race_ethnicity`, `disability`, `veteran_status`) come from setup too, as `Literal`
  strings where `""` means never asked and the session **declines**, which is what it
  did for every one of them before they existed. They are never inferred from the
  name or the resume. Essays are written from the resume and the job description,
  never from the search profile, for the same reason the scorer never sees it.
- **A form question aimed at bots is never answered, followed or evaded.** When a
  field asks whether the applicant is a bot or AI, or tells an AI to type something,
  `apply_one.md` stops before submitting and reports `error` with exactly `Manual
  application required.`, so the job lands under Needs Attention. Obeying gets the
  application flagged; answering as a human is a misrepresentation made in the user's
  name. It is a verdict — the form asks the same thing next sweep — and it is checked
  in-session only, because the question lives in the form, not the description.
- **`applied_ids` is re-read before every launch**, so a job recorded since the queue
  was built is never applied to twice.
- **The session loads the browser server itself** (`--mcp-config <ROOT>/.mcp.json
  --strict-mcp-config`). A `claude -p` the engine starts is not guaranteed to load the
  plugin — measured, it did not, and the namespaced tools were simply absent. So in
  that session the tools are `mcp__playwright__*`, and `apply_one.md` names them that
  way. Do not "fix" the worker to use the namespaced names.
- **Either CLI can drive the browser, and there is no failover between them.**
  `applier.provider` (empty → `claude_code`, or `codex`) picks one session class in
  `hireshire/applier/sessions.py`, which mirrors `matcher.make_backend` deliberately:
  one provider, built once, and a raise if it cannot be. `run_apply_worker` turns that
  raise into its existing **`blocked`** state — no `applied` rows, no expiry pass, every
  job left shortlisted for the backlog. That is forced, not chosen: retrying the job on
  the other CLI would be a second browser session against a form the first may already
  have submitted, which is the one thing the applier must never risk. So the pydantic
  validator on `provider` **rejects a typo** rather than falling back, because "unset"
  and "misspelled" must not collapse into each other. The keys are the applier's own
  (`provider`/`model`/`effort` in `applier.yaml`), not `matcher.*`: the judge is one text
  call and this drives a browser for minutes. `applier.model` has no default for the
  same reason `matcher.model` has none, and `codex_cli.is_claude_model` is shared by
  both so a `sonnet` cannot reach `codex exec`.

  Four things about the codex session were settled by probing codex-cli 0.157.0 against
  a real form, and each fails silently if undone:

  - **`approval_policy="never"` alone denies every MCP tool call** — measured, on the
    first navigate: `"MCP tool call requires approval, but approval policy is never"`.
    The browser never moves. `mcp_servers.playwright.default_tools_approval_mode` must
    be `"approve"`; the accepted values are `auto | prompt | writes | approve` and
    **`auto` is not the permissive one**. It is set in `worker._mcp_overrides`, which
    renders the same `ROOT/.mcp.json` the Claude session gets as `-c` pairs, so the two
    providers cannot drift on the server's command or version pin.
  - **The sandbox is `workspace-write`, not the judge's `read-only`.** Codex's sandbox
    reaches the MCP server's operations and not merely its own shell: under `read-only`
    a `file://` navigation failed outright. No `network_access` override is needed —
    the browser is its own process. The writable root is `-C`, which is `dirs.cwd`, and
    `session_dirs` already guarantees cwd contains `out_dir`, so the screenshot is
    inside the writable root by construction. `--ignore-user-config` is the
    `--strict-mcp-config` analogue: it keeps the user's own servers out of an
    unattended session.
  - **Codex exposes MCP tools under their BARE names**, with the server as a separate
    event field. So `apply_one.md` carries one `{{TOOL_PREFIX}}` token that
    `build_prompt` substitutes, and is **never forked** — the no-fabrication rules, the
    bot-question rule, the location rule and the Needs Attention labels stay in one
    place. A test asserts no prompt reaches a model with the token still in it.

    Still true on 0.160.1, where it was re-checked because it looked like the cause of
    a live failure and is the one change here that would break everything: a real call
    emits `mcp_tool_call server='playwright' tool='browser_navigate'`. **Do not set
    `tool_prefix = "mcp__playwright__"` for codex** — that name is not callable, and
    `tests/test_codex_applier.py` pins the empty prefix against exactly that edit.
  - **Whether the model USES the browser tools is nondeterministic, and that is not the
    same question as whether they are attached.** Measured on 0.160.1: six identical
    runs of the shipped argv, three made a real `mcp_tool_call` and three declared the
    browser unavailable and gave up in 7-10 s, against 12-14 s for the ones that
    worked. Live, that was 8 failed applications against 2 submitted in a day, each
    failure ending in 4-12 s where a real application takes 2-4 minutes — and **zero**
    in 199 applications on `claude_code`, which is what made it look provider-related
    rather than a coin flip.

    Two things follow, and the second is the one that cost real applications:

    - `apply_one.md` says the server **is** attached and to search for the tools before
      concluding anything, which took the same probe to **six of six**. That paragraph
      is load-bearing, not reassurance.
    - **A session with no browser tools is a DEFERRAL**, raised as `ApplyLaunchError`
      by `worker.apply_one` — the one clean ending that is not a verdict. With no
      browser it cannot have opened the posting, let alone clicked submit, so the
      no-double-apply rule that makes every other clean ending a verdict has nothing to
      protect. Recorded, it retired the job permanently on a fact about the *session*;
      11 jobs went that way on one install. Because the model writes the cause as free
      text — three wordings for one cause — the rows did not group either, so a query
      for one wording found 3 of the 11. `reasons.BROWSER_UNAVAILABLE` and its
      `short_label` rule collapse the stored text; the rule sits ahead of
      `POSTING_CLOSED`, whose "no longer available" would otherwise claim it. **No
      retry counter** — `backlog_hours` already bounds it, and counting a host-level
      fault would retire a whole sweep's shortlist.

    `--disable tool_search_always_defer_mcp_tools` is **not** the fix and must not be
    added: the name is accepted on 0.160.1 (the feature is `removed`, defaulting on) so
    it fails silently, and a probe carrying it still reported no browser tool.
  - **A failed turn is `SUBMIT_UNCONFIRMED`, not a deferral**, and this is where the
    mapping deliberately differs from `CodexBackend`'s, which raises for the matcher to
    retry. A session that drove a form for ten minutes and then failed its turn may
    already have submitted it. A *non-zero exit* is still a deferral for both CLIs.
    Reading the answer stays `codex_cli.parse_events`, i.e. the **last** `agent_message`,
    and here that is load-bearing rather than tidy: one real run emitted three premature
    `{"status":"submitted","screenshot":null}` messages before the form had been touched.

  `--output-schema` does survive with MCP tools active at this version with this
  `--disable` list, which openai/codex#15451 warns it may not. That was the gate the
  feature had to clear before any of it was written, and it is worth re-checking rather
  than assuming if a Codex upgrade starts returning unreadable outcomes.
  `codex_cli.APPLY_DISABLED_FEATURES` holds the same 15 names as the judge's list on
  purpose — an MCP server is *config*, not a feature, so nothing had to be lifted, and
  `browser_use`/`computer_use` stay off precisely *because* this session has a browser:
  Codex's own would honour neither `--output-dir` nor the roots rule above.
- **Screenshots go to the run's own folder; the session runs in the WORKSPACE.** Those
  are two different questions and `worker.session_dirs` answers them separately, from
  the `run_dir` `orchestrate` hands the worker — the same `results_dir` the CSV, JSON
  and dashboard are written to, so the applier's output cannot land somewhere the rest
  of the sweep's did not. `out_dir` is `<run dir>/applied/`, reached through an absolute
  `screenshot_path`. It was one folder shared by every sweep, which left the user's only
  record of each submitted form in a pile with nothing saying which run it came from.
  There is no setting for it: `applied_dir` is gone, and the fallback needs none because
  `make_run_dir` already falls back to `DATA/results/<YYYY-MM-DD>/<stamp>`.

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
  biggest lists: 24,200 companies held back against 17,707 swept (greenhouse 9,071,
  lever 4,370, ashby 4,260, direct 6), out of 41,907 shipped. `docs/SPECS.md` leads with
  40,000+ but must state plainly that the default sweep is ~17,707. Setup presents it
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

- **The location gate normalises the job, never the terms, and must not go back to a
  raw substring test.** `scraper._matches_location` matches `location_filter` against
  `locations.filter_haystack(raw)` — the employer's string plus its inferred country,
  the spelled-out name of a US state written as an abbreviation, and every alias of
  that country. The two sides are written by different people and almost never agree:
  users write `california` or `united states`, employers write `Foster City, CA`.
  Measured on a real install, the raw test **silently discarded 15% of every job
  scraped** (84.7% → 99.2% of 447,742 rows), and it discarded them per employer rather
  than at random: all 236 of Zoox's Lever postings bar the 46 whose alternate office
  happened to be a city someone had typed out by hand. A user naming only a state was
  worse — `["california"]` admitted 11%, against 28% now.

  Four things here that look redundant and are not:

  - **`filter_haystack` is not `normalize_location`.** The latter appends only the
    country, and the six direct handlers **store** what it returns, so it is a string a
    user reads; its exact output is pinned by tests. The haystack is matching-only text
    and is where new synonyms go. Folding the two together puts `", california, usa,
    u.s., us, america"` on the overview page.
  - **The country's aliases are appended because the terms are matched verbatim.**
    Setup records what the user typed, so `usa`, `us` and `america` have to be found in
    the haystack — none is a substring of `United States`.
  - **The gate still fails closed, with one exception.** A location that resolves to no
    country is dropped, so `Portugal` and `Ukraine` stay out of a US+India scope. The
    exception is a **remote-shaped** location when the filter contains a `remote` term:
    `Remote` cannot be rolled up to a country, so inference has nothing to answer with.
    That is why a setup writing short country terms must still write a `remote` term for
    a user who wants remote work.
  - **`US_STATE_BY_ABBREV` is the only state table.** `US_STATES` and
    `US_STATE_ABBREVS` are derived from it. They were two hand-kept tuples and could
    not map `CA` → `california` between them, which is the whole reason a state-level
    filter failed. `_US_MARKER_RE` is derived from the table's aliases for the same
    reason — the hand-copy it replaced had drifted and lacked bare `us`, so thousands of
    `Remote - US` postings inferred nothing.

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
    first sweep takes in Meta's whole backlog once and `postings.retired_at` handles
    the rest.
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
- **Every child the engine starts with fully piped stdio gets its own invisible console
  on Windows; the two re-exec launchers must never get one.**
  `claude_cli.own_console_kwargs()` is the single place that decides, and it returns
  `creationflags` and nothing else — the moment it carries `stdout`, `env` or `cwd` the
  exclusion below becomes unstateable. A Windows child inherits its parent's console by
  default, and `USER32.dll`'s init attaches the process to that console's window station
  and desktop *before any user code runs*, so when the console the sweeper inherited
  goes stale (its Claude Code session ended or restarted, its terminal was closed) every
  subsequent spawn dies with `0xC0000142` in ~20 ms with no stdout and no stderr — while
  the parent, which only does HTTP, carries on scraping.

  **It is per process and an event, not per sweep and not an accumulation**, and that is
  what retired the old diagnosis. Measured: one sweeper scored cleanly at 23:25, 00:33
  and 01:39, then failed every cycle from 02:46 to 09:26 and never recovered; a restart
  cured it instantly. Onset was 3h20m into that process and ~74 min into another, and a
  census found 344 processes with zero orphaned children — so desktop heap, leaked
  processes, a locked screen and sleep are all ruled out, and `docs/known-issues.md` S2's
  original suspects were wrong. `DETACHED_PROCESS` is the wrong flag: with no console at
  all, `npx` and `cmd` grandchildren may allocate **visible** ones on the user's desktop.

  **The exclusion is `run_orchestration._reexec_in_venv` and `run_engine.run`**, which
  pass no `stdout`/`stderr` and must keep inheriting: the child's one-line-per-cycle
  `print(..., flush=True)` is the only thing the background shell task surfaces to the
  agent, so a new console there would silently sever the sweep's output from the user —
  the same failure class as announcing a sweep that was never running. Getting it wrong
  on a piped site costs a dead child that logs an exit code; getting it wrong there costs
  silence, which is why the rule is "every piped site, mechanically" rather than "the
  ones we think are at risk" — whose console is stale is not knowable at the call site.
  `tests/test_child_console.py` pins both halves by AST over the shipped spawn sites, so
  a new spawn site cannot be added without answering the question, and it asserts the two
  exempt calls pipe *nothing*, so piping one of them fails the build rather than quietly
  reclassifying it.

  Note this is **spawn kwargs, not CLI flags.** `scorer.py` is right that `--safe-mode`
  and `--tools ""` must never be shared through `claude_cli` — the applier needs tools
  and an MCP server — and that rule is about argv, which decides what the model can do.
  This decides whether Windows will start the process at all. The judge and the applier
  want identical spawn kwargs and different argv, and the two policies do not touch;
  `tests/test_apply_worker.py` asserts both halves in one body so they read as a pair.
  For the same reason `scorer._reap` kills the **leaf** rather than the tree: a judge
  session has no children by construction, and a tree-wide kill on Windows *means
  spawning `taskkill`*, which is subject to the very failure this bullet is about.
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
  Neither `matcher.model` nor `applier.model` has a Codex default: setup pins one from
  `setup_cli.py codex-check`, and both refuse a Claude name through the shared
  `codex_cli.is_claude_model`, because `provider` alone can be switched. The applier
  reaches `codex exec` with its own rules — see the applier bullets above, and note its
  sandbox and approval settings are **not** the judge's.
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
