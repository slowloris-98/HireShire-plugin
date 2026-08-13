---
name: setup
description: One-time guided setup for HireShire — points it at your resume, works out what roles to look for, and does the first-run downloads. Run this before find-jobs.
---

# HireShire setup

Walk the user through configuration **conversationally**. Never show them a YAML
file, never ask them to edit one, and never ask them to open a terminal. Every
value below is written for them by `hireshire.config_writer`.

## Running engine commands

Always go through the bundled launcher. Never call `python` directly: macOS has
no bare `python`, and on Windows a Microsoft Store stub named `python3` sits on
PATH but does not work. The launcher resolves a real interpreter, bootstraps the
venv if needed, and re-execs inside it.

```bash
sh "<plugin root>/scripts/hireshire.sh" <script.py> [args]
```

**One-off Python goes through the same launcher.** Write the snippet to a file and
pass its absolute path — the launcher accepts one, and this is the only way the
child gets a correct `CLAUDE_PLUGIN_DATA` and `PYTHONPATH`:

```bash
cat > /tmp/snippet.py <<'EOF'
from hireshire import config_writer
config_writer.install_user_config()
EOF
sh "<plugin root>/scripts/hireshire.sh" /tmp/snippet.py
```

Two rules behind that, both learned the hard way:

- **Never invoke the venv interpreter yourself.** It skips `run_engine.py`, which
  is what sets `PYTHONPATH` — so `import hireshire` fails — and what pins the data
  directory. Hand-rolled `export` lines get one of the two wrong.
- **`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` do not exist in your shell.**
  Claude Code sets them for hooks, not for the Bash calls you make. Substitute the
  plugin's real install path yourself, and **never guess a data directory** — a
  machine can have more than one (a marketplace install and a local load keep
  separate ones), and picking the wrong one makes a configured install look empty.
  The launcher works this out; you should not.

## Step 0 — set expectations, then install

Before anything else, tell the user what this will cost them in time:

> Setup takes about 10-15 minutes, most of it a one-time ~2.5 GB download of the
> models that decide which jobs are worth scoring. It happens now rather than in
> the middle of your first search.

Then run `scripts/bootstrap.py` if the venv isn't ready, and call
`config_writer.install_user_config()` to copy the default config into the data
directory. Everything after this edits that copy, so plugin updates never
overwrite their answers.

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

- ```python
  from hireshire import workspace, config_writer
  ws = workspace.init_workspace(r"<their answer, or cwd>")
  config_writer.write_config("scraper", {"workspace_dir": str(ws)})
  ```

`init_workspace` creates `resume/original/` and `hireshire_run_results/` if they
are missing, so a folder they made thirty seconds ago and left empty is fine. It
refuses a folder inside the plugin's own directories — those are wiped on update —
and tells them why.

Say once, plainly: **this is recorded, so results land there even if they later
launch Claude Code from somewhere else.** Re-running `/hireshire:setup` is how they
move it; results already written stay where they are.

## Step 2 — the questions

Ask these **conversationally and in small groups** — two or three at a time, not
as a ten-item form. Confirm what you understood before writing.

### How to write a value

`write_config(phase, {...})` takes **flat keys**, never the YAML nesting. The names
below are the keys; where they sit in the file is the writer's business:

| phase | keys |
|---|---|
| `scraper` | `location_filter`, `max_age_hours`, `enabled_platforms`, `workspace_dir` |
| `matcher` | `threshold`, `provider`, `model`, `effort`, `resume_path`, `search_profile_path`, `include_keywords`, `exclude_keywords` |
| `funnel` | `targets`, `top_k` |
| `applier` | `enable_applier`, `dry_run`, `resume_path`, `first_name`, `last_name`, `email`, `phone` |

So it is `write_config("matcher", {"exclude_keywords": [...]})` — **not**
`{"title_filter": {"exclude_keywords": [...]}}`, which is rejected.

Three things that trip people up:

- `matcher` and `funnel` are two whitelists over the *same* `matcher.yaml`. Writing
  one never disturbs the other, but the phase has to match the key.
- List keys (`location_filter`, `enabled_platforms`, `include_keywords`,
  `exclude_keywords`, `targets`) want a **list**, even for one item. A bare string
  is wrapped for you, but write the list.
- `config_writer.field_docs(phase)` returns the live keys and what they mean. Use it
  if this table and the code ever disagree.

