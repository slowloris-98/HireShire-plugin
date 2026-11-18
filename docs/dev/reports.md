# Run outputs and the reports

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

## Run outputs

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
the `postings` notes in `docs/dev/storage.md`.

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


