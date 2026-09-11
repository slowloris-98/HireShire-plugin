---
name: setup
description: One-time guided setup for HireShire — points it at your resume, works out what roles to look for, and does the first-run downloads. Run this before find-jobs.
---

# HireShire setup

Walk the user through configuration **conversationally**. Never show them a YAML
file, never ask them to edit one, and never ask them to open a terminal. Every
value below is written for them by `hireshire.config_writer`.

## Running engine commands

Every action in this skill is one command. Always go through the bundled launcher:
macOS has no bare `python`, and on Windows a Microsoft Store stub named `python3`
sits on PATH but does not work. The launcher resolves a real interpreter,
bootstraps the venv if needed, and re-execs inside it.

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py <subcommand> [args]
```

| what you need | subcommand |
|---|---|
| copy the default config into the data dir | `install-config` |
| create the user's job-search folder | `init-workspace "<path>"` |
| resumes already sitting in the workspace | `find-resumes "<workspace>"` |
| validate + copy their resume in | `install-resume "<file>" "<workspace>"` |
| read the resume, to draft target roles | `resume-text "<file>"` |
| see a phase's current settings | `get <phase>` |
| see a phase's editable keys and what they mean | `field-docs <phase>` |
| write settings | `set <phase> --json '{...}'` |
| write the search profile | `write-profile --text "..."` |
| pull the models | `warm-models` |

**Never write Python to a file and run it.** That is what this skill used to do, and
it is why setup asked the user's permission a dozen times before showing them a
single job: Claude Code matches permission rules against the exact command string,
so a heredoc — whose body differs on every call — can never be approved once. The
subcommands above are fixed shapes and are approved automatically. If you need
something the table does not cover, prefer doing without it; a new subcommand
belongs in `scripts/setup_cli.py`, not in a temp file.

Three rules behind that, all learned the hard way:

- **Never invoke the venv interpreter yourself.** It skips `run_engine.py`, which
  is what sets `PYTHONPATH` — so `import hireshire` fails — and what pins the data
  directory. Hand-rolled `export` lines get one of the two wrong.
- **`${CLAUDE_PLUGIN_ROOT}` is fine to write. The matching CLAUDE_PLUGIN_DATA
  placeholder is not** — never put it in a command. Claude Code substitutes both
  into this file before you read it, but the data one does not resolve to the same
  directory on every interface: the Claude desktop app gets a different one from the
  terminal and the VS Code extension. A file written there lands somewhere the
  engine never looks, and nothing reports the mistake.
- **Ask for the data directory, never guess it.** One command, and it works before
  the venv exists:

  ```bash
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --paths
  ```

  It prints `ROOT=<path>` and `DATA=<path>`. You rarely need it now —
  `write-profile` resolves `DATA` itself and prints where it wrote — but it is the
  one supported answer when you do.

## Step 0 — set expectations, then install

**Say this before you run anything.** The install is minutes of silence otherwise,
and a user watching a still spinner concludes the plugin has hung — which is exactly
what happened when this ran from a session hook instead:

> Setup takes about 15-20 minutes, most of it a one-time download of the models
> that decide which jobs are worth scoring. It happens now rather than in the
> middle of your first search. I'll tell you when it's done.

**Give the time, never the download size.** A number of gigabytes is not something
the user can act on — they cannot make it smaller, and it lands as a warning about
their disk rather than an answer to the only question they are asking, which is how
long they will be sitting there. State the minutes and move on. This applies
wherever the install is described, including the text `--bootstrap` prints.

Only then start the install, and say so again when it returns:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --bootstrap
```

This is the step that downloads. It is safe to re-run: it compares the shipped
requirements against a lock file in the data directory and returns immediately when
the environment is already current, so on a warm install it costs nothing and you
can move straight on.

If a session-start message already told you dependencies are missing, that is the
same fact reaching you early — pass it to the user in your first sentence rather
than waiting until you are about to install.

Then copy the default config into the data directory. Everything after this edits
that copy, so plugin updates never overwrite their answers.

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py install-config
```

## Step 1 — where their job search lives

Ask this **before** the resume question: the resume gets copied into the folder
they pick, so it has to exist first.

This is the one answer you supply rather than the engine. **You can see the working
directory; the engine deliberately cannot** — a plugin's cwd is whatever project
the user happens to be in, so the engine reads this from config and never from cwd.
Capture it once, here.

Default to the folder this session started in. Show it and ask:

> I'll keep your job search in `<cwd>`. Your results go in
> `hireshire_run_results/`, and I'll keep a copy of your resume in
> `resume/original/`. Somewhere else? Give me the full path.

Then create the structure and record it:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
    init-workspace "<their answer, or cwd>"
```

