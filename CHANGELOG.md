# Changelog

All notable changes to this plugin are documented here. Versions follow
[semver](https://semver.org/); users only receive an update when `version` in
`.claude-plugin/plugin.json` is bumped.

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
