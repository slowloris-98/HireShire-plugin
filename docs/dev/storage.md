# Storage: the postings table

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

### Layer 2 — the engine

Two phases, each independent: own entrypoint, own `hireshire/<phase>/` subpackage,
own `config/<phase>.yaml`. All tabular data lives in one SQLite DB (WAL).

**One posting is one row**, in `postings`, keyed `(board_token, job_id)`: the scraper
upserts it, the matcher writes its scores and the judge's verdict into the same row,
and the applier reads scored rows from it and writes the outcome back. It replaced four
tables — `jobs`, `matches`, `applied` and `seen_jobs` — and the four things that are
still per-run are per-run by nature: `runs` (phase spans and stats), `run_companies`
(which employers a sweep reached), `run_progress` (the dashboards' three bars) and
`pipeline_results` (a sweep's export rows). `meta` is per install.

Six things about that table that are easy to break, each of which has its own note
elsewhere in this file:

- **No writer may be `INSERT OR REPLACE`.** `OR REPLACE` deletes the row and reinserts
  it, so a column the statement does not name comes back as its default — and one row
  now carries the scrape, the gates' verdicts, the scores and the application. Only
  `insert_jobs` inserts; `upsert_match`, `record_applied`, `record_gate_reasons`,
  `mark_seen` and the rest are `UPDATE`s against the row it made.
- **`job_json` and `match_json` must never collapse into one name.** One is the scraped
  `Job` dump, the other the `MatchResult`; they overlap on `job_id`, `board_token`,
  `title` and `absolute_url`, and `location` has a different *shape* in each. A single
  `raw_json` is what forced `_sibling_sql` to take a table alias, and merging them would
  make every blob read silently ambiguous.
- **Both key columns are declared `NOT NULL` explicitly.** In SQLite a composite
  `PRIMARY KEY` on a rowid table does not imply it, and two NULLs compare distinct — so
  without them the key admits the duplicate rows it exists to prevent.
- **`board_token` leads the key**, for write locality: the scraper upserts one
  employer's batch at a time across ~15,871 employers. The lookups that have only the
  bare id — the overview's buttons, `applied_ids`, `mark_applied_by_hand` — take
  `idx_postings_job`.
- **The wide columns come last in the row.** SQLite reads columns in order and spills
  long values to overflow pages, so a dashboard query that stops before `job_json`,
  `match_json` and `content_text` never traverses them.
- **`scored_at IS NOT NULL` (`Database._SCORED`) is what "this posting has a verdict"
  means.** The existence of a `matches` row used to imply it; a posting always has a
  row now, so every reader of the scoring columns has to say so or it counts the whole
  scrape as judged.

**The schema version is load-bearing for the first time.** `SCHEMA_VERSION = 2`, and a
file stamped higher is *refused* rather than read with the wrong shape — a downgrade is
the one case where carrying on is worse than stopping. The merge itself is gated on the
shape it finds (`PRAGMA table_info(jobs)`) rather than on the version, which is what
avoids having to answer "does an absent version mean v1, or a new file?". Three
operational consequences:

- **Stop a running sweep before updating.** A sweeper started on the old code is
  executing old SQL against its file; migrating underneath it would fail every
  statement it issues, loudly and repeatedly, mid-sweep. So `_migrate_to_postings`
  asks `sweep_pid` + `process_liveness.is_alive` first and raises
  `SchemaMigrationBlocked` with one actionable sentence. Deferring silently is not
  available, because the new readers have no old SQL to fall back on. No ancestor walk
  is needed: `run_orchestration._loop` proves no sweeper is alive, connects, and only
  *then* records its own pid, so a new sweeper always migrates with the pid file empty
  or stale.
- **The merge is one transaction, driven by hand.** Python's `sqlite3` at the default
  `isolation_level=""` opens a transaction for DML only, so `with self._conn:` would
  leave every `CREATE`/`DROP`/`ALTER` in autocommit — non-atomic while *looking*
  atomic. For the same reason it cannot use `executescript`, which issues an implicit
  COMMIT when a transaction is pending; `_statements` splits the DDL instead, with
  `sqlite3.complete_statement` rather than `str.split(";")`, because the comments in
  `_POSTINGS_DDL` contain semicolons of their own.
- **`VACUUM` is a separate, deliberate step.** `DROP TABLE` only frees pages to the
  freelist, so the file keeps its old size — about 3.8 GB of a 5.3 GB file on the
  install this was measured against. The merge sets `meta.vacuum_pending` and
  `scripts/jobs_cli.py compact` reclaims it, because `VACUUM` wants another copy's
  worth of temp space and minutes of work while `Database.__init__` is on the path of
  every process, including the report refresh and `--stop`. That subcommand is also the
  entry point `prune_runs` never had.


