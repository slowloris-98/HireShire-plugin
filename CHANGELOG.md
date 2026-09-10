# Changelog

All notable changes to this plugin are documented here. Versions follow
[semver](https://semver.org/); users only receive an update when `version` in
`.claude-plugin/plugin.json` is bumped.

## [0.3.0] — unreleased

### Removed

- **`dry_run` is gone. The applier is on or off.** It used to fill every form and
  stop short of submitting, and it read as a safety net without being one. Left on,
  it produced a plugin that looked like it was working and had never applied to
  anything; left off, it had already been bypassed. The run it rehearsed was never
  the run that followed.

  `enable_applier` is now the only gate, and it still ships false. **Turning it on
  means real applications are submitted, unattended, from the first sweep** — the
  monitor launches the apply skill with `--permission-mode auto`, so the
  click/upload prompt that `scripts/approve.py` normally preserves does not apply
  there. `exclude_companies` is the only other limit, and neither it nor the
  read-only browser allowlist may be widened.

  The `dry_run` column stays in the `applied` table: rows written before this carry
  real values, and dropping a SQLite column is awkward for no gain. Nothing reads
  it, and new rows write 0.

- **`ApplierSettings.headless`** — declared, never read. The browser is driven by
  the Playwright MCP server, which is headed by default; this setting had no effect
  on anything.

### Fixed

- **The recurring sweep was killed by other Claude Code sessions ending, roughly a
  minute after it started, every time.** The `SessionEnd` hook fires for *every*
  session that ends anywhere on the machine — including the short-lived `claude -p`
  sessions the scorer itself spawns — and it called `stop()` unconditionally, killing
  whatever pid the status file named.

  It presented as a crash and was misdiagnosed as one for two days. `taskkill /T /F`
  sets exit code **1** and terminates without unwinding, so the sweep left no
  traceback, no `ERROR` line, no `finally`, and a stale status file. Six runs died at
  61, 64, 85, 99, 136 and 148 seconds with nothing in any log.

  `run_orchestration.py` now records `session_pid` at start-up, and the hook stops the
  sweep only when the ending session's `CLAUDE_PID` matches it. A sweep with no
  recorded owner is still stopped, so pre-existing and scheduled sweeps do not become
  unreapable; a sweep with an owner whose ending session cannot be identified is left
  alone, because guessing there is what caused this.

- **`--status` reported dead sweeps as running for up to five minutes, and blocked new
  ones.** Liveness was decided purely by heartbeat freshness, and a heartbeat is only
  refreshed once a minute against a five-minute staleness window. Inside that gap
  `--status` printed `running (pid 25860)` against a process that did not exist, and
  — worse — the start-up guard refused a new sweep with `already running — not
  starting a second` against dead pid 26616.

  A recorded pid that is definitively gone now vetoes the answer. It stays a veto
  rather than the primary signal, which keeps the original reasoning intact: a
  recycled pid can only make a dead sweep read as alive, and the stale heartbeat still
  catches that. The probe goes through `process_liveness.is_alive` (Win32
  `OpenProcess`), not `os.kill(pid, 0)`, which is not portable to Windows.

- **`/hireshire:find-jobs` had no teardown at all, and now shares the monitor's.** It
  ran `orchestrate.py --once` through `run_engine.py` — a second launcher that
  registered nothing and watched nothing. A find-jobs sweep was invisible to
  `--status`, unreachable by `--stop` (which reported *"not running; nothing to stop"*
  while a full sweep plus up to four `claude -p` scorers ran), and outlived the session
  that started it.

  Both skills now run the same program: `hireshire.sh --sweep` is one cycle of
  `--monitor`. The OS scheduler entry uses `scripts/run_orchestration.py --once` for
  the same reason. `orchestrate.py` remains as the developer entrypoint; nothing in
  the plugin invokes it.

  Consequence worth knowing: find-jobs now inherits the single-instance guard, so it
  declines rather than putting a second writer on the same SQLite database while a
  recurring sweep runs.

- **The session watchdog reacted up to a minute late and orphaned its children.** It
  shared the heartbeat's 60-second loop, though the two measure unrelated things, and
  it called `os._exit` — leaving the scorer's `claude -p` children re-parented and
  running until their 600-second timeout. It is now a separate task at 5 seconds and
  kills its own process tree. Measured: session force-killed, sweep and all three
  processes gone in 3 seconds, status cleared.

- **The recurring sweep did not stop with the session, and now it does.** Users are
  told it is a session watcher rather than a background service; on Windows that was
  untrue. An orphan there is re-parented in silence — no process group, no SIGHUP —
  so nothing signalled the sweeper when Claude Code closed. It happened twice in one
  afternoon, the second time with seven jobs shortlisted and `enable_applier` on: a
  process one step from submitting real applications with nobody watching, reachable
  only through Task Manager. Two mechanisms now end it, and neither is sufficient
  alone:

  - a **`SessionEnd` hook** runs `hireshire.sh --session-end`, which reuses `--stop`.
    It filters on the payload's `reason`, so a `/clear` — which leaves the user in a
    live session — does not kill their sweep. It cannot fire if Claude Code is
    force-killed or crashes.
  - the sweeper's **heartbeat watches `CLAUDE_PID`**, the pid Claude Code publishes for
    itself, and exits within one interval once it is gone. This is the half that
    survives a crash. If that variable is absent the watchdog does not arm at all — a
    plain terminal or the scheduled route must never be guessed at.

  Coverage is deliberately partial: a closed CLI and a crash, but **not** killing only
  the background Bash task, which waits for `SessionEnd` or `--stop`. The first version
  tried to cover that too by exporting `$$` and `$PPID` from the launcher, and it
  **killed a healthy sweep 60 seconds after it started**. Git Bash is MSYS and MSYS
  keeps its own pid namespace — `ps` reports PID 1684 for a shell Windows calls WINPID
  14072 — while the liveness probe uses Win32 `OpenProcess`, which understands only
  Windows pids. It read two meaningless numbers as dead. Walking the process tree is
  not a fix either: the ancestry measured under the VS Code extension is
  `python → bash → bash → bash → claude.exe → Code.exe`, three shell levels with
  nothing pinning that depth. A test now asserts a live pid reads as live, which is
  what nothing checked before.

  Shutdown is immediate rather than graceful: every job already judged is in `matches`,
  so what is abandoned is the employer batch in flight, not work anyone paid for. An
  in-flight `claude -p` apply subprocess is taken down with it — an orphaned one would
  go on submitting applications, which is the whole point. Nothing needs killing above
  the leaf: each parent in the re-exec chain is blocked in `subprocess.run` and unwinds
  on its own. `--stop` remains the manual backstop.

- **`--stop` could report success while leaving the sweeper running, on macOS and
  Linux.** It ran `pkill -TERM -P <pid>`, which signals the *children* of a pid and
  never the pid itself, then gated the `SIGTERM` fallback on pkill having **failed**.
  So whenever pkill succeeded — whenever the sweep had a child — the sweeper survived
  and was reported as stopped. The sweep has a child in exactly one situation: while
  `claude -p` is driving a browser through the apply phase, so the stop path failed at
  the one moment that mattered most. It now always signals the recorded process, with
  the child sweep as an addition rather than a substitute.

- **The apply phase never ran.** `orchestrate._launch_skill` passed the SKILL.md
  body to `claude -p` as a positional argument. A SKILL.md opens with `---`
  frontmatter, which the CLI parses as an option, so every unattended apply phase
  died with `error: unknown option '---\nname: apply...'` and exit code 1 — visible
  only as one ERROR line in a multi-megabyte log, while the status file went on
  reporting `apply_enabled: true`. The prompt now goes on stdin, and a test asserts
  it never appears in argv.

- **Jobs skipped by `exclude_companies` were invisible.** Those employers need an
  account login, so the applier genuinely cannot complete them — but it dropped
  them without a word. One real run shortlisted three jobs, all at an excluded
  employer, and would have reported nothing to do without saying why. The apply
  skill now always prints an **Apply manually** section with company, title and URL.

### Changed

- **`funnel.rerank.min_score` now defaults to 3.0, up from 0.0.** 0.0 is the
  cross-encoder's own decision boundary, which is permissive enough that the cutoff
  rarely bound — most of what reached the reranker went on to cost an LLM call, and
  the budget, not the cutoff, decided who got scored. 3.0 is the operating point
  every study in `analysis/results/` was run at; on that corpus it admits 61 jobs a
  sweep, well inside `top_k`. The docs already described the funnel this way
  (`docs/sys_arch.md`), so this brings the code and the shipped YAML in line with
  them. **The number is still a raw logit and still personal** — it is not a
  percentage, it means nothing if `rerank.model` changes, and
  `scripts/calibrate_cutoffs.py` remains the way to derive your own. Existing
  installs keep whatever is in their own `config/matcher.yaml`; this changes new
  installs only. Anyone whose sweeps come back emptier than before should lower it.

- **`funnel.encoder.threshold` now defaults to 0.30, up from 0.25.** The title gate
  stays a recall net — everything in its comment block still holds, in particular
  that tightening it saves no money, since both title gates run locally and only
  `rerank.min_score` decides what reaches the LLM. What it buys is CPU seconds and
  skipped detail fetches on Workday/BambooHR, paid for in recall at the stage that
  sees the least. `docs/sys_arch.md` already documented 0.30. Like the cutoff, this
  does not transfer between users: outside tech, titles bunch into a narrow cosine
  band, and max-over-targets loosens the gate on its own as `targets` grows.

- **Scale numbers corrected everywhere — they were understated by ~60%.** The shipped
  slug lists had grown well past the figures in the docs: **40,068** boards, not
  24,754, and a default sweep of **15,868**, not ~10,000 (Workday 12,884, BambooHR
  11,316, Greenhouse 8,333, Lever 4,369, Ashby 3,163, direct portals 3). Updated in
  the README, `CLAUDE.md`, the setup skill's board-type prompt, and both the
  `plugin.json` and `marketplace.json` descriptions. The README table now also lists
  the direct portals, which were enabled by default but absent from the docs, and says
  plainly that the figure is the shipped list rather than a promise of live boards.

- **Install size was overstated ~2×.** Measured: venv ~1.2 GB plus ~350 MB of models
  (all-MiniLM-L6-v2 88 MB, ettin-reranker-68m 265 MB). The docs said 2.5–3 GB in six
  places; all now say ~2 GB.

### Added

- **Jobs that ask for far more experience than you have are skipped, for free.**
  Postings state a minimum in plain text — "5+ years", "at least five years",
  "3-7 years" — so it is read with a pattern match rather than a model. No API call,
  no cost, and it runs once per group of duplicate postings rather than once per job.

  It only ever filters from **below**: "5-10 years" means "at least 5", and you are
  never dropped for being over-qualified. Anything within six months of the stated
  bar still counts as a match, "preferred" is treated the same as "required" because
  employers use the words interchangeably, and the roughly one posting in four that
  states no requirement is always kept.

  **Off until `/hireshire:setup` asks you.** Setup reads a number off your resume and
  makes you confirm it, because every skipped job is measured against that one value
  and a value set two years low would quietly cost you two years' worth of jobs.
  Re-run setup to change it — though jobs already skipped stay skipped, the same way
  raising the relevance cutoff does not bring back what it rejected.

  Measured against a real sweep: of 61 jobs that got past the relevance cutoff, it
  skips 19. Nine of those nineteen had actually been scored, and the best of them
  managed 31 out of 100 against a shortlist bar of 65-75 — while every job the
  scorer rated 53 or higher survived the filter untouched. The
  new `yoe_required` column in the all-jobs CSV records what each posting asked for —
  on **every** job, whether or not the filter is switched on, so you can see what
  turning it on would have cost you before you do.

- **`hireshire.sh --stop`.** The recurring sweep is meant to end with the session
  that started it; on Windows it has outlived one more than once, leaving a sweeper
  on the database reachable only through Task Manager. `--stop` kills the recorded
  process **tree** — the monitor re-execs twice, so the pid on record is a leaf and
  killing it alone strands its parents — then clears the status file.

- **Every sweep now writes two HTML reports, and the skills publish one of them.**
  The scoring prompt returns four rationales per job — core skills, experience,
  education, and the reasons for and against — and until now all of it went into
  the `matches` table's `raw_json` and was never rendered anywhere. A run that
  shortlisted nothing left the user with a CSV of numbers and no way to see that
  the best job scored 61 because the judge discounted project work against
  professional work. That sentence was in the database the whole time.

  `<run>/<stamp>_matching.html` is the reasoning, ranked, followed by every job
  that was considered and never scored — the all-jobs CSV in a form a person can
  read, with a filter box over it. `dashboard.html`, at the root of the results
  folder, is every sweep the install has ever done: employers, postings, reranked,
  scored, shortlisted, applied.

  The **engine** writes both, not the agent. That is what makes them appear on
  unattended sweeps too, costs no tokens, and keeps the skills reporting numbers
  they were handed rather than numbers they assembled.

- **The dashboard is live.** It rewrites itself every few seconds during a sweep
  and reloads itself in the browser while one is running, so the ~20 minutes of
  rate-limited waiting is finally legible. The meta refresh is armed only while
  the pipeline's own run row is absent — a finished run stops reloading rather
  than looking like one that never ended.

  What can stream is stated honestly on the page: the scrape counts do, the
  reasoning cannot. Top-K is a decision across the whole sweep, so no job is
  scored until every job has been seen, and all the rationales land in the last
  couple of minutes.

- **One rolling artifact instead of a trail of them.** `/hireshire:find-jobs`
  republishes the match report as the sweep advances (four or five times, off
  milestone lines in the engine log); `/hireshire:start-orchestration` republishes
  once per completed cycle, deliberately not on milestones — an unattended sweep
  running for hours should not put several messages a cycle into the session. Both
  find the existing artifact by its stable title and republish to the same URL, so
  the user keeps one bookmark.

### Changed

- **Setup no longer asks permission for its own plumbing.** A first-time install
  opened roughly fifteen "Allow this command?" dialogs before the user saw a single
  job. Claude Code matches permission rules against the exact command string, and
  setup ran its Python by writing a heredoc to a temp file — so no two calls ever
  matched, no allowlist rule could cover them, and "don't ask again" never stuck.

  Every setup action is now a fixed-argv subcommand of `scripts/setup_cli.py`, and a
  `PreToolUse` hook (`scripts/approve.py`) recognises those shapes and approves them.
  A fresh setup should now prompt for nothing.

  **The questions setup asks are unchanged** — locations, target roles, exclusions,
  threshold, jobs per run, boards, poll interval, scoring backend, auto-apply — as is
  the step that shows the generated targets and profile back for editing before
  anything is written. Only the mechanism that saves the answers changed.

  The guard is deliberately narrow. It refuses anything carrying a shell operator,
  redirection or substitution; it refuses a launcher that is not this install's; and
  it never approves the launcher's bare `<script.py>` form, which runs an arbitrary
  file. Anything it does not recognise prompts exactly as before. In `/hireshire:apply`
  only the browser tools that *look* — navigate, snapshot, screenshot — are approved;
  clicking, typing and uploading still ask, because with `dry_run` off those are what
  send a real application.

### Fixed

- **The reranker was choosing what to score almost at random.** Measured over one
  real sweep, the correlation between a job's rerank score and the LLM score it
  eventually received was **+0.16**. Two causes, compounding:

  `max_doc_chars` was 1,200, but **41% of job descriptions do not reach their first
  requirements heading until after character 1,200** (median offset: 1,094). For
  those, the cross-encoder scored company boilerplate and never saw the duties.
  And `cross-encoder/ms-marco-MiniLM-L-6-v2` caps at 512 tokens and is trained on
  ~6-word search queries, while the query here is a ~1,400-character candidate
  profile — so the pair overflowed and the document tail was discarded regardless
  of the setting.

  The cost was concrete: of 100 LLM calls in that sweep, 21 went to plainly
  off-target engineering roles (all scoring ≤17) and 31 to copies of one
  requisition, while genuinely good matches sat unscored at ranks 243–266.

  Reranking is now a two-stage cascade of Ettin models (ModernBERT, 8,192-token
  window, Apache 2.0): `ettin-reranker-17m-v1` reads every description,
  `ettin-reranker-68m-v1` re-reads the best 500. `max_doc_chars` is 15,000, which
  covers 99.8% of real postings in full.

  **This costs real time.** Measured on a 16-core CPU over a 7,021-job sweep:
  ~32 min for stage 1 plus ~9 min for stage 2, against a sweep that is otherwise
  rate-limit-bound at ~20 min. The published throughput figures for these models
  are measured on short passages and do not survive contact with 1,200-token job
  descriptions. `max_doc_chars` is the dial, and it is cheaper to turn down than it
  looks: 4,000 chars cuts stage 1 to ~12 min while still including the requirements
  section for 96.9% of postings (3,000 → ~9.5 min / 92.9%; 2,000 → ~6.8 min /
  82.0%). Stage 1 only has to be right at `refine.depth`, not at `top_k`. Batch
  size makes no measurable difference.

- **One employer could consume the whole LLM budget.** A single Townsquare Media
  requisition, posted for 31 locations, took 31 of 100 budget slots. Repeat
  postings are now grouped by company and normalised title; one representative is
  scored and the verdict is copied to every sibling. Nothing is discarded — all 31
  keep their own location and link in the new all-jobs CSV — and the shortlist that
  `/hireshire:apply` reads carries one row per requisition, so 31 copies cannot
  become 31 applications. Different titles at one employer stay independent.

### Added

- **`<stamp>_results_all_jobs.csv`** beside the existing results files: every job
  that reached the matcher, with four score columns kept deliberately separate —
  `bi_score` (cosine), `cross_score_wide` and `cross_score_refined` (logits from two
  *different* models), and `llm_score`. Sorted best-first. A budget drop shows a
  **blank** `llm_score` rather than the `0` stored internally, because printing that
  zero reads as "the model judged this worthless" and is exactly what disguised the
  reranker fault. `last_run.json` gains an `all_jobs_csv` pointer; the `json`
  pointer `/hireshire:apply` reads is unchanged.
- The bi-encoder score is now persisted. It was previously computed, compared to
  the threshold and thrown away, which made a `title_low_relevance` drop
  unexplainable after the fact.
- `funnel.rerank.refine` and `funnel.dedupe` config blocks. `refine.depth` must be
  `>= top_k`, validated at config load — below it, the tail of the budget would be
  filled by comparing the two rerank stages' incomparable scores.

### Changed

- `matches` and `pipeline_results` gain `encoder_score`, `rerank_score_wide` and
  `rerank_score` columns. Existing databases are migrated additively on connect.
- `requirements-core.txt` now floors `sentence-transformers>=5.0` and names
  `transformers>=4.48` explicitly — ModernBERT does not load below it.

### Known issues

- **`threshold: 85` is effectively unreachable and is NOT fixed here.** The scoring
  rubric caps a category at 50% for each unmet mandatory requirement, so a single
  missing item puts the ceiling at 80 before anything is credited. In the sweep
  analysed above, 99 of 100 jobs hit a cap; the one that did not scored 73, the run
  maximum. Recalibrating the rubric and the threshold is a separate change.

## [0.2.4] — unreleased

### Fixed

- **`/hireshire:start-orchestration` reported sweeps that were not running.** The skill
  announced that orchestration had started on the strength of having been invoked,
  because a plugin monitor was supposed to start it. Monitors are an experimental
  component that is skipped on hosts where the Monitor tool is unavailable, so on some
  interfaces nothing started — and the skill had no way to notice. Users were told a
  sweep was live while the log directory stayed empty.

  Told to start one anyway, a session improvised a detached
  `nohup … orchestrate.py --now & disown`, which was wrong three ways: it ignored the
  user's `poll_interval_hours` (`orchestrate.py --interval` defaults to 4 hours and
  never reads their config), it outlived the session it had just promised to stop with,
  and nothing stopped a second copy — one test machine ended with two sweepers writing
  the same SQLite database.

  The monitor is gone. `/hireshire:start-orchestration` now starts
  `hireshire.sh --monitor` as a background task, **confirms it with `--status`**, and
  reports only what that returned — including saying plainly when it could not start
  one. Same behaviour on the terminal, the Claude app and the VS Code extension.

