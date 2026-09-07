# HireShire

**An automated job search that runs on your own machine, on your own Claude
subscription.** HireShire sweeps job board APIs across **24,754 employer boards**,
scores every opening against your resume, and fills out the applications.

Your resume never leaves your disk. The sweep runs from your own IP. Scoring runs
on your Claude Pro or Max subscription, so there is no API bill and no shared
rate limit.

## Install

Make a folder for your job search, put your resume in it, and open Claude Code
there:

```
hireshire_job_search/
└── resume/
    └── original/
        └── your_resume.pdf
```

Then, in Claude Code:

```
/plugin marketplace add slowloris-98/HireShire-plugin
/plugin install hireshire@hireshire
/hireshire:setup
```

That is the whole installation. No `git clone`, no terminal, no YAML.

Setup adopts the folder you launched from, finds the resume you left there, and
writes every run's results back into it. You do not have to create the folders
first — point setup at an empty folder and it builds the layout for you, copying
in a resume from wherever you keep it.

**Requirements:** Claude Code with a Pro or Max subscription (an OpenAI API key
works as an alternative), Python 3.10+, and about 3 GB of disk for the models.
Node 18+ only if you want auto-apply.

### Platform support

| | |
|---|---|
| **macOS** | Supported. Needs `python3` on PATH — `brew install python` or the python.org installer. |
| **Linux** | Supported. Some distros split out the venv module: `apt install python3-venv`. |
| **Windows** | Supported, and needs **Git Bash** (bundled with [Git for Windows](https://git-scm.com/download/win)). |

Everything mutable resolves through one module, so there are no hardcoded paths
and no shell-specific engine code — the venv is built at
`venv/bin/python` or `venv\Scripts\python.exe` as appropriate, and every
later invocation uses that interpreter by absolute path.

The Git Bash requirement on Windows is the one real constraint: the startup hook
and the recurring sweep are POSIX shell one-liners that probe for
`python3`, then `python`, then `py`. That probe is what makes the same command
work on a Mac, where there is no bare `python` at all.

## The four commands

| | |
|---|---|
| `/hireshire:setup` | One-time guided setup. Asks about ten questions in plain English and does the first-run downloads. |
| `/hireshire:find-jobs` | One sweep, scored and ranked, written to a CSV. |
| `/hireshire:start-orchestration` | Keeps sweeping on a schedule while the session is open. |
| `/hireshire:apply` | Fills out the application forms. Never submits until you say so. |

## How it decides what to score

Scoring every posting with an LLM is what makes a search this wide accurate — and
also what makes it expensive. HireShire spends that budget deliberately:

```
location + age filter    free
exclude keywords         free
bi-encoder recall net    cheap semantic check on the job title
detail hydration         fetches descriptions for the survivors that need one
cross-encoder rerank     reads full descriptions against your candidate profile
                         and drops the ones that do not clear the cutoff
LLM score                a real 0-100 verdict on what is left
```

Everything above the last line runs on your own machine and costs nothing, so the
cutoff is the only thing standing between a 10,000-employer sweep and a very large
bill.

The reranker is the part that matters. It reads your profile and the job
description *together*, so it recognises that "seeking strong frontend state
management" is asking for the React work on your resume. Keyword and
embedding filters miss that; this is built to catch it.

Every stage is a yes/no about one job, which is why results appear **while the
sweep is still running** rather than all at once at the end. A separate `top_k`
setting caps how many LLM calls any single run may make — a fuse, not the way jobs
are chosen — so a badly-set cutoff costs you a quiet run rather than your whole
Claude allowance.

Setup writes an expanded "ideal candidate" profile from your resume: not just the
words on it, but the transferable skills underneath, in the vocabulary employers
actually use. That profile is the reranker's query. It is generated from *your*
resume and *your* answers, which is why this works for any field, not just
engineering.

## Scale

| Board | Companies |
|---|---:|
| BambooHR | 8,763 |
| Workday | 6,017 |
| Greenhouse | 5,432 |
| Ashby | 2,518 |
| Lever | 2,024 |
| **Total** | **24,754** |

**The default sweep is about 10,000 of these** — Greenhouse, Ashby, Lever and the
direct portals. Workday and BambooHR are off by default because they are slow:
Workday is POST-based and BambooHR needs two requests per company. Turning them
on is one answer during setup, and it makes each run considerably longer.

Dead slugs are recorded and skipped before any HTTP call, so runs get faster over
time. Each release ships a refreshed list, and anything your own install
discovers is kept separately so an update never erases it.

## Where your data lives

Everything mutable — the SQLite database, your config, your results, the venv —
lives in `~/.claude/plugins/data/hireshire-hireshire/`. That directory survives
plugin updates. The install directory is replaced wholesale on every update and
holds only shipped, read-only content.

## Safety

The applier submits **real applications**. Two independent gates, both shipped
in the safe position:

- `enable_applier: false` — the phase does not run at all.
- `dry_run: true` — fills every form, never clicks submit.

Watch it work in dry-run and read the screenshots before you change either. The
applier will not invent experience you do not have: if a required question cannot
be answered honestly from your resume, it records an error and moves on.

## Development

```bash
claude plugin validate . --strict     # before every release
claude --plugin-dir .                 # load this repo as a plugin locally
pytest                                # the test suite
```

Running the engine directly from a checkout works too — with no plugin
environment variables set, everything falls back to `./data/`.

```bash
python scraper.py                     # sweep the boards
python matcher.py                     # score the latest scrape
python orchestrate.py --once          # both, end to end
python scripts/verify_bad_slugs.py --prune   # re-check dead slugs
```