It prints the absolute path it created. Write **that** path back:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
    set scraper --json '{"workspace_dir": "<the printed path>"}'
```

`init-workspace` creates `resume/original/` and `hireshire_run_results/` if they
are missing, so a folder they made thirty seconds ago and left empty is fine. It
refuses a folder inside the plugin's own directories — those are wiped on update —
and tells them why.

Say once, plainly: **this is recorded, so results land there even if they later
launch Claude Code from somewhere else.** Re-running `/hireshire:setup` is how they
move it; results already written stay where they are.

## Step 2 — the questions

Ask these **conversationally and in small groups** — two or three at a time, not
as a ten-item form. Confirm what you understood before writing.

**Use `AskUserQuestion` for every question below that has a small set of sensible
answers**: locations, posting age, match threshold, jobs per run, job boards, scoring
backend, scoring effort, poll interval, auto-apply, and the title-exclusion list you
draft in question 6. This skill already names a default or a recommendation for almost
all of them — put that option first and mark it recommended. The user gets one tap
instead of typing, and "Other" is always there for anyone who wants something else, so
offering options never narrows what they can say.

Three answers stay free text, because they are open-ended and a menu would constrain
them: the path to their resume, the correction to the target roles you drafted in
question 6, and the years of experience you read off the resume in question 7 — that
one is a number to accept or correct, not a choice between options. Question 6 is the
one step that uses both: the roles come back in the user's own words, and the title
exclusions — which cannot be undone — get their own tappable confirmation before
anything is written.

### How to write a value

`set <phase> --json '{...}'` takes **flat keys**, never the YAML nesting. The names
below are the keys; where they sit in the file is the writer's business. One `set`
can carry several keys for the same phase — batch a group of answers into one call
rather than one call per question:

| phase | keys |
|---|---|
| `scraper` | `location_filter`, `max_age_hours`, `enabled_platforms`, `poll_interval_hours`, `workspace_dir` |
| `matcher` | `threshold`, `provider`, `model`, `effort`, `resume_path`, `search_profile_path`, `include_keywords`, `exclude_keywords` |
| `funnel` | `targets`, `top_k`, `rerank_min_score` |
| `applier` | `enable_applier`, `resume_path`, `first_name`, `last_name`, `email`, `phone` |

So it is `set matcher --json '{"exclude_keywords": [...]}'` — **not**
`'{"title_filter": {"exclude_keywords": [...]}}'`, which is rejected.

Three things that trip people up:

- `matcher` and `funnel` are two whitelists over the *same* `matcher.yaml`. Writing
  one never disturbs the other, but the phase has to match the key.
- List keys (`location_filter`, `enabled_platforms`, `include_keywords`,
  `exclude_keywords`, `targets`) want a **list**, even for one item. A bare string
  is wrapped for you, but write the list.
- `field-docs <phase>` prints the live keys and what they mean. Use it if this table
  and the code ever disagree.

1. **Resume** → `matcher.resume_path`, and the same path to `applier.resume_path`.

   Look in the workspace first. A PDF already sitting in `resume/original/` means
   the documented flow worked — offer it by name and confirm rather than asking for
   a path.

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       find-resumes "<workspace>"
   ```

   Otherwise ask for the file and install it:

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       install-resume "<their path>" "<workspace>"
   ```

   It prints the copy's path. Write that to both phases:

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       set matcher --json '{"resume_path": "<printed path>"}'
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       set applier --json '{"resume_path": "<printed path>"}'
   ```

   It validates with `extract_resume_text` **before** copying, so a scanned PDF
   fails now — while they can still pick another file — rather than three minutes
   into the first run, and it leaves nothing behind in their folder when it does.
   Then it copies the file into `resume/original/` so the whole search is one
   directory. It never overwrites: same name, different contents gets a numbered
   suffix.

   **Write the path it returns**, which is the copy's, not the one they typed.

2. **Locations** → `scraper.location_filter`, a **list**. Case-insensitive substring
   match, so `["united states"]`, `["remote"]`, `["london", "berlin"]` all work. One
   location is still a list. Empty list means everywhere.