### Added

- **`hireshire.sh --status`** — whether a recurring sweep is running, its interval, its
  last sweep and when the next is due. Answers the question the skill used to guess at,
  and is there for the user to ask directly at any point.
- **A single-instance guard.** `hireshire/orchestration_status.py` records a heartbeat
  in the data directory; a second `--monitor` reports the running one and exits instead
  of duplicating the sweep. Liveness is heartbeat freshness, not a PID probe —
  `os.kill(pid, 0)` is not portable to Windows and a recycled PID reads as alive.

### Changed

- **Setup asks with selectable options.** Nothing in the skill named `AskUserQuestion`,
  so whether a user got tappable choices or a wall of numbered prose was left to
  judgment and varied between runs of the same skill. Locations, posting age, threshold,
  jobs per run, job boards, scoring backend, effort, poll interval and auto-apply now
  specify it, recommended option first. The resume path and the target-role correction
  stay free text, where a menu would constrain a genuinely open answer.

## [0.2.3] — unreleased

### Fixed

- **Setting up in the Claude desktop app configured a different plugin from the one
  that runs.** Claude Code resolves `${CLAUDE_PLUGIN_DATA}` from the plugin
  *identifier*, and the identifier is not the same on every interface: the terminal
  and the VS Code extension report `hireshire@hireshire` and get
  `data/hireshire-hireshire`, while the desktop app reports the plugin as an inline
  source and gets `data/hireshire-inline`. Claude Code expands that placeholder
  inside skill content, so `/hireshire:setup` run in the desktop app wrote the
  generated search profile into a directory no engine run ever reads.

  Nothing failed. `_load_search_profile` logged one line and returned `""`, which
  makes `Reranker.usable` False — so the cross-encoder, the funnel's only real
  precision stage, was skipped for every sweep afterwards and the LLM budget went on
  unranked jobs. Two changes close it:

  - `resolve_dirs()` now derives DATA from ROOT's install path whenever that layout
    is provable, **outranking** `CLAUDE_PLUGIN_DATA` instead of deferring to it. ROOT
    is the same on all three interfaces, so the derivation is too. The environment is
    still honoured for a ROOT that is not an install — a checkout or a `--plugin-dir`
    load — where there is nothing to derive from.
  - Skills no longer name a data directory. `hireshire.sh --paths` prints `ROOT=` and
    `DATA=`, works before the venv exists, and is now the only supported way for a
    skill to find DATA. A test fails the build if `${CLAUDE_PLUGIN_DATA}` reappears in
    any SKILL.md. `${CLAUDE_PLUGIN_ROOT}` is unaffected and still used everywhere.

  The practical effect: setup in the Claude app, then sweep from the terminal, and
  both use one config, one `profile.md` and one `hireshire.db` — so `seen_jobs` is
  shared and the second run does not re-score what the first already retired.

  Installs that ran setup from the desktop app before this release should re-run
  `/hireshire:setup`; a `data/hireshire-inline` directory left behind is inert and
  can be deleted.

