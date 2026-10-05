# HireShire — Manual Setup (no subscription, no plugin)

Run HireShire from a plain checkout to **scrape and rank jobs locally**, with no Claude /
Codex / API subscription. Ranking uses two on-device models; there is no LLM job scoring and
no auto-apply.

## 1. Prerequisites

- An optimized, refined resume (reference for writing your profile — not uploaded in this mode)
- ~3 GB of free disk space
- Python 3.10+, and Git Bash on Windows (so `sh` works)

## 2. Installation and setup

1. Clone and enter the repo:
   ```
   git clone https://github.com/slowloris-98/HireShire-plugin
   cd HireShire-plugin
   ```
2. Build the Python environment (no subscription needed; ~2 GB, first run downloads the two
   ranking models):
   ```
   sh scripts/hireshire.sh --bootstrap
   ```
3. Edit `config/scraper.yaml`:
   - `workspace_dir` — absolute path to your job-search folder (where results are written)
   - `location_filter`, title keywords, and which boards are enabled
4. Edit `config/matcher.yaml`:
   - `skip_llm: true` — scrape and rank only, no LLM scoring
   - `search_profile_path` — absolute path to your `profile.md` (see next step)
5. Create `profile.md` — a short "ideal candidate" description used only as the ranking query.
   Include target titles / seniority, core skills and tools, domains, and location preference.
   A few short paragraphs or bullets; no personal data.

   > Tip: paste your resume into claude.ai or ChatGPT and ask for an "ideal candidate profile,"
   > or write it yourself.

## 3. Run HireShire

- One sweep:
  ```
  sh scripts/hireshire.sh --sweep
  ```
- Recurring (honors `poll_interval_hours`; stop with `sh scripts/hireshire.sh --stop`):
  ```
  sh scripts/hireshire.sh --monitor
  ```

Ranked results and dashboards appear under `<workspace_dir>/hireshire_run_results/` (same
layout as the main README's section 3). Every scraped and ranked job is listed in
`<stamp>_results.csv`.

## Where things live

- **Configs you edit:** `config/*.yaml` inside the clone (a bare clone reads these shipped
  copies directly).
- **Dashboards + ranked CSV:** your `workspace_dir` → `<workspace_dir>/hireshire_run_results/…`
  (or `<clone>/data/results/` if `workspace_dir` is left blank).
- **Engine state** (database, logs, and `profile.md` if `search_profile_path` is relative):
  `<clone>/data/`.

Auto-apply is off in this mode. It needs the `claude` or `codex` CLI — see the main README.