1. **Resume** → `matcher.resume_path`, and the same path to `applier.resume_path`.

   Look in the workspace first with `workspace.find_resumes(ws)`. A PDF already
   sitting in `resume/original/` means the documented flow worked — offer it by
   name and confirm rather than asking for a path.

   Otherwise ask for the file and call `workspace.install_resume(path, ws)`:

   - ```python
     dest = workspace.install_resume(r"<their path>", ws)
     config_writer.write_config("matcher", {"resume_path": str(dest)})
     config_writer.write_config("applier", {"resume_path": str(dest)})
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
   phase. Ask for the number directly. Suggest 75 if they want a recommendation. Do
   not offer word-based tiers.

   The funnel has its own `encoder_threshold` — a 0-1 cosine recall net, an unrelated
   setting that setup must never touch. It is deliberately loose; raising it throws
   away the differently-worded jobs the reranker exists to catch.

5. **How many jobs to score per run** → `top_k` on the **`funnel`** phase. Default
   100. Explain the real trade-off:
   - On a **Claude subscription** the limit is prompts per 5-hour window, not
     money, so a few hundred per run is the practical ceiling.
   - On a **paid API key** it is a straight cost dial and can go higher.

   Jobs that miss the cut are recorded, not discarded, and stay eligible for the
   next run — so this is safe to raise later.

6. **Target roles.** This single step is what makes the plugin work for any field,
   and it is the main defence against missing jobs that are a real fit but worded
   differently.

   **Draft first, then ask.** By this point their resume is installed and readable —
   `extract_resume_text(dest)` from `hireshire.matcher.resume`. Read it and propose
   concrete target roles and hard exclusions, then let them correct you:

   > From your resume this looks like mid-level Account Management / Customer
   > Success in SaaS. Is that the target, and is there anything you'd rule out —
   > seniority, industries, a specialisation you're done with?

   That is a far better question than asking cold, and it is faster for them. If the
   answer really has only one option, state it as an assumption and move on — never
   pad a question out to two choices so it fits a multiple-choice tool.

   Then generate three things from their answer *and* the resume text:

   - `exclude_keywords` (phase `matcher`) — hard no's: wrong seniority, wrong
     specialisation, anything they said they don't want.
   - `targets` (phase `funnel`) — an **exhaustive** list of adjacent and synonymous
     **job titles** they are qualified for. Aim for dozens. This is a recall net;
     over-inclusion is cheap and under-inclusion loses jobs permanently.
   - `search_profile_path` (phase `matcher`) — write a dense ~200-word "ideal
     candidate" profile to `${CLAUDE_PLUGIN_DATA}/profile.md` and store the
     filename. Describe the **underlying transferable skills**, in the vocabulary
     employers use, not just the literal nouns on the resume — "React" should also
     appear as "component-based UI development" and "frontend state management".
     This text is the reranker's query and is what closes the vocabulary gap.

   `include_keywords` is optional: leave it empty unless the user wants a hard
   keyword requirement. An empty include list means the semantic gate decides, which
   is usually what they want.

   **Show all three back and let them edit before you write anything.** Then:

   - ```python
     config_writer.write_config("matcher", {
         "exclude_keywords": [...],
         "search_profile_path": "profile.md",
     })
     config_writer.write_config("funnel", {"targets": [...]})
     ```

7. **Which job boards** → `enabled_platforms` (phase `scraper`), a list. Present as
   a time trade-off, not a list of vendor names:

   > The default sweep covers about 10,000 employers. Turning on the two slower
   > board types adds roughly 15,000 more, but each run takes considerably
   > longer.

   Default (`greenhouse`, `ashby`, `lever`, `direct`) is ~9,974 companies. Adding
   `workday` and `bamboohr` takes it to 24,754. Do not quote a specific
   multiplier for the extra time — nobody has timed it yet. Say "considerably
   longer" until a real timed run exists.

8. **How often to re-run** → `scraper.poll_interval_hours`, default 4.

9. **Scoring backend** → `matcher.provider`.
   - **Their Claude subscription** (`claude_code`) — the default, and the reason
     this plugin exists. No API key, no per-job cost. Then ask for `model` and
     `effort` (low / medium / high / xhigh / max; medium is a good default).
   - **An API key** (`openai` etc.) — tell them to put the key in their
     environment and install `requirements-byo-key.txt` into the plugin venv.

10. **Auto-apply?** → `applier.enable_applier`. If yes, collect first name, last
    name, email and phone. **Leave `dry_run: true`.** Say plainly that it will
    fill forms and stop short of submitting until they change that themselves,
    after they have watched it work.

## Step 3 — warm the models

Do this before declaring setup finished, so the download happens while the user
still expects to be waiting:

```python
from sentence_transformers import SentenceTransformer, CrossEncoder
SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2").encode(["warmup"])
CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2").predict([("warmup", "warmup")])
```

Both are imported lazily by the engine, so without this the first `/hireshire:find-jobs`
would stall mid-run on a several-hundred-megabyte download.

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