## [0.2.2] — unreleased

### Fixed

- **The sweep interval could not be saved.** Setup asked "how often should this
  re-run?" and then discarded the answer: `poll_interval_hours` is a real
  `ScraperSettings` field, shipped in `scraper.yaml` and read at runtime by
  `scripts/run_orchestration.py` — but it was missing from the config writer's
  whitelist, so the write was rejected and every install swept on the default 4 hours
  no matter what the user chose. It is now writable, and bounded above zero so a
  continuous sweep cannot be configured. A new test rejects any `phase.field` the
  setup skill names that `write_config` would refuse; the existing drift guard only
  checked the skill's field table, which was correct, and so missed this.

- **The 0.2.1 data rescue could move files out of unrelated directories.** Mandatory
  upgrade for anyone running the plugin from a directory rather than the marketplace.
  `legacy_data_dirs()` scanned every sibling of the install directory for a stranded
  `data/` folder — correct when siblings are other version folders under
  `cache/<marketplace>/<plugin>/<version>`, catastrophic from a checkout or a
  `--plugin-dir` load, where the siblings are whatever else the user keeps beside it.
  A neighbouring project with a `data/` directory matched, and `rescue_stranded_data()`
  — which moved *everything* except the venv rather than the `MIGRATABLE` allowlist it
  already had — moved it away. Both halves are fixed: the sibling scan now runs only
  when the install layout is provable, and only allowlisted names are ever moved.
