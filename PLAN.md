# HireShire — Claude Code plugin

Build plan. This repo starts empty; the engine is copied from the source repo (not forked).

- **Source repo**: `D:\Atreya\College\Projects\HireShire` — read-only reference. **Make no
  changes there.** All work happens in this repo.
- **Repo name**: `HireShire-plugin`. **Plugin `name` field must be `hireshire`** — it becomes
  the namespace on every invocation (`/hireshire:setup`).

---

## Context

HireShire is a five-phase job-search pipeline (scrape → match → tune → apply, plus a web
dashboard) built for one operator: ~6 YAML config files, a React dashboard, and a
tuner/applier that assume you wrote the LaTeX bullet corpus yourself. This repo releases it
to people who will never open a YAML file or a terminal.

Delivery is a **Claude Code plugin**, decided after ruling out the alternatives:

- A **claude.ai / Desktop chat skill** is free-tier accessible, but its sandbox has *no
  network access at all* ("Internet access: completely disabled; no outbound network requests
  permitted") — the board-API scraper physically cannot run there. Dead end.
- A **hosted webapp** would make us controller of every user's resume (PII), put ~24k board
  requests on one datacenter IP (rate-limit risk to the project's core asset), put every LLM
  call on our bill, and — because a shared sweep means every user sees a posting at the same
  instant — destroy the apply-early advantage the streaming pipeline exists to create.
- The **plugin** inverts all of that: resume stays on the user's disk, each user scrapes from
  their own residential IP, and scoring runs on their own Claude subscription.

Accepted trade-off: Claude Code needs **Pro or Max**, so free-tier users are excluded. A
BYO-OpenAI-key path is the escape hatch.

**Differentiator, and it belongs in the README's first paragraph:** competitors exist
(`proficiently-claude-skills`, `jobpilot`, `jobs-for-ai-agents`) and jobpilot also runs on the
user's Claude subscription — but they all scrape LinkedIn/Indeed aggregators. This scrapes
**direct ATS board APIs across 24,754 employer boards**. First-party data, no aggregator lag,
no anti-bot arms race.

### Distribution — no clone, no review

`/plugin marketplace add <owner>/HireShire-plugin` → `/plugin install hireshire@hireshire`.
Any git repo can serve as a marketplace; **no approval process gates this**. Review applies
only if we later opt into the community marketplace (`anthropics/claude-plugins-community`),
which runs `claude plugin validate` plus automated safety screening — that buys discoverability,
nothing else. Run `claude plugin validate` locally regardless.

Two mechanics shape everything below:

- `${CLAUDE_PLUGIN_ROOT}` = install dir. **Replaced wholesale on every update**; its name is a
  version string.
- `${CLAUDE_PLUGIN_DATA}` = `~/.claude/plugins/data/<id>/`. **Survives updates**, and is
  documented explicitly for Python virtualenvs. Venv, SQLite DB, user config, tuned PDFs and
  CSVs live here.

---

## What ships

| Skill | Does |
|---|---|
| `/hireshire:setup` | One-time guided setup + first-run downloads. |
| `/hireshire:find-jobs` | Scrape + match → CSV. `orchestrate.py --once --no-tuner`. |
| `/hireshire:start-orchestration` | Full streaming pipeline on an interval. |
| `/hireshire:apply` | The existing `apply.md`, bundled. Invoked by orchestration, also runnable alone. |

Tuner and applier are **optional, chosen at setup** (`enable_tuner` / `enable_applier` — both
keys already exist and `orchestrate.py` already reads them as defaults). Matching always runs.
`start-orchestration` respects whichever stages are enabled, degrading to scrape → match.

### Repo layout

```
HireShire-plugin/
├── .claude-plugin/{plugin.json, marketplace.json}   # self-hosting marketplace
├── skills/{setup,find-jobs,start-orchestration,apply}/SKILL.md
├── hooks/hooks.json            # SessionStart → bootstrap
├── monitors/monitors.json      # on-skill-invoke:start-orchestration
├── scripts/bootstrap.py        # venv + pip install into ${CLAUDE_PLUGIN_DATA}
├── .mcp.json                   # Playwright MCP for the applier
├── hireshire/                  # engine, copied from source repo
├── config/                     # default YAMLs, curated bad_slugs.json, *_companies.json
├── scraper.py  matcher.py  tuner.py  orchestrate.py
├── requirements-core.txt  requirements-applier.txt
└── README.md  CHANGELOG.md
```

**Copy from the source repo**: `hireshire/{config,http_client,models,scrapers,funnel,matcher,
tuner,storage,direct}`, `scraper.py`, `matcher.py`, `tuner.py`, `orchestrate.py`, `config/*`,
and `.claude/skills/apply.md`.

**Do not copy**: `webapp/`, `frontend/`, `run_web.py`, `hireshire/applier/filler.py`,
`applier.py`, `.claude/skills/scrape-direct.md`. (Keep the plain-HTTP Apple/Google/Intuit
handlers in `hireshire/direct/`; drop only the browser-driven Microsoft/Meta path.)

---

## Work items

### 1. `ClaudeCodeBackend` for the matcher — `hireshire/matcher/scorer.py`

Scoring runs on the user's Claude subscription via the local `claude` CLI. Mirror the existing
`ClaudeCodeOptimizerBackend` in `hireshire/tuner/optimizer.py:232` — same `shutil.which("claude")`
guard, `asyncio.create_subprocess_exec` + `wait_for` timeout, non-zero-exit handling, and
`request_interval_s` sleep under the semaphore.

The one real difference: the matcher's `LLMBackend.call` returns a **`ScoringSchema`**, not a
`str`. Get that natively rather than regex-scraping prose. These flags are confirmed present in
the installed CLI (`claude --help`):

```
claude -p --system-prompt <rubric> --model <settings.model> --effort <settings.effort> \
          --output-format json --json-schema <ScoringSchema.model_json_schema()>
```

`--effort` accepts `low|medium|high|xhigh|max` — this is the user-selectable "thinking level".
Write the schema to a temp file if the CLI rejects a large inline value. Parse the `json`
envelope, then `ScoringSchema.model_validate`. On parse failure, raise so the existing per-job
error path records a skip rather than killing the run.

Register `"claude_code"` in the matcher's backend factory, exactly as `_OPTIMIZER_BACKENDS`
does at `hireshire/tuner/optimizer.py:272`. Reuse the same `effort`/`model` values for the
tuner's `evaluator_provider` / `optimizer_provider` so tuning runs on the subscription too.

### 2. Config additions — `hireshire/config.py` (`MatcherSettings`)

- `effort: str = "medium"`, validated against `low|medium|high|xhigh|max`
- `claude_cli_timeout_s: int` — copy the field `TunerSettings` already has
- Allow `provider: claude_code`

### 3. CSV fields — `orchestrate.py`

`_CSV_FIELDS` (`orchestrate.py:71`) has no location and no posting date — `processed_at` is
when *we* saw it, not when it was posted. Emit:

```
title, company, location, posted_at, job_url, relevance_score, job_id, found_at
```

Add `location` and `posted_at` to the record dict in `_bypass_tuner` (`orchestrate.py:78`)
**and** the real tuner result dict, sourced from the `Job` already in hand. Keep the
tuner/applier columns only when those stages are enabled. ~~Write under
`${CLAUDE_PLUGIN_DATA}/results/`.~~

> **Superseded (0.2.0).** Results are written into the user's own job-search folder
> — `<workspace>/hireshire_run_results/<stamp>/<stamp>_results.csv` — recorded once
> at setup as `scraper.workspace_dir`. `${CLAUDE_PLUGIN_DATA}/results/` remains the
> fallback for installs that predate the setting. Everything else (DB, logs, venv,
> config, profile) still lives in the data directory. The cwd rule is untouched: the
> setup skill captures the absolute path, the engine only reads config.

### 4. Board selection — Workday and BambooHR opt-in

BambooHR (8,763 companies, list→detail so **two requests per company**) and Workday (6,017,
POST-based) dominate sweep time. The source repo already special-cases exactly this pair as
`_NO_SWE_PLATFORMS` at `scraper.py:45`. Default them **off**:

| Default on | Boards | Companies |
|---|---|---:|
| ✓ | Greenhouse + Ashby + Lever | **9,974** |
| opt-in | + Workday + BambooHR | 24,754 |

Add `enabled_platforms: list[str]` to `ScraperSettings` (default
`["greenhouse", "ashby", "lever", "direct"]`), filter in `load_config()` so disabled boards'
JSON files are never even read, and gate the dispatch sites keyed off `_PLATFORMS`
(`scraper.py:37`).

Setup presents this as a time trade-off, not a list of platform names — *"add ~15k more
employers, roughly Nx longer per run"* — and **N must come from a real timed run, not a guess.**

Ship `config/no_swe.json` so users who enable those boards inherit the pruning.

**The README must not overclaim**: lead with 24,754 employer boards, but state plainly that the
default sweep is ~10,000 and the rest is one setup answer away.

### 5. Seed-plus-delta slug lists — `scraper.py`

`config/bad_slugs.json` is mutated at runtime *and* must receive our curated updates — but
`${CLAUDE_PLUGIN_ROOT}` is wiped on every update. So split it:

- **Seed** (ROOT, read-only): the curated `bad_slugs.json` / `no_swe.json` we ship
- **Deltas** (DATA, read-write): `user_bad` (they discovered) and `user_recovered`
  (`verify_bad_slugs.py --prune` found reachable again)

Effective set:

```
shipped_bad ∪ user_bad − user_recovered
```

A release then adds new dead slugs without erasing local learning, and a board that came back
online for that user stays enabled. Rework `_load_bad_slugs` / `_save_bad_slugs`
(`scraper.py:48`) accordingly; same rule for `no_swe.json`; `scripts/verify_bad_slugs.py --prune`
writes into `user_recovered` rather than editing the shipped file.

**Do this before first release** — migrating users off a wrong layout later is much worse.

### 6. Bootstrap hook + monitor

`hooks/hooks.json` — `SessionStart`: if `${CLAUDE_PLUGIN_DATA}/venv` is missing, or the bundled
requirements differ from `${CLAUDE_PLUGIN_DATA}/requirements.lock`, create the venv and install,
then copy the manifest across. Idempotent, with a file-hash compare on the happy path so it
costs nothing per session. Point `db_path` at `${CLAUDE_PLUGIN_DATA}/hireshire.db`.

> **Superseded in 0.2.4 — the monitor was removed.** The first constraint below turned
> out to be the fatal one: on interfaces where monitors are skipped, nothing started and
> `/hireshire:start-orchestration` still told the user sweeps were running. The skill now
> launches `hireshire.sh --monitor` as a background task and verifies with `--status`
> before reporting. Everything else in this section still holds — it is the wrapper
> script's contract, and that script is now the only recurring entrypoint.

`monitors/monitors.json` — `name: orchestration`,
`"when": "on-skill-invoke:start-orchestration"`, command runs `orchestrate.py --now --interval N`
through the bootstrapped venv.

Constraints, all documented and all load-bearing:
- Monitors run **only in interactive CLI sessions**, unsandboxed at hook trust level, and are
  **experimental**.
- They **stop when the session ends** — a session watcher, not a daemon.
- A monitor command **cannot reference `${user_config.*}`**; Claude Code rejects the monitor
  outright rather than substituting. So the wrapper script reads `poll_interval_hours` from the
  user's config in `${CLAUDE_PLUGIN_DATA}`.
- Every stdout line becomes a notification → emit **one summary line per completed cycle**
  (`quiet=True`), never the Rich UI.

### 7. Config writer — `hireshire/config_writer.py`

Setup must write YAML without mangling it. Extract the existing whitelisted writer from the
source repo's `hireshire/webapp/routers/config_api.py` — ruamel-based, comment- and
CRLF-preserving (`sequence=4, offset=2`), re-validates against the pydantic settings model
before writing. Extract it so the plugin gets it **without FastAPI**.

### 8. `/hireshire:setup`

Conversational. **Never shows YAML.** Asks ten things:

1. Resume file → `matcher.resume_path`
2. Target location(s) → `scraper.location_filter`
3. Max posting age in days → `scraper.max_age_hours`
4. **Match threshold** — asked as *"how selective? (only near-perfect / strong matches / cast a
   wide net)"* → `matcher.threshold` ≈ 85 / 75 / 60, raw number accepted if given
5. Target roles in plain English → derives `title_filter.include_keywords`/`exclude_keywords`
   **and `funnel.targets`**. This is where match quality comes from — a resume-derived profile
   beats the generic anchors a new user would otherwise inherit
6. Which job boards → `enabled_platforms` (item 4)
7. How often to re-run → `poll_interval_hours`, default 4
8. Scoring backend → Claude subscription (then **model** + **thinking level**) or OpenAI key
9. Enable resume tuning? → `enable_tuner` (prerequisites below)
10. Enable auto-apply? → `enable_applier`, always `dry_run: true` initially

`plugin.json`'s `userConfig` can prompt for plain values at enable time, but the conversational
skill is the source of truth because 4–10 need derivation and validation.

**Warn about first-run cost before starting**, with a real estimate: torch +
`sentence-transformers` (~2.5 GB) for the funnel encoder, MiniLM weights on first encode,
Playwright chromium if the applier is enabled, and the first full sweep against an empty DB.
**Do these downloads during setup, not lazily on the first `/hireshire:find-jobs`** — warm the
encoder with a dummy encode so the model cache is populated while the user still expects to be
waiting.

**Tuner prerequisites — detect, generate, or bring your own.** Only if tuning is enabled:

1. Check `pdflatex` on PATH. Missing → show the install line for their OS and offer to continue
   with tuning off rather than failing setup.
2. Ask whether they have their own `resume_template.tex` / `projects_bullets.yaml`. **If yes,
   take their paths** (`config/tuner.yaml` already has `*_path` keys) and validate with
   `python -m hireshire.tuner.lint`.
3. If not, ship a default `resume_template.tex` and generate a starter `projects_bullets.yaml`
   from their resume via LLM — then **make them review it**, showing the bullets inline. This is
   the one place the product writes resume content; the pipeline's "the LLM selects, it doesn't
   invent" property only holds if the corpus is theirs.
4. Compile once end-to-end and show the PDF path before declaring tuning ready.

**Tier-2 recurring runs**: offer to register an OS scheduler entry running
`orchestrate.py --once` on the interval (`schtasks` / `launchd` / `cron`). This is the only way
polling survives closing Claude. It is a real system change: explicitly opt-in, print the exact
command before running it, and provide a documented way to remove it.

### 9. Dependencies

- `requirements-core.txt` — httpx, pydantic, PyYAML, tenacity, beautifulsoup4, lxml, rich,
  pdfplumber, pdfminer.six, ruamel.yaml, openai, sentence-transformers
- `requirements-applier.txt` — installed only if the user enables the applier
- **Excluded**: fastapi, uvicorn, sse-starlette, langgraph, langchain-* (webapp only), and
  `browser-use`

**Applier without `browser-use`**: bundle Playwright MCP via `.mcp.json` and ship `apply.md` as
`/hireshire:apply`. That skill already uses Playwright MCP, and `orchestrate.py --apply` already
launches it via `claude -p`. Removes a heavy dependency and a whole code path.

### 10. Release hygiene

Set an explicit `version` in `plugin.json` — users then get updates **only** when it's bumped.
(Omitting it pushes every commit at them.) Semver + `CHANGELOG.md`. New slugs ship as a normal
patch release. Refresh the shipped `bad_slugs.json` from the source repo on each release
(run `scripts/verify_bad_slugs.py --prune` there first) — that repo does the real sweeps at
volume, so its curated list beats anything a fresh install accumulates.

---

## Verification

1. Construct the `claude_code` backend directly, score one known job, assert a valid
   `ScoringSchema` comes back.
2. `provider: claude_code`, `effort: low`, `python matcher.py` on an existing scrape run;
   confirm `matches` rows with non-null `relevance_score`.
3. `python orchestrate.py --once --no-tuner`; confirm the CSV has populated `location` and
   `posted_at` (not `processed_at` duplicated).
4. Repeat with `provider: openai` to confirm the BYO-key path is unbroken.
5. Full `start-orchestration` with tuner on: a PDF compiles under `${CLAUDE_PLUGIN_DATA}`. With
   applier on and `dry_run: true`: a form fills and **nothing submits**.
6. `claude plugin validate .`, then `claude --plugin-dir .` and invoke each of the four skills.
7. **Clean-machine test — the real acceptance test.** Fresh user dir, no venv, no repo:
   `/plugin marketplace add` → `/plugin install` → `/hireshire:setup` → `/hireshire:find-jobs`.
   Time it; check the estimate shown matches reality. Anything needing a terminal, a `git clone`,
   or a YAML file is a bug.
8. **Update test**: bump `version`, add a slug to a shipped `*_companies.json`, change
   requirements. Confirm `/plugin update` delivers the slug, the hook reinstalls deps, and the
   DB / tuned PDFs / past CSVs / user YAML in `${CLAUDE_PLUGIN_DATA}` survive. Then the bad-slugs
   merge: mark a slug bad locally, ship a release, verify it's still bad; recover one via
   `verify_bad_slugs.py --prune`, ship again, verify it stays enabled.
9. **Recurring runs**: invoke `/hireshire:start-orchestration`, confirm the monitor starts, one
   summary notification arrives per cycle, and a second invocation doesn't spawn a duplicate.
   Close the session, confirm it stops. If Tier 2 was chosen, confirm the scheduled task fires
   and setup can remove it.
10. **Board selection**: time a default run (~9,974 boards), then with Workday + BambooHR
    enabled. Use the real ratio in setup's warning text.
11. `pytest` + `python -m hireshire.tuner.lint`.

---

## Out of scope

Web dashboard, `browser-use` applier, `/scrape-direct` (Microsoft/Meta), gemini/anthropic API
providers, community-marketplace submission, and any hosted component.