3. **Posting age**, in days → `scraper.max_age_hours` (multiply by 24).

4. **Match threshold, as a number from 0 to 100** → `threshold` on the **`matcher`**
   phase. Offer numbers — 75 recommended, plus a couple either side — and let "Other"
   take any value they prefer. **Never offer word-based tiers** like "strict" or
   "relaxed": the setting is a number, the user should see the number they are
   choosing, and a label hides how much a step actually moves the filter.

   The funnel has its own `encoder_threshold` — a 0-1 cosine recall net, an unrelated
   setting that setup must never touch. It is deliberately loose; raising it throws
   away the differently-worded jobs the reranker exists to catch.

5. **A ceiling on jobs scored per run** → `top_k` on the **`funnel`** phase.
   Default 150. Present it as a safety limit, not as how jobs get chosen — the
   cross-encoder cutoff does that, and it is not a setup question.
   - On a **Claude subscription** these calls draw on the **same allowance as
     their own Claude chat** — a rolling 5-hour window plus a weekly one. Say this
     plainly; a user who does not know it will be surprised when a sweep eats into
     their conversations.
   - On a **paid API key** it is a straight cost dial and can go higher.

   Jobs that arrive after the ceiling is reached stay eligible for the next run, so
   this is safe to raise later. Do not ask about `rerank_min_score`: it is a raw
   model logit with no meaning a user could reason about, and
   `scripts/calibrate_cutoffs.py` derives it from real runs once they have some.