- **The first session no longer sits silent for minutes.** The SessionStart hook ran
  `--bootstrap`, so a fresh install spent ~4 minutes downloading 2.5 GB before the user
  could be told anything — and the warning that explains the wait lives in the setup
  skill, which cannot run until the hook finishes. The hook now runs a new `--check`
  mode that recovers stranded data, reports readiness in one line and installs nothing,
  returning in well under a second. The download moved to the setup skill, which
  announces it first. `find-jobs` and `apply` carry the same warning, since they can
  trigger an on-demand install through the launcher.

## [0.2.1] — unreleased

Scoring never worked in 0.2.0. Every run scraped normally, sent its budget of jobs
to the scorer, failed all of them, and reported "0 new matches" — so this release is
mandatory for anyone who installed 0.2.0.

### Fixed

- **Scoring on a Claude subscription never worked.** The `claude_code` backend passed
  `--json-schema` the *path* to a temp file, but the flag parses its argument as JSON
  — so every call failed with `not valid JSON: Unexpected identifier "C"` (the drive
  letter of `C:\Users\...`). The schema is now passed inline.
- **A broken backend permanently retired the jobs it failed on.** `api_error` was not
  in `_RETRYABLE_SKIP_REASONS`, so every job a failed scoring call touched was written
  to `seen_jobs` and would never be scored again — fixing the backend could not bring
  them back. Scoring failures are now retryable, and `SeenStore` releases jobs retired
  this way on the next run, so installs affected by the bug above recover on their own.
