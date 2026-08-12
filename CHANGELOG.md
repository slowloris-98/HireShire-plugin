# Changelog

All notable changes to this plugin are documented here. Versions follow
[semver](https://semver.org/); users only receive an update when `version` in
`.claude-plugin/plugin.json` is bumped.

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