6. **Target roles.** This single step is what makes the plugin work for any field,
   and it is the main defence against missing jobs that are a real fit but worded
   differently.

   **Draft first, then ask.** By this point their resume is installed and readable:

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       resume-text "<the installed path>"
   ```

   Read it and settle **two** things before you draft anything: what they do, and
   **which rung of their field's ladder they are actually on**. The rung is what makes
   the exclusion list below correct, and nothing else in setup gives it to you — the
   number in question 7 is about the *posting's* stated minimum and never looks at a
   title at all.

   Then propose concrete target roles and let them correct you:

   > From your resume this looks like mid-level Account Management / Customer
   > Success in SaaS — individual contributor, no direct reports. Is that the target,
   > and is there anything you'd rule out — industries, a specialisation you're done
   > with?

   That is a far better question than asking cold, and it is faster for them. Ask this
   one as free text — the useful answer is a correction in their own words, which no
   menu can anticipate. And if a question turns out to have only one real answer, state
   it as an assumption and move on rather than inventing a second option to pad it out
   into a choice. That is a rule against fake choices, not against offering real ones.

   Then generate three things from their answer *and* the resume text:

   - `exclude_keywords` (phase `matcher`) — title words never worth an LLM call. Draft
     these from the seniority ladder below, plus anything the user ruled out. **Never
     leave this empty by default, and never fill it by guessing**: it is the one value
     in this step that cannot be taken back.
   - `targets` (phase `funnel`) — an **exhaustive** list of adjacent and synonymous
     **job titles** they are qualified for. Aim for dozens. This is a recall net;
     over-inclusion is cheap and under-inclusion loses jobs permanently.
   - `search_profile_path` (phase `matcher`) — a dense ~200-word "ideal candidate"
     profile. Describe the **underlying transferable skills**, in the vocabulary
     employers use, not just the literal nouns on the resume — "React" should also
     appear as "component-based UI development" and "frontend state management".
     This text is the reranker's query and is what closes the vocabulary gap.

     `write-profile` puts it in the right directory and prints where it landed, so
     you never name `<DATA>` yourself and there is nothing to `ls` afterwards. That
     matters: a run whose profile went to the wrong directory does not fail — the
     reranker simply has no query, the cross-encoder is skipped, and the LLM budget
     is spent on unranked jobs. The only symptom is one
     `search_profile_path set but not found` line in the log.

   `include_keywords` is optional: leave it empty unless the user wants a hard
   keyword requirement. An empty include list means the semantic gate decides, which
   is usually what they want. If they do want one, **check it against the exclusions
   first — exclude wins.** The title is tested against the exclusions *before* the
   include fast-pass is considered, so `include: ["senior engineer"]` alongside
   `exclude: ["senior"]` does not fast-pass anything; it drops everything.

   **The seniority ladder.** The recall net cannot do this part for you: "Senior
   Software Engineer" and "Software Engineer" are nearly the same string, so the
   encoder scores them alike, and the cross-encoder reads a description written for
   much the same work. Seniority is the one distinction the free gates cannot make,
   which is why it has to be a keyword rule — not a way to save money, a way to stop a
   capped per-run budget being spent two rungs above the user's head.

   Every field has the same ladder; only the words change. Place the user on it from
   the most responsible title they have actually **held**, not one they are aiming at,
   and read scope — reports, budget, who signs off — rather than the noun alone.

   | rung | what it is |
   |---|---|
   | 0 | trainee, student, apprentice, new grad |
   | 1 | individual contributor, no qualifier on the title |
   | 2 | experienced IC — the first "promotion word" in that field |
   | 3 | expert IC or first-line manager of a single team |
   | 4 | department head, several teams or a function |
   | 5 | executive — the function's name sits in the title with a chief or vice |

   **Exclude upward only.** Draft one term for each rung *above* theirs, spelled the
   way their field spells it. **Never auto-add junior, intern, entry-level, trainee,
   apprentice or co-op terms** — those rungs are below them, they are cheap to skim
   past, and a career changer who took a pay cut wants exactly those postings.
   Below-band terms go in only when the user names them.

   *Worked example — software, ~4 years, titles "Software Engineer" then "Software
   Engineer II":* rung 1. Above them: `"senior"`, `"staff engineer"`, `"principal
   engineer"`, `"engineering manager"`, `"director of engineering"`, `"head of
   engineering"`, `"vice president"`.

   *Worked example — registered nurse, 5 years, med-surg floor:* also rung 1, and the
   words are completely different. Above them: `"nurse manager"`, `"director of
   nursing"`, `"chief nursing"`, `"assistant director of nursing"`. Note what is
   **not** there: `"staff"` is this user's own rung (Staff Nurse), `"charge nurse"` is
   a shift role rather than a rung, and Nurse Practitioner is a different licence
   rather than a promotion — a credential the user does not hold is not a seniority
   exclusion, and it only goes in if they ask.

   **When the band is ambiguous, resolve it upward.** A career change, a long contract
   stretch, or a two-person startup where they were "Head of Growth" at three years
   all read as two rungs at once. Take the higher one, say which two you were choosing
   between, and let them settle it — the same posture as the number in question 7. The
   errors are not symmetric: a band read one rung too high leaves a few over-ambitious
   postings in the pool, which costs some scoring budget and nothing else, while a
   band read one rung too low permanently deletes the promotion they were applying
   for.

   **That asymmetry governs every choice below.** A term that is too narrow costs one
   extra scoring call. A term that is too broad silently deletes a slice of their
   market for the life of the install: a title-excluded posting is dropped before it
   is scored, is kept out of the results table on purpose, and is never reconsidered
   on a later sweep even if the keyword is removed. When in doubt, spell it longer.

   **So check every term for substring damage.** The filter is a plain
   case-insensitive substring test over the job **title** and nothing else — no word
   boundaries, no stemming, no description. `"lead"` is not the rule "no lead roles";
   it is the rule "drop any title containing l-e-a-d", which is Lead Generation
   Specialist, Team Leader and Leadership Development Partner.

   The test to run on each drafted term before it goes in the list: **say it inside
   three other titles from the user's own field.** If any of the three is a job they
   would want, the term is too short. Two rules fall out of that and cover most cases:

   - **Qualify the rung word with the field noun.** `"staff engineer"`, not `"staff"`.
     `"nurse manager"`, not `"manager"`. `"director of engineering"`, not
     `"director"`. A two-word term is nearly always right; a bare rung word is nearly
     always wrong.
   - **An abbreviation must carry its punctuation or the word after it.** `"sr. "`,
     not `"sr"` — which matches SRE. `"vice president"` or `"vp of"`, not `"vp"` —
     which matches AVP, a *mid-level* title in banking.

   The traps worth naming, because in some field each of these words means the
   opposite of a promotion:

   - `staff` is the junior IC rung in accounting, nursing, law and journalism — Staff
     Accountant, Staff Nurse, Staff Attorney, Staff Writer. Excluding it bare deletes
     the user's own job.
   - `senior` is a *client group* in care work: Senior Care Coordinator, Senior Living
     Advisor, Senior Services Manager.
   - `director` is a craft title in film, TV and design — Art Director, Creative
     Director, Director of Photography — and in a small nonprofit an Executive
     Director runs four people.
   - `principal` runs a school in education and means partner in law, consulting and
     architecture.
   - `manager` is the IC title in sales, product, projects and social work: Account
     Manager, Product Manager, Case Manager.
   - `partner` is an IC in HR and marketing: HR Business Partner, Partner Marketing
     Manager.
   - `head` matches Headhunter and Head of Household. `"head of"` is the spelling that
     means the rung.
   - `intern` matches Internal Auditor, International Sales and Internal Comms; there
     is no safe short spelling, so use `"internship"` and accept that a bare "Intern"
     posting gets through. The ladder never adds this on its own — it only comes up if
     the user asks for it.
   - `lead` matches Lead Generation, Leader and Leadership. If what they mean is "no
     people management", exclude the management nouns instead.

   **Last check before the list reaches the user: run the exclusions against your own
   `targets`.** Lowercase both. If any exclusion is a substring of any target title,
   one of the two is wrong, and you have written a filter that deletes the recall net
   you built in the same breath. Fix it first.

   **Then confirm the list with `AskUserQuestion`** — not a rhetorical "sound good?".
   Show the drafted terms in full, on one line, say which model drafted them — you
   are whatever model this session is running, and a user on a small one should know
   to read the list twice — and state the consequence in one sentence:

   > Drafted by <the model you are running as> from your resume. These are permanent:
   > a posting whose title contains one of these words is dropped before it is scored,
   > never appears in your results or in any "why was this skipped" list, and does not
   > come back if you remove the word later.

   Offer four outcomes, with the drafted terms named in the first:

   - **Use these** (recommended) — name the actual terms: "Skip senior, staff
     engineer, principal engineer, engineering manager, director of engineering, vice
     president."
   - **Keep the next rung up** — "Drop `senior` from the list so Senior <role>
     postings still get scored." Offer this whenever they are not already on the top
     rung: the rung immediately above them is the one they may be promoted into, and
     it is the term most likely to be regretted.
   - **Exclude more** — "Name any titles, seniorities or specialisations to add."
   - **No title exclusions** — "Score whatever the recall net returns. Rules nothing
     out, spends more of the per-run budget."

   Those are four genuinely different outcomes, which is what makes them options
   rather than padding; "Other" is for the user who wants to hand you an edited list
   in their own words.

   If they edit, their answer is a request and not a keyword list. Re-run the
   substring test and the `targets` check over whatever they say — "no lead roles"
   becomes `"tech lead"` and `"team lead"`, never `"lead"` — then state the final list
   back in one line and write it. **Do not ask a second time**; one confirmation is
   the deal and a second is an interrogation. If the answer leaves the list empty,
   write the empty list and say so plainly: nothing is filtered on title, and every
   posting the recall net returns is eligible for scoring.

   **Show all three back before you write anything** — the targets and the profile to
   edit freely in conversation, the exclusions through the confirmation above. Then:

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       write-profile --text "<the ~200-word profile>"
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       set matcher --json '{"exclude_keywords": [...], "search_profile_path": "profile.md"}'
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       set funnel --json '{"targets": [...]}'
   ```

7. **Years of experience.** You already have the resume text from the step above, so
   read a number out of it and make them confirm it — one short question, in the same
   turn as the draft above if that reads naturally:

   > From your resume I read about **4 years** of professional experience. I'll use
   > that to skip postings asking for meaningfully more — correct me if it's off.

   **Propose, never assume.** Every drop this setting causes is relative to this one
   number, in every future sweep, and a value two years low silently discards two
   years' worth of legitimate jobs with no error anywhere. If the resume is ambiguous
   — a career change, a long gap, freelance work — say what you are unsure about and
   let them settle it. Count professional experience, not education.

   This is not the seniority band from question 6, and the two never touch. The band
   is about words in the *title* and drops the posting for good; this number is
   compared against the minimum the *posting* states in plain text ("5+ years", "3-7
   years"), and that gate deliberately refuses to infer years from a seniority word —
   a "Senior" posting naming no number is kept. Getting one right does not cover for
   the other being wrong.

   Write it only once they have confirmed:

   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py \
       set funnel --json '{"experience_enabled": true, "candidate_years": 4}'
   ```

   What to tell them if they ask what it does: postings state a minimum in plain text
   ("5+ years", "3-7 years"), it is read directly with no model and no cost, and
   anything within six months of the bar still counts as a match. It never rejects
   them for having *too much* experience, and the ~quarter of postings that state
   nothing are always kept.

   If they would rather not filter on this at all, write
   `'{"experience_enabled": false}'` and move on — do not argue the point.