- **A dead backend now says so.** A circuit breaker stops the run after five
  consecutive scoring failures and reports the error text, instead of spending the
  whole budget on a backend failing every call and finishing with a summary that reads
  like a normal empty result.
- **Everything the skills wrote was going into the install directory.** Claude Code
  sets `CLAUDE_PLUGIN_DATA` for hooks but not for the Bash calls a skill makes, so the
  engine resolved DATA to `ROOT/data` for every path the skills took: the user's
  config, the SQLite DB, the generated profile, the logs — all in a directory replaced
  wholesale on the next update. DATA is now *derived* from the install path when the
  environment is silent (`hireshire/plugin_dirs.py`), and `scripts/bootstrap.py` moves
  anything stranded by an earlier version into the real data directory at session
  start. **Nobody loses their setup answers or their job history on this update.**
- A `str_list` config field now accepts a bare string, so a location given as
  `"united states"` is read as a one-item list instead of being rejected. Commas are
  deliberately not split on: "San Francisco, CA" is one location.
- `funnel`'s writable `threshold` key is renamed `encoder_threshold`. Both phases
  write `matcher.yaml` and both had a `threshold` — 0-100 for the LLM, 0-1 for the
  cosine recall net — asked two steps apart during setup. Writing the LLM's value into
  the funnel validated cleanly and silently rejected every job in the sweep. Both
  settings are range-bound now, so a hand-edited YAML is caught too.
