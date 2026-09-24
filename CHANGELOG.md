# Changelog

All notable changes to this plugin are documented here. Versions follow
[semver](https://semver.org/); users only receive an update when `version` in
`.claude-plugin/plugin.json` is bumped.

## [Unreleased]

### Changed

- **Needs Attention is shorter and easier to scan.** Each reason a job needs you now
  appears as one short label instead of a sentence the applier wrote. Examples:
  "Requires human verification", "Required question: zip code", "Posting closed" and
  "Submit not confirmed — check before reapplying". Jobs at Google, Apple, Meta,
  Microsoft, Intuit and Amazon, and forms that email you a verification code, now read
  "Requires human verification". Jobs already on your dashboard switch to the new
  labels too. Hover over a label to see the full original message.

## [0.15.1] — 2026-09-24

### Changed

- **Auto-apply no longer answers questions meant to catch bots.** If an application
  form asks whether you are a bot or an AI, or tells an AI to type a particular word,
  the applier stops without submitting. The job moves to Needs Attention with
  "Manual application required." so you can apply to it yourself.

## [0.15.0] — 2026-09-24

### Added

- **Auto-apply can now fill in more of each application form.** When you turn on
  auto-apply, `/hireshire:setup` also asks four optional self-identification
  questions: gender, race/ethnicity, disability and veteran status. It uses your
  answers to fill the voluntary EEO section of each form. These answers are never
  used to find or score jobs, "Prefer not to say" is always an option, and if you
  skip them the applier declines on every form, as it did before.
- **Your GitHub link has its own field.** Setup reads your GitHub URL off your resume
  separately from your portfolio or personal site, so a form that asks for both gets
  both.

## [0.14.0] — 2026-09-24

### Added

- **You can now tell HireShire what you did about a job yourself.** Every job under
  Needs Attention or Jobs Shortlisted on the dashboard carries two buttons: *I applied
  to this* and *Not pursuing this*. Clicking one copies a command; paste it into Claude
  and the job moves, and the dashboard is rewritten straight away. You can also just run
  `/hireshire:mark-applied` and pick from a list.

  Until now those jobs were stuck. When an application stopped short of submitting, when
  an employer's portal needed an account login, or when the retry window closed, the job
  sat under Needs Attention for good — however many times you went and applied yourself.
  And a shortlisted job you applied to before the sweep reached it would be applied to
  a second time.

  The two answers do different things. *I applied to this* counts toward Jobs applied.
  *Not pursuing this* records no application at all: the job moves to Jobs Filtered,
  labelled, and HireShire stops offering it to the applier. Neither can be undone, so
  the list shows you what you are about to mark before it writes anything.

- **Amazon, Meta and Microsoft jobs are now part of every sweep.** HireShire now reads
  their career sites directly, as it already did for Apple, Google and Intuit. It runs
  quietly in the background like the rest of the sweep: no browser window opens and no
  extra Claude usage is spent fetching them. Amazon and Microsoft are searched in the
  countries your location list covers. Meta publishes its whole job board at once, so
  HireShire reads all of it and your location filter keeps the jobs in your places.
  Meta's listings carry no posting date, so your first sweep after updating reads
  through Meta's open jobs once, however old they are. Later sweeps only look at
  what is new. There is nothing to set up.
- **Amazon is skipped when applying, as Meta and Microsoft already were**, because its
  application forms need an account login. This applies to new installs only. If you
  set HireShire up before this update, it will still try to apply to Amazon jobs, and
  they will land under Needs Attention.

### Changed

- **Apple, Google and Intuit are now searched in the countries you chose.** The three
  company career sites HireShire reads directly were searched for the United States
  and India whatever your locations said — Intuit for everywhere — so if you were
  looking in London or Berlin, their jobs there were never fetched. Each site is now
  asked for the countries your location list covers, worked out from the cities,
  states and countries you gave at setup; nothing new to set. If your list includes a
  place HireShire cannot pin to a country, those sites are searched everywhere
  instead and your location filter narrows the results, so nothing you asked for is
  left out. If you are searching the United States and India, the searches are
  exactly what they were.
- **Setup now asks how recent a posting must be in hours, not days.** It offers 6 hours
  (recommended), 12 hours or 24 hours, so a sweep every few hours only reads what is
  new. Your current setting is unchanged; re-run `/hireshire:setup` to pick one.

### Fixed

- **Intuit jobs open in several cities are no longer thrown away.** Intuit lists those
  as "Multiple Locations", which names no country, so the location filter discarded
  every one — on the install this was found on, not one had ever been saved, and they
  were 18 of the first 30 jobs Intuit returned in a check. They are now kept, since
  Intuit is only searched in your countries to begin with.
- **Google's jobs are no longer all dropped when your locations are cities or states
  only.** Google's search results do not say where a job is until its page is opened,
  and a list like `california` or `remote` never matched the stand-in location they
  carried, so the whole of Google was filtered out before a single job was read.
- **Indianapolis is no longer read as India**, nor Busan as the United States: place
  names on these three sites are now matched as whole words.
- **The lifetime dashboard now shows each job once, as it stands today.** A job the
  sweep could not get to — the call budget ran out, or the scorer failed — comes back
  on a later sweep, and the record of that later sweep was being added beside the old
  one rather than replacing it. So a job could appear twice: once with the score it
  eventually got, and again, further down, still described as waiting for a call it had
  already had. 381 jobs on a real install were listed under two contradicting labels.
  Where a job had several unfinished attempts, the page picked between them arbitrarily,
  which could show a job as still in the running after the relevance check had ruled it
  out. Every part of the page — the four tiles, the five lists and the progress bars —
  now reads the same, most recent record of each job, so the tiles and the lists beneath
  them can no longer tell you different things.

  The page also stopped cutting off scored jobs. It loaded at most ~650 of them, so on a
  mature install several hundred judged jobs were missing from the lists entirely; on the
  install this was measured against, 1,233 now appear where 755 did. Per-sweep dashboards
  were never affected, and no score, verdict or database row changes — only which one the
  page reads.
- **A job the applier never managed to start on no longer disappears quietly.** When a
  browser session fails to launch — the Claude CLI missing, or the machine refusing to
  start it — the job is left alone and retried on later sweeps, for three days. After
  that the retrying stopped and nothing said so: the job kept its place under **Jobs
  Shortlisted** on your dashboard for good, looking like work that was still coming,
  and the posting link you could have used yourself was buried among jobs that were
  genuinely queued. Those jobs now move to **Needs Attention** when the three days are
  up, with a line saying no application was completed and the link to apply by hand.

  Nothing is given up on sooner than before — three days of retrying is unchanged, and
  a sweep that could not start a single session gives up on nothing at all, since the
  fault there is the machine's rather than the job's.

## [0.13.2] — 2026-09-22

### Fixed

- **A job in the wrong location is no longer re-opened every sweep.** When the applier
  found a posting was somewhere you had not asked for, it set the job aside but never
  wrote that down — so every sweep for the next three days picked it up again, opened a
  browser and re-read the same page, up to about 36 times per job. One job was reopened
  eleven times before this was caught, and the wasted sessions were compounding sweep
  over sweep. The job is now set aside once and moves to **Jobs Filtered** on your
  dashboard, with its score and a line saying which location it was, instead of sitting
  under Jobs Shortlisted as though it were still queued.
- **The location check now uses your own list.** It was comparing against six
  hard-coded words and never read your settings, so a job in `Arlington, VA` could be
  set aside as out of area even though Virginia is on your list. It now reads the same
  locations `/hireshire:setup` saved for the job search, and works them out rather than
  matching them letter by letter — `Arlington, VA` counts as the United States. When
  the posting is vague about where the job is, it applies rather than guessing.

Two notes if you look at the files a sweep writes: the results CSV shows these jobs
with their real score and `shortlisted` empty, while `<stamp>_results.json` still lists
them, because that file records what the sweep handed the applier. And a dashboard from
an *older* sweep is a saved file that is never rewritten, so it keeps showing such a job
under Jobs Shortlisted; the lifetime dashboard is always current.

## [0.13.1] — 2026-09-22

### Fixed

- When an application or a scoring call fails, the log now says what Claude or Codex
  actually reported — a usage limit, for instance — instead of the call's token counts.
  Nine applications were deferred over one night without the log naming a single reason.

## [0.13.0] — 2026-09-21

### Added

- **Uses your GPU when you have one.** On a machine with an NVIDIA (or AMD) graphics
  card, HireShire now installs the GPU build of PyTorch, and the two models that read
  every job before anything is scored run on the card instead of the processor. Large
  employers that took most of an hour on the processor should finish in minutes.
  Without a supported GPU, or on a Mac, nothing changes. Your first sweep after the
  update spends a few extra minutes upgrading.
- The sweep log names the device the models run on, for example
  `on cuda:0 (NVIDIA GeForce RTX 3060 Laptop GPU)`.

### Fixed

- An update never pulls PyTorch out from under a sweep that is already running; it
  waits for the next start. If the GPU download fails (offline, for instance), the
  sweep carries on with the build it already had and tries again next time.
- If the graphics card runs out of memory, the reranker uses smaller batches, and
  moves to the processor if even that fails. Jobs are scored the same either way.

## [0.12.0] — 2026-09-21

### Added

- **Score jobs on a ChatGPT plan.** `/hireshire:setup` now offers a third scoring
  backend, `codex`, which judges each job through your local Codex CLI signed in
  with ChatGPT — no API key, like the Claude option. Setup checks that Codex is
  installed and signed in, lists the models your plan offers, and pins the one you
  choose. The calls count against your plan's Codex usage limits.
- Unlike the Claude backend, Codex cannot reuse your resume from cache between
  jobs, so each scoring call reads it in full. The log's usage line says what a
  sweep used; it shows no price, because Codex reports none.

## [0.11.0] — 2026-09-21

### Removed

- **`/hireshire:find-jobs` and `/hireshire:apply`.** The plugin now has two commands:
  `/hireshire:setup`, once, and `/hireshire:start-orchestration`, which sweeps straight
  away and then on your poll interval — scraping, scoring, writing each sweep's CSV and
  dashboard, and applying to shortlisted jobs if you turned auto-apply on. Auto-apply
  itself is unchanged: sweeps still submit each application as its job is shortlisted.
  A job whose apply session could not start is retried on the next sweep, which is what
  the manual catch-up was for.
- `scripts/applied_cli.py`, which only `/hireshire:apply` used, and the permission hook
  that auto-approved read-only browser tools for it. Browser actions in your own
  session now always ask.

## [0.10.0] — 2026-09-21

### Changed

- **Title keywords now match whole words instead of any run of letters.** Excluding
  `intern` used to drop **Internal Tools Developer**, **International Sales** and
  **Internal Auditor** as well, because the filter only asked whether those letters
  appeared anywhere in the title. It now asks whether the word is there. `ios` no
  longer drops **Kiosk Manager** or **Biosciences Analyst**; `mobile` no longer drops
  **Automobile Design Engineer**; `senior` no longer drops **Seniority Programs Lead**.
  Phrases still work exactly as before, punctuation included — `manager, engineering`
  and `head of` match the same titles they always did.

  **Your existing keywords keep working and you do not need to change anything.** Some
  are now longer than they need to be: `sr` on its own is safe (it does not match SRE)
  and already covers both `Sr Engineer` and `Sr. Engineer`, so a list containing
  `sr. `, `sr software`, `sr engineer`, `sr ml` and `sr ai` can become a single `sr`
  whenever you feel like tidying it.

  **Two things to know before you rely on it.** First, nothing is stemmed, in either
  direction: `intern` does not catch **Interns** or **Internship**, and `internship`
  does not catch a bare **Intern**. If you were leaning on the old behaviour to cover a
  plural, add the other spellings — `intern`, `interns`, `internship`, `internships`.
  Second, this does not undo the past. Jobs already dropped by an over-broad keyword
  were recorded as decided and are not reconsidered, so the fix applies to postings
  found from here on rather than retrieving what an earlier sweep discarded.

- **Setup drafts exclusion keywords differently.** It now writes single words —
  `staff` rather than `staff engineer` — because one word covers every specialisation
  at that rung, catching **Staff ML Engineer** and **Staff Data Scientist** without
  anyone having to list them. It also writes out every spelling a posting might use
  rather than assuming one covers the others, so asking it to rule out internships
  produces all four of `intern`, `interns`, `internship`, `internships`, and ruling out
  vice presidents produces both `vice president` and `vp`.

  Which words it considers still depends on your profession, and that has become more
  important rather than less: `staff` means a promotion in software and means the job
  itself for a Staff Nurse, so it is drafted for one and never for the other. The
  confirmation step is unchanged — it still shows you the full list and still tells you
  the drops are permanent — but it now groups the spellings, as `senior (+ sr)`, so the
  line stays readable.

## [0.9.0] — 2026-09-19

### Changed

- **Each sweep now keeps its application screenshots in its own folder.** They used to
  pile up in one shared `hireshire_run_results/applied/`, with nothing to say which
  sweep a screenshot came from. From now on they go to
  `hireshire_run_results/<date>_<time>/applied/`, beside that run's results CSV, JSON
  and dashboard. Your existing shared folder is left exactly as it is — nothing is
  moved or deleted, so every screenshot you already have stays where you last saw it.
  (The `applied_dir` setting in `applier.yaml` is gone with it; it is safe to leave the
  line in your file, where it is now ignored.)

- **The overview pages are now called dashboards, and are saved under new names.**
  `overview.html` at your results root is now `Dashboard_Lifetime.html`, and each
  sweep's own copy is `Dashboard_<date>_<time>.html` instead of
  `<date>_<time>_overview.html`. The pages themselves are unchanged apart from the
  subtitle, which now reads *Lifetime Dashboard* / *Dashboard Run: …* rather than
  *Control Room*.

  **Update your bookmark.** Nothing writes the old `overview.html` any more, so the
  copy already in your results folder will sit there frozen at whatever your last
  sweep left. It is safe to delete, along with any `*_overview.html` inside old run
  folders — every one of those runs has its results CSV and JSON beside it either
  way.

- **The `Est. cost` tile is gone from the run dashboard.** The figure was the Claude
  CLI's own estimate at list price, not a bill and not a reading of your actual
  subscription usage, and on a page about which jobs you have it read as if it were
  one. The number is still measured and still recorded for every sweep — it appears
  in the matcher's console summary and in `logs/`, and it is kept in the database —
  it is simply no longer shown as a headline. The `Took` tile stays.

## [0.8.0] — 2026-09-19

### Changed

- **Employers the applier can't apply to now appear under Needs Attention.** A few
  companies (Google, Apple, Meta, Microsoft, Intuit by default — the
  `exclude_companies` list) make you sign in before their application form appears, so
  the applier can never complete them. Those jobs used to sit under **Jobs
  Shortlisted** looking like something it would get to, and the only mention of them
  was a line in a log file — which nobody sees on an unattended sweep. They now appear
  under **Needs Attention** on the overview page, with the reason *"Requires human
  verification — this employer's portal needs an account login, so apply to it
  yourself."*

  They also stop being retried: previously the same job was picked up and dropped
  again on every sweep for three days. The trade-off is that if you later remove a
  company from `exclude_companies`, jobs already recorded that way are not applied to
  automatically — apply to them from the Needs Attention list.

  `/hireshire:apply` records them the same way, and still prints its **Apply
  manually** list with the URLs.

  If you already have shortlisted jobs at those employers, the ones from the last
  three days move to Needs Attention on your next sweep. Anything shortlisted longer
  ago than that stays under Jobs Shortlisted — apply to those by hand, or leave them.

## [0.7.1] — 2026-09-19

### Changed

- **The main overview page's progress bars now cover all your sweeps, and they
  stay put.** In 0.7.0 the bars on `overview.html` showed only the sweep that was
  running, and disappeared when it finished. They now show totals and remain on the
  page between sweeps:
  - **Scraper:** how many different jobs your sweeps have found, with each posting
    counted once. The bar fills as companies are checked.
  - **Matcher:** jobs filtered or scored across all your sweeps.
  - **Applier:** every job you've ever been shortlisted for, and how many have been
    applied to, split into applied, needs attention and not yet applied.

  Each sweep's own page is unchanged.

## [0.7.0] — 2026-09-19

### Added

- **The overview page shows how far a sweep has got.** Three progress bars sit above
  the numbers:
  - **Scraper:** companies checked out of all the companies in the sweep.
  - **Matcher:** jobs filtered or scored out of the jobs in scope.
  - **Applier:** shortlisted jobs dealt with out of the jobs shortlisted, split into
    applied, needs attention and skipped.

  A sweep's own page keeps its bars after the sweep ends, so you can see where each
  stage finished. The main `overview.html` shows the bars only while a sweep is
  running.

## [0.6.0] — 2026-09-19

### Changed

- **Recurring sweeps no longer stop after 24 hours.** `/hireshire:start-orchestration`
  now keeps sweeping on your schedule until you run `--stop` or kill its task. If
  auto-apply is on, it keeps submitting applications until then.
- **Fewer applications stop on a question your resume can't answer.** When you turn on
  auto-apply, `/hireshire:setup` now asks three screening questions: are you authorized
  to work, do you need sponsorship, will you relocate. It also reads your LinkedIn and
  portfolio links off your resume, along with your name, email and phone, and asks you
  to confirm them rather than typing each one. Essay questions ("why do you want to
  work here?") are written from your resume and the job description. A question about
  a tool your resume doesn't list is answered **yes**, citing the closest tool it does
  list.
- **Applications that didn't go through now have their own section.** The overview
  page has a **Needs Attention** section after Jobs Applied, listing each application
  that stopped short with a one-line reason (for example, a sign-in wall or a required
  question with no answer). The **Jobs applied** number now counts only real
  submissions. Before, it also counted those failed attempts.

## [0.5.1] — 2026-09-18

### Fixed

- **Scoring no longer gives up after a brief Windows hiccup, and says so plainly when
  it does stop.** When Windows refused to start the scoring process (exit code
  `3221225794` = `0xC0000142`), five of those in a row stopped scoring for the whole
  sweep, and the log looked like the scoring backend had broken. Each scoring call now
  waits and tries again (after 5, 20 and 60 seconds) before it counts as a failure. If
  scoring still stops, the log says the cause is the machine, not your login: usually
  the terminal running the sweep was closed or the PC was locked or asleep. As before,
  no jobs are lost; they are scored next sweep. Apply sessions that fail this way now
  name the error too.

## [0.5.0] — 2026-09-18

### Changed

- **The judge shows its evidence before it scores, and costs far less per job.** It
  used to pick a number out of 40 per category and then write a sentence justifying
  it. It now lists the posting's requirements first, quoting your resume for each one
  it can, writes its reasoning, and only then picks a 0-5 band per category. The
  plugin turns those bands into the same 40/40/20 scores as before, so the overview
  page and the results CSV look the same. The arithmetic moved out of the judge: a
  mandatory requirement with no evidence caps its category, applied once, by the
  engine, and the rationale says so when it happens.

  Each judge call now runs with no tools and none of Claude Code's surrounding
  context (`--safe-mode --tools ""`). On one measured call that was **54,441 input
  tokens without, 1,765 with**. Thinking effort defaults to `low`, since the written
  checklist now carries the reasoning; one measured call used no thinking tokens.

  The judge is no longer asked for years of experience. The free pattern-match before
  it already reads that, so the judge only mentions years if a posting asks for
  plainly more than your resume shows.

  **Check your `threshold` after updating.** Scores now move in steps of 8 (skills,
  experience) and 4 (education), so a threshold chosen under the old judge will not
  shortlist the same share of jobs.

- **Applications go out as soon as a job is shortlisted, not after the sweep.** The
  apply phase used to be one `claude -p` session run once the whole sweep had
  finished, reading the shortlist back out of `last_run.json` — so a job judged in
  minute three waited for every other employer before anything applied to it. Each
  shortlisted job now goes straight from the matcher's queue to an apply worker that
  runs one short browser session for that job alone, one at a time. The browser is
  still driven by Claude through the plugin's Playwright MCP, and the form-filling
  rules are unchanged; they now live in `hireshire/applier/apply_one.md`, shared by
  the worker and `/hireshire:apply`.

  The engine records each outcome itself. A session that could not start (the CLI
  missing, a non-zero exit) records nothing, and three in a row stop the applier for
  the rest of the sweep. Those jobs are picked up by the new **backlog**: every sweep
  also applies to shortlisted jobs from the last `backlog_hours` (default 72) that
  have no application record. A session that timed out (`apply_timeout_s`, default
  900) or ended without a readable result *is* recorded as an error, because the form
  may already have been submitted and a retry could apply twice.

  Each session loads the plugin's own browser server (`--mcp-config .mcp.json
  --strict-mcp-config`). A `claude -p` started by the engine was measured *not* to
  load the plugin, so the Playwright tools the old apply phase depended on were not
  there at all; the session also no longer sees the user's other MCP servers.

  `/hireshire:apply` is now a manual catch-up over those same pending jobs, read with
  the new `applied_cli.py pending` rather than from `last_run.json`.

  **With `enable_applier` on, the browser now opens mid-sweep.** Nothing else about
  the gate changed.

- **The years-of-experience gate is now on by default**
  (`funnel.experience.enabled: true`). It still does nothing until
  `/hireshire:setup` has recorded your years of experience. Once that is set,
  postings that ask for more than you have are filtered out before they cost a
  scoring call.

### Fixed

- **The plugin no longer ships a `.claude/settings.json`.** Claude Code refused it on
  every scoring call, because the plugin folder is never trusted, and each refusal
  printed about 645 characters of warning. That warning is what made one real scoring
  failure look like a trust problem.

- **Sweep applications no longer fail at the resume upload.** The browser server only
  uploads files from inside the session's working directory. That directory was the
  plugin's data folder, while your resume is in your job-search folder, so every form
  that required a resume stopped unsubmitted (5 of 8 errors on one real sweep). Each
  apply session now runs in your job-search folder. The pre-submit screenshot, one per
  job, goes to `hireshire_run_results/applied/`, and the session no longer leaves page
  snapshots lying around. On an install without a job-search folder, or with a resume
  kept elsewhere, the resume is copied into the session's folder first.

## [0.4.0] — 2026-09-11

### Changed

- **One CSV instead of two.** `<stamp>_results.csv` now holds **every job that
  reached the funnel**, best first, with eight columns: `posted_at`, `company`,
  `job_title`, `link`, `llm_score`, `cross_score`, `applied`, `shortlisted`. A blank
  `llm_score` means no judge read that job and is never a zero. It replaces both the
  old shortlist CSV and `<stamp>_results_all_jobs.csv`, which overlapped heavily and
  between them still could not answer "show me everything, best first, and tell me
  what I've already applied to".

  It is written once at the end of the run, because the sort needs every row — so it
  no longer fills live mid-sweep. The overview page is what moves during a sweep.
  `<stamp>_results.json`, which `/hireshire:apply` reads, is unchanged.

- **All four sections of the overview page now read the same way**: a filter box over
  a sticky six-column header (`# | Title | Company | Location | LLM | Cross`) over a
  bounded scroll box. Three of them used to be unbounded flat lists beside one that
  was not, which made a sweep with 300 filtered jobs a page you scrolled past rather
  than read. Clicking a job still expands its full reasoning in place, and a job's
  drop reason moved to a line under its title — the columns are single-line and the
  reasons are sentences.

### Fixed

- **When a scoring call fails, the log now shows why.** The plugin reported whichever of
  the CLI's two output streams it found first, and one of them always carries a routine
  warning — so the real message, which the CLI writes to the other stream for failures
  like an unavailable model, was thrown away. Failures logged as an unrelated warning, or
  as "(no output)". Both streams are now reported, each kept short enough that neither can
  crowd out the other.

- **A sweep that fails part-way now leaves its results.** Before this, a run that died
  fifteen minutes in wrote no JSON, no diagnostic CSV, and left `last_run.json` still
  pointing at the *previous* run — everything it had actually scored was reachable
  only by opening the database by hand. The run's output files are now written in a
  `finally`, so they hold whatever the sweep judged before it stopped, and both
  `last_run.json` and the run's database row record `complete: false` so a partial run
  is not mistaken for a whole one.

- **A crashed sweep no longer leaves its pages reloading forever.** The pages decide
  whether a sweep is still going by looking for the run's own database row, and a
  failed run never wrote one — so both overview pages went on refreshing every fifteen
  seconds with their elapsed figure climbing on a process that had been dead for
  hours. That row is now written whether or not the run finished.

### Removed

- **`dashboard.html` and the per-run matching report** (`<stamp>_matching.html` and
  `latest_matching.html`). Three pages answered overlapping questions; the overview
  page answers "what have I got" at both scopes — one sweep, and every sweep the
  install has done — and keeps the judge's reasoning inside each job.

- **Artifact publishing.** `/hireshire:find-jobs` and `/hireshire:start-orchestration`
  used to publish the matching report to a rolling artifact URL. Both overview pages
  are local files that rewrite themselves while a sweep runs, so the user watches them
  directly rather than waiting on a republish. Nothing leaves the machine.

- `last_run.json` no longer carries `matching_html`, `latest_matching_html`,
  `dashboard_html` or `all_jobs_csv`; it names `csv`, `json`, `overview_html` and
  `run_overview_html`, and gains `complete`.

## [0.3.1] — 2026-09-10

### Fixed

- **A sweep no longer stops itself, and this is the fix for sweeps dying ~60 seconds
  in with exit code 1 and no traceback.** Reported on a fresh Windows install running
  the latest code, and reproducible on any host that does not publish `CLAUDE_PID`.

  The sweep used to be tied to the Claude Code session that started it, by two
  mechanisms that both read `CLAUDE_PID`: a watchdog inside the sweep, and a
  `SessionEnd` hook outside it. Where that variable is absent they did not degrade —
  they failed *together*, in the worst direction. The watchdog never armed, and the
  hook's "no recorded owner" fallback answered **yes, stop it** for every Claude Code
  session ending anywhere on the machine.

  The sweep spawns one `claude -p` per scoring call, so it manufactured its own
  killers. The first scoring call to finish ended the run: `taskkill /T /F`, which
  exits 1 and unwinds nothing, leaving no traceback, no `ERROR` line and a stale
  status file. It affected **every** sweep path, `/hireshire:find-jobs` and the OS
  scheduler entry included, because all three run the same program.

  Removed rather than patched. Each fix here added another conditional on an
  environment the plugin does not control, and the failure direction is destroying the
  user's work. A recurring sweep now bounds its own runtime (24 hours) instead, which
  gives the same protection against an unattended auto-applying sweep without asking
  the host a question it may not be able to answer.

### Removed

- **The `SessionEnd` hook**, `hireshire.sh --session-end`, and the `CLAUDE_PID`
  watchdog in `run_orchestration.py`.
- **`hireshire.sh --status`** and `hireshire/orchestration_status.py` — the heartbeat,
  the five-minute staleness window and the liveness veto existed to answer "is it
  running" for `--status` and the teardown above. `hireshire/sweep_pid.py` records a
  single pid so `--stop` can reach a sweep and a second sweep will not start alongside
  the first.

### Changed

- **A recurring sweep is no longer session-scoped, and `/hireshire:start-orchestration`
  now says so.** It ends on `--stop`, on its shell task being killed, or on the
  24-hour bound. The skill previously promised it stopped when the session ended;
  that is no longer true, and saying it would be the same class of failure as
  announcing a sweep that was not running. Watch a sweep through the dashboard or the
  shell task rather than through the skill.
- `--stop` is approved by the permission guard, so ending a sweep does not prompt.

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