8. **Which job boards** → `enabled_platforms` (phase `scraper`), a list. Present as
   a time trade-off, not a list of vendor names:

   > The default sweep covers about 16,000 employers. Turning on the two slower
   > board types adds roughly 24,000 more, but each run takes considerably
   > longer.

   Default (`greenhouse`, `ashby`, `lever`, `direct`) is ~15,868 companies. Adding
   `workday` and `bamboohr` takes it to 40,068. Do not quote a specific
   multiplier for the extra time — nobody has timed it yet. Say "considerably
   longer" until a real timed run exists.

9. **How often to re-run**, in hours → `poll_interval_hours` on the **`scraper`**
   phase, default 4. This is what `/hireshire:start-orchestration` sweeps on; the
   monitor cannot read `${user_config.*}`, so this value is the only way the user's
   answer reaches it.

10. **Scoring backend** → `matcher.provider`.
   - **Their Claude subscription** (`claude_code`) — the default, and the reason
     this plugin exists. No API key, no per-job cost. Then ask for `model` and
     `effort` (low / medium / high / xhigh / max; medium is a good default).

     This model judges jobs during a sweep and nothing else. It has no bearing on the
     exclusions, targets or profile drafted in question 6 — those are written by
     whichever model is running this setup conversation, which is why that list gets
     confirmed before it is written.
   - **An API key** (`openai` etc.) — tell them to put the key in their
     environment and install `requirements-byo-key.txt` into the plugin venv.