- The config writer rejects a nested patch (`{"title_filter": {...}}`) with an error
  naming the flat call that works, and `skills/setup/SKILL.md` documents the flat keys
  it actually takes rather than the dotted YAML paths — which is what produced the
  failed writes users saw during setup.

## [0.2.0] — 2026-08-12

### Added

- **A job-search folder that belongs to the user.** Make a folder, put your resume
  in `resume/original/`, open Claude Code there, and every run's results are written
  back into it. `/hireshire:setup` adopts the folder it was launched from, creates
  the layout if it does not exist, and copies in a resume from anywhere else on disk
  so the whole search is one directory to back up or delete.
- `scraper.workspace_dir` — the folder's absolute path, captured **once** at setup.
  It is stored rather than derived from the working directory because a plugin's cwd
  is whatever project the user is in: a session launched from somewhere else must
  still write to the folder they chose. It sits in `scraper.yaml` for the same
  reason `poll_interval_hours` does — the monitor needs it and cannot reference
  `${user_config.*}`.
- `hireshire/workspace.py` — creates the workspace and installs the resume. It
  validates the PDF *before* copying, so a scan fails while the user can still pick
  another file instead of leaving a rejected file in their folder; it never
  overwrites an existing resume; and it refuses a workspace inside the install or
  data directories, which would be silently erased on the next update.
