# HireShire

**An automated job search that runs on your own machine, on your own Claude
subscription.** HireShire sweeps job board APIs across **40,000+ employer boards**,
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
works as an alternative), Python 3.10+, and about 2 GB of disk — most of it PyTorch,
the rest the two small models the funnel uses. Node 18+ only if you want auto-apply.

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
| `/hireshire:apply` | Fills out and **submits** the application forms. Off until you turn it on — see [Safety](#safety). |

## How it decides what to score

Diagrams of the whole flow and of the funnel below: [docs/sys_arch.md](docs/sys_arch.md).

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
cutoff is the only thing standing between a 15,000-employer sweep and a very large
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

| Board | Companies | In the default sweep |
|---|---:|:--:|
| Workday | 12,884 | |
| BambooHR | 11,316 | |
| Greenhouse | 8,333 | ✓ |
| Lever | 4,369 | ✓ |
| Ashby | 3,163 | ✓ |
| Direct portals | 3 | ✓ |
| **Total** | **40,068** | **15,868** |

**The default sweep is 15,868 of these** — Greenhouse, Ashby, Lever and the direct
portals (Apple, Google, Intuit, which post outside the big platforms). Workday and
BambooHR are off by default because they are slow: Workday is POST-based and BambooHR
needs two requests per company. Turning them on is one answer during setup, and it
makes each run considerably longer.

That 40,068 is the shipped list, not a promise of 40,068 live boards. Dead slugs are
recorded and skipped before any HTTP call, so runs get faster over time and the
reachable count drifts down as your install learns. Each release ships a refreshed
list, and anything your own install discovers is kept separately so an update never
erases it.

## Where your data lives

Everything mutable — the SQLite database, your config, your results, the venv —
lives in `~/.claude/plugins/data/hireshire-hireshire/`. That directory survives
plugin updates. The install directory is replaced wholesale on every update and
holds only shipped, read-only content.

## Safety

The applier submits **real applications**, and there is exactly one gate, shipped
in the safe position:

- `enable_applier: false` — the phase does not run at all.

Turn it on and every sweep opens a browser on its own and submits applications to
real employers, with no confirmation step. There is deliberately no rehearsal mode:
a `dry_run` setting used to fill forms without submitting, but a rehearsal left on
indefinitely is indistinguishable from a broken applier, which is what it became.

Two things still limit the blast radius. `exclude_companies` skips employers whose
portals need an account login — those are listed for you to apply to by hand. And the
applier will not invent experience you do not have: if a required question cannot be
answered honestly from your resume, it records an error and moves on.

## Development

```bash
claude plugin validate . --strict     # before every release
claude --plugin-dir .                 # load this repo as a plugin locally
pytest                                # the test suite
```

When a sweep misbehaves, the launcher answers the diagnostic questions without
building anything:

```bash
sh scripts/hireshire.sh --paths       # where ROOT and DATA actually resolve to
sh scripts/hireshire.sh --status      # is a recurring sweep running?
sh scripts/hireshire.sh --stop        # stop one, killing the whole process tree
```

Running the engine directly from a checkout works too — with no plugin
environment variables set, everything falls back to `./data/`. Note that this is a
*separate* install from your real one: config written this way does not reach
`~/.claude/plugins/data/`, and the first such command builds a second venv under
`./data/`. Check `--paths` first if you meant to touch the real one.

```bash
python scraper.py                     # sweep the boards
python matcher.py                     # score the latest scrape
python orchestrate.py --once          # both, end to end
python scripts/verify_bad_slugs.py --prune   # re-check dead slugs
```
