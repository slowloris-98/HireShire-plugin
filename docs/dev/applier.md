# The applier

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

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

    **`postings.apply_deferred_at` is a timestamp, and it must never become a count.**
    A deferral writes no status, so unlike the per-company cap there is nothing to
    recompute at render time — the overview cannot otherwise tell a job being retried
    every sweep from one the applier has not reached, which is the whole reason the
    column exists. It is deliberately only *when*: a count here is what something would
    use to retire a job after N failures, and that is the rule `EXPIRED_STATUS` exists
    to state — the trigger is **the window closing, never a count of failures**, because
    a launch failure is a fact about the host and counting them discards a whole sweep's
    shortlist for a transient fault. Holding only a timestamp makes that unrepresentable
    rather than merely unused.

    Two things keep it honest. It is **inert to every existing query** — `apply_status`
    stays NULL, so both halves of `_unapplied` still see the job and the retrying is
    bit-for-bit unchanged; a status here would silently turn the deferral into the
    verdict this path exists to avoid. And `record_applied` and `decline_job` both clear
    it, so the line cannot outlive its cause. In `_job_entry` the hold branch stays
    **ahead** of it: a job can carry both, and a hold says the applier will not try this
    sweep, so promising a retry there would be a lie. It is also the first
    `_ADDED_COLUMNS` entry naming `postings` — nullable and additive, so
    `SCHEMA_VERSION` stays 2, and measured at 0.01 s on a 6 GB, 104,942-row file.
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