- `${CLAUDE_PLUGIN_DATA}/last_run.json` — a fixed pointer to the newest run, so
  `/hireshire:apply` no longer has to guess where the results root is.
- `FieldSpec.normalise` in the config writer. The pydantic models validate a copy of
  the document, so a `field_validator` could reject a value but never clean one —
  which meant a path pasted with the quotes Windows' "Copy as path" adds was stored
  with them. Path fields now normalise on the way in.

### Changed

- Results move from `${CLAUDE_PLUGIN_DATA}/results/<run_id>/pipeline_results.csv` to
  `<workspace_dir>/hireshire_run_results/<stamp>/<stamp>_results.csv`, and the JSON
  alongside it likewise. **Existing installs are unaffected until they re-run
  `/hireshire:setup`** — an empty `workspace_dir` still writes to the data directory.
- The per-run folder and the files in it are stamped `YYYY-MM-DD_HHMMSS` in **local**
  time, because a human reads them off a directory listing. `run_id` is unchanged
  (UTC): it keys five tables, and a local-time key goes backwards for an hour at the
  end of DST.
- A locked CSV now says so on the console instead of only in the log. The file lives
  somewhere users actually open it, so Excel holding it is routine rather than
  theoretical; results still go to the database and the run still completes.
- `_finalise_pipeline` no longer fails a run that succeeded — its JSON write is
  guarded, where before an `OSError` after every row was already written reported the
  whole sweep as failed.

