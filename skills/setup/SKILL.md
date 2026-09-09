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
backend, scoring effort, poll interval, and auto-apply. This skill already names a
default or a recommendation for almost all of them — put that option first and mark it
recommended. The user gets one tap instead of typing, and "Other" is always there for
anyone who wants something else, so offering options never narrows what they can say.

Three questions stay free text, because their answers are open-ended and a menu would
constrain them: the path to their resume, correcting the target roles you drafted in
question 6, and confirming the years of experience you read off the resume in question
7 — that one is a number to accept or correct, not a choice between options.

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

   Read it and propose concrete target roles and hard exclusions, then let them
   correct you:

   > From your resume this looks like mid-level Account Management / Customer
   > Success in SaaS. Is that the target, and is there anything you'd rule out —
   > seniority, industries, a specialisation you're done with?

   That is a far better question than asking cold, and it is faster for them. Ask this
   one as free text — the useful answer is a correction in their own words, which no
   menu can anticipate. And if a question turns out to have only one real answer, state
   it as an assumption and move on rather than inventing a second option to pad it out
   into a choice. That is a rule against fake choices, not against offering real ones.

   Then generate three things from their answer *and* the resume text:

   - `exclude_keywords` (phase `matcher`) — hard no's: wrong seniority, wrong
     specialisation, anything they said they don't want.
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
   is usually what they want.

   **Show all three back and let them edit before you write anything.** Then:

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
sh "${CLAUDE_PLUGIN_ROOT}/scripts/hireshire.sh" orchestrate.py --once
```

via `schtasks` (Windows), `launchd` (macOS) or `cron` (Linux). In the scheduler
entry use the venv interpreter's **absolute path** plus `orchestrate.py` rather
than the launcher — a scheduled task runs with a minimal environment and may not
have `sh` or the same PATH.

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