11. **Auto-apply?** → `applier.enable_applier`. Default to **no**, and only turn it
    on if they ask for it. If yes, collect first name, last name, email and phone,
    and say plainly, before writing the setting:

    > Each sweep will open a browser on its own and **submit real applications** to
    > real employers, with no confirmation step. There is no rehearsal mode. The only
    > way to stop it is to set `enable_applier` back to false.

    Do not soften that. It used to be guarded by a second `dry_run` gate that filled
    forms without submitting; that gate is gone, so `enable_applier: true` means
    applications go out from the very first sweep.

## Step 3 — warm the models

Do this before declaring setup finished, so the download happens while the user
still expects to be waiting:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" scripts/setup_cli.py warm-models
```

Both are imported lazily by the engine, so without this the first
`/hireshire:find-jobs` would stall mid-run while they download. The bi-encoder gates
job titles; the cross-encoder reads each survivor's full description and decides
which are worth scoring. The cross-encoder is loaded partway through a sweep, which
is the worst possible moment to discover it is missing — hence warming it now.

## Step 4 — offer a recurring schedule (optional, opt-in)

`/hireshire:start-orchestration` only runs while the Claude Code session is open.
If the user wants sweeps to continue after they close it, offer an OS scheduler
entry running:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" --sweep
```

via `schtasks` (Windows), `launchd` (macOS) or `cron` (Linux). In the scheduler
entry use the venv interpreter's **absolute path** plus `scripts/run_orchestration.py
--once` rather than the launcher — a scheduled task runs with a minimal environment
and may not have `sh` or the same PATH.

Point it at `scripts/run_orchestration.py --once`, **not** `orchestrate.py --once`.
The former records its pid, so `--stop` can reach it and a manual sweep will not start
a second writer alongside it; the latter records nothing. Nothing about a Claude Code
session is involved either way, which is what a scheduled run needs — it has no session
behind it, and the plugin no longer asks for one.

This is a real change to their system. **Print the exact command and get explicit
confirmation before running it**, and tell them how to remove it afterwards.
Never register it silently.

## Finishing

Summarise what you configured in plain language — locations, how selective, how
many jobs per run, which boards, what happens next — and tell them to run
`/hireshire:find-jobs`. Warn that the first sweep is the slowest, because the job
database starts empty and every posting is new.

Name the workspace and show them where the first CSV will appear:

> Everything lives in `<workspace>`. Your resume is in `resume/original/`, and
> after the first search you'll find `hireshire_run_results/<date>_<time>/` with
> the results CSV in it.