## [0.1.0] — unreleased

First release. Repackages the HireShire pipeline as a Claude Code plugin.

### Added

- Four skills: `setup`, `find-jobs`, `start-orchestration`, `apply`.
- **Scoring on the user's Claude subscription** via a `claude_code` matcher
  backend that shells out to the local Claude CLI with `--output-format json
  --json-schema`, so no API key is needed. The BYO-key path is unchanged.
- **Cross-encoder rerank + top-K budget.** Candidates are ranked against a
  generated candidate profile and only the best `funnel.top_k` are LLM-scored, so
  scoring cost is bounded by a number the user picks instead of by wherever a
  similarity threshold lands. Jobs that miss the cut are recorded as
  `rerank_below_top_k` and stay eligible for later runs.
- **Resume expansion at setup**: one LLM call derives adjacent job titles, exclude
  keywords, and a transferable-skills profile from the user's own resume. This is
  what makes the plugin work for any field and what catches jobs worded
  differently from the resume.
- `enabled_platforms` — Workday and BambooHR are opt-in, so the default sweep is
  ~9,974 companies rather than 24,754. Disabled boards' slug files are never read.
- Seed-plus-delta slug lists, so a plugin update can ship newly-dead slugs
  without erasing what the local install learned.
- `hireshire/paths.py` — all state resolves under `${CLAUDE_PLUGIN_DATA}` instead
  of the working directory.
- `hireshire/config_writer.py` — whitelisted, comment-preserving YAML writes,
  validated against the pydantic settings models before anything reaches disk.
- `SessionStart` hook that builds a venv in the data directory, and a session
  monitor for recurring sweeps.
- `scripts/hireshire.sh`, the single launcher every entry point goes through. It
  resolves a working interpreter by *executing* candidates rather than checking
  PATH, which is what makes the plugin run on macOS (no bare `python`) and on
  Windows (where a Microsoft Store `python3` stub exists on PATH but does not
  work). Windows requires Git Bash.

### Changed

- The results CSV now carries `location`, `posted_at` and `rerank_score`.
  `posted_at` is when the employer posted the job; the old `processed_at` field
  was when we saw it, which is a different question.
- Shipped config carries no personal details and no field-specific keywords or
  semantic targets — a user hunting non-engineering roles no longer inherits a
  software-engineering filter.
- The applier uploads the user's own resume and no longer depends on a tuned one.
- Playwright MCP tool names corrected to the `mcp__plugin_hireshire_playwright__*`
  namespace that plugin-bundled servers actually get.

### Removed

- The resume tuner and its LaTeX toolchain (~1,970 LOC), the `browser-use`
  applier, the web dashboard, and the browser-driven `/scrape-direct` path. The
  plain-HTTP Apple/Google/Intuit scrapers are kept.
- The no-software-engineering company prune, which was a single-operator
  assumption that had no place in a plugin meant for any field.
- 13 dependencies: pdfminer.six, browser-use, playwright, fastapi, uvicorn,
  sse-starlette, langgraph and the langchain adapters.
