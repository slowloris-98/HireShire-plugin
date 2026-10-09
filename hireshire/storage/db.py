"""Central SQLite storage for every HireShire phase.

One `data/hireshire.db` (WAL mode) holds all tabular data. **One posting is one row
in `postings`**, keyed `(board_token, job_id)`, carrying the scrape, the funnel
scores, the judge's verdict and the application outcome together: the scraper
upserts it, the matcher writes scores into it, the applier reads scored rows from it
and writes the outcome back. Genuine binary artifacts (tuned PDFs/tex, applier
screenshots) stay on disk and are referenced by path from the DB.

What remains per-run is per-run by nature: `runs` (phase spans and stats),
`run_companies` (which employers a sweep reached), `run_progress` (the dashboards'
three bars) and `pipeline_results` (a sweep's own export rows). `meta` is per
install.

Concurrency: a single connection per DB path is shared process-wide (see
`get_db`) and guarded by a `threading.Lock`, so all writes serialize with zero
`SQLITE_BUSY` within a process. WAL + `busy_timeout` handle the rare cross-process
writer (e.g. a standalone `matcher.py` running while the orchestrator writes).
The connection uses `check_same_thread=False` so async callers can offload
blocking writes with `await asyncio.to_thread(...)` without stalling the event
loop.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from hireshire import paths
from hireshire.models.job import Job

logger = logging.getLogger(__name__)


class SchemaMigrationBlocked(RuntimeError):
    """The one-time upgrade to `postings` cannot run right now.

    Carries one actionable sentence, because every caller prints it instead of a
    traceback: the entry points (`scripts/jobs_cli.py`, the report builders, setup)
    catch this specifically.

    Two causes, and both are honest refusals rather than failures:

    * **A sweep is running.** It was started before this update, so it is executing
      the old code against this file; migrating underneath it would fail every
      statement it issues naming `jobs`, `matches` or `applied` — loudly, repeatedly,
      mid-sweep. Deferring silently is not an option either, because the new readers
      have no old SQL left to fall back on, so the refusal has to reach the user.
      (The migration also holds the write lock for minutes against a 5,000 ms
      `busy_timeout`, which would break a live writer on its own.)
    * **Not enough free disk.** The migration appends ~1 GB of new pages before it
      drops the old tables. Without the check the failure is `SQLITE_FULL` several
      minutes in — safe, since it rolls back, but with a message nobody can act on.
    """

DEFAULT_DB_PATH = paths.DB_PATH

#: Bumped to 2 when `jobs`, `matches`, `applied` and `seen_jobs` were merged into
#: `postings`. **This is the first release in which the stored value does anything.**
#: It is recorded and *enforced* — a file stamped higher than this is refused rather
#: than read with the wrong shape — while the migration itself is gated on the shape
#: it finds (`PRAGMA table_info(jobs)`), not on the version. That split is deliberate:
#: a brand-new file has no stored version either, and "absent means v1 or means new?"
#: is a question nothing has to answer if the shape is the trigger.
SCHEMA_VERSION = 2

# Phase identifiers used in the `runs` table.
PHASE_SCRAPE = "scrape"
PHASE_MATCH = "match"
PHASE_TUNE = "tune"
PHASE_PIPELINE = "pipeline"

#: `skip_reason` for a job the user decided by hand not to pursue. Shaped exactly like
#: `location_mismatch`: a verdict reached *after* the job was scored, so it keeps its LLM
#: score and is un-shortlisted rather than written an `applied` row.
#:
#: It must not become an `applied` status. `data.overview_snapshot` sends `submitted` to
#: Jobs Applied and every other status to Needs Attention — deliberately, so a status
#: nobody has named yet cannot silently disappear — which means a "not pursuing" status
#: would sit in the one section this exists to clear.
DECLINED_BY_USER = "declined_by_user"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT NOT NULL,
    phase       TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    stats_json  TEXT,
    PRIMARY KEY (run_id, phase)
);

CREATE TABLE IF NOT EXISTS run_companies (
    run_id       TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    platform     TEXT,
    status       TEXT,
    job_count    INTEGER DEFAULT 0,
    fetch_time_s REAL,
    error        TEXT,
    PRIMARY KEY (run_id, board_token)
);

CREATE TABLE IF NOT EXISTS pipeline_results (
    run_id          TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    company         TEXT,
    title           TEXT,
    location        TEXT,
    posted_at       TEXT,   -- when the employer posted it
    job_url         TEXT,
    relevance_score INTEGER,
    encoder_score     REAL, -- bi-encoder cosine (0-1) of the title vs target roles
    rerank_score_wide REAL, -- wide-pass cross-encoder logit, every reranked job
    rerank_score      REAL, -- refined cross-encoder logit — a DIFFERENT model, so
                            -- not comparable to rerank_score_wide. This is the one
                            -- that won the job its LLM budget slot.
    found_at        TEXT,   -- when we processed it
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_run ON pipeline_results(run_id);

-- Live progress for the overview page's three bars, one row per orchestrated sweep.
-- Counters rather than a derivation from the tables above, because none of them can
-- give these numbers: the scraper's total exists only in memory, a not-found slug
-- writes no run_companies row, title-gate rejections write no matches row, and an
-- excluded or deferred application writes no applied row.
CREATE TABLE IF NOT EXISTS run_progress (
    run_id          TEXT PRIMARY KEY,
    companies_total INTEGER,            -- NULL until the scraper has loaded its lists
    companies_done  INTEGER DEFAULT 0,
    jobs_processed  INTEGER DEFAULT 0,  -- jobs whose batch the matcher has finished
    apply_enabled   INTEGER DEFAULT 0,
    apply_queued    INTEGER DEFAULT 0,  -- representatives handed to the apply worker
    apply_handled   INTEGER DEFAULT 0,  -- of those, how many the worker is done with
    updated_at      TEXT
);
"""

#: The one table. A posting, its funnel scores, the judge's verdict and the
#: application outcome, all on one row keyed `(board_token, job_id)`.
#:
#: It replaced four tables — `jobs` (keyed `(run_id, job_id)`, so the scraper wrote a
#: full copy of every posting on every sweep: 580,727 rows for 75,732 postings on a
#: real install, 3.81 GB of it re-sightings), `matches`, `applied` and `seen_jobs`.
#:
#: **`(board_token, job_id)` is also what fixes the cross-board `job_id` collision**,
#: and it fixes it *because* of the merge rather than because `jobs` gained a column.
#: `job_id` is only unique per board — Greenhouse and BambooHR both mint bare
#: integers and Workday falls back to the job *title* when a posting has no req id —
#: and the damage was never in `jobs`, whose per-run key never separated two boards
#: either. It was that `seen_jobs(job_id)` retired a posting permanently and
#: `applied(job_id)` credited an application, both on the bare id. Those keys no
#: longer exist. No id was reminted, so no application record could be mis-mapped.
#:
#: Held as one `.format(table=…)` template because the migration builds a staging
#: table that must be byte-identical to this one. Keep it free of braces.
_POSTINGS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    -- Identity. Both NOT NULL *explicitly*: in SQLite a composite PRIMARY KEY on a
    -- rowid table does not imply NOT NULL, and two NULLs compare distinct -- so
    -- without these the key would admit the duplicate rows it exists to prevent.
    board_token   TEXT NOT NULL,
    job_id        TEXT NOT NULL,

    -- The posting, as scraped. Newest sighting wins.
    source        TEXT,
    title         TEXT,
    location      TEXT,
    url           TEXT,
    updated_at    TEXT,            -- when the employer posted it
    scraped_at    TEXT,            -- the newest sighting
    -- The only per-run facts the collapse kept. `first_run_id` replaces an anti-join
    -- (`NOT EXISTS (… e.run_id < j.run_id)`) that had to derive it from 7.7 rows per
    -- posting, and the index it required -- without which one run-scope read measured
    -- 31.8s against 7ms. Lexicographic over a fixed-width UTC stamp is chronological
    -- by construction, which is what lets `insert_jobs` keep them with MIN()/MAX().
    first_run_id  TEXT NOT NULL,
    last_run_id   TEXT NOT NULL,

    -- Why the free title gate dropped this posting, and whether a verdict retired it.
    --
    -- `gate_reason` is the only record of that verdict for the tens of thousands of
    -- postings a sweep deliberately keeps out of the scoring columns entirely. One
    -- verdict per posting, not per sweep: the last sweep to gate it wins, which is
    -- what a cross-run `COALESCE(…, MAX(gate_reason))` lookup used to compute. Written
    -- by `record_gate_reasons`; `insert_jobs` must never name it.
    --
    -- `retired_at` replaces the `seen_jobs` table. Non-NULL means a verdict retired
    -- this posting and the matcher must not stream it again; NULL means never retired,
    -- or un-retired after a scoring error. A column rather than a derivation from
    -- `skip_reason`, deliberately: `_RETRYABLE_SKIP_REASONS` stays one rule in
    -- `matcher.py` instead of being restated in SQL where it could drift.
    gate_reason   TEXT,
    retired_at    TEXT,

    -- The funnel and the judge. `scored_run_id` is the sweep whose verdict this is,
    -- and is what run- and day-scope pages filter on; `source_run_id` keeps its old
    -- meaning (the run the scored job was scraped in).
    --
    -- **One row, so the row is current state rather than a per-sweep ledger.** A job
    -- the call cap deferred in sweep #1 and sweep #5 judged has one row, stamped #5:
    -- it moves off #1's dashboard onto #5's. That is the row every lifetime read
    -- already chose (newest wins, because the matcher retires a judged job, so a
    -- verdict is always the last word) -- the choosing is simply gone now.
    scored_run_id TEXT,
    source_run_id TEXT,
    scored_at     TEXT,
    -- Scores on different scales; see MatchResult for why they are never combined.
    -- encoder_score is a 0-1 cosine over the title, rerank_score a cross-encoder
    -- logit. rerank_score_wide is read-only: only runs made under the old two-model
    -- cascade wrote it, from a DIFFERENT model, so it was never comparable to
    -- rerank_score. Kept so those rows still render.
    encoder_score     REAL,
    rerank_score      REAL,
    rerank_score_wide REAL,
    -- Years of experience the POSTING asks for, read from its description by
    -- funnel/experience.py. Not the LLM's own reading of the same question, which
    -- stays in match_json as years_experience_required.
    yoe_required  REAL,
    relevance_score INTEGER,
    shortlisted   INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    skip_reason   TEXT,

    -- The application. A NULL `apply_status` means no application record at all,
    -- which is exactly what an absent `applied` row used to mean -- including for a
    -- job the user declined, which must have no record rather than a status, since
    -- every status that is not `submitted` renders under Needs Attention by design.
    --
    -- `from_backlog` says the application came off an earlier sweep's shortlist
    -- rather than the sweep that found the job. Stored because it cannot be derived:
    -- there is no run_id here, and `applied_at` against `scored_at` is a guess.
    --
    -- `apply_deferred_at` is when a session last gave up WITHOUT reaching a verdict,
    -- for lack of its browser tools. It exists only so the overview can say a
    -- shortlisted job is being retried rather than merely queued -- a deferral writes
    -- no status, so unlike the per-company cap there is nothing to recompute from.
    --
    -- It is a TIMESTAMP, and that is deliberate rather than incidental: a COUNT here
    -- would let something retire a job after N failures, which is the one thing the
    -- deferral rule forbids. A launch failure is a fact about the host, not the job
    -- (known issue S2: a machine where every launch failed at once), so counting them
    -- would discard a whole sweep's shortlist for a transient fault. Holding only
    -- "when" makes that rule unrepresentable instead of merely unused.
    --
    -- It must stay inert to every existing query: `apply_status` stays NULL, so both
    -- backlog loaders (`_unapplied`) still see the job and the retrying is unchanged.
    -- Cleared by `record_applied` and `decline_job` so the line cannot outlive its
    -- cause.
    apply_status  TEXT,
    applied_at    TEXT,
    apply_error   TEXT,
    screenshot    TEXT,
    from_backlog  INTEGER NOT NULL DEFAULT 0,
    apply_deferred_at TEXT,

    -- Wide columns LAST, deliberately. SQLite reads a row's columns in order and
    -- spills a long value to overflow pages, so a query that stops before these never
    -- traverses them -- and every dashboard query reads only the columns above.
    --
    -- **Two JSON names, never one.** `job_json` is the scraped Job dump (detail_path,
    -- detail_fetch_failed, location_is_placeholder, questions); `match_json` is
    -- MatchResult (cluster_representative, the rationales, applier_location). They
    -- overlap on job_id, board_token, title and absolute_url, and `location` has a
    -- different SHAPE in each -- a bare string in one, {{"name": …}} in the other. A
    -- single `raw_json` is what forced `_sibling_sql` to take a table alias, and
    -- collapsing them would make every blob read silently ambiguous.
    job_json      TEXT NOT NULL,
    match_json    TEXT,
    content_text  TEXT,

    PRIMARY KEY (board_token, job_id)
);
-- Board-leading, for write locality: the scraper upserts one employer's batch at a
-- time across ~15,871 employers, so a company's postings sit on adjacent pages.
-- The lookups that have only the bare id -- the overview's buttons, `applied_ids`,
-- `mark_applied_by_hand` -- take idx_postings_job instead.
CREATE INDEX IF NOT EXISTS idx_postings_job        ON {table}(job_id);
CREATE INDEX IF NOT EXISTS idx_postings_first_run  ON {table}(first_run_id);
CREATE INDEX IF NOT EXISTS idx_postings_scored_run ON {table}(scored_run_id);
CREATE INDEX IF NOT EXISTS idx_postings_shortlist  ON {table}(shortlisted, scored_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _statements(script: str) -> list[str]:
    """Split a DDL script into statements, respecting comments.

    `str.split(";")` is wrong here and was wrong once: the comments in
    `_POSTINGS_DDL` contain semicolons of their own, so it handed SQLite six
    fragments of a `CREATE TABLE` and failed with "incomplete input".
    `sqlite3.complete_statement` is the supported test — it knows a `;` inside a
    comment or a string literal does not end anything.

    This exists because the migration cannot use `executescript`: that issues an
    implicit COMMIT when a transaction is pending, which would silently end the one
    the whole merge depends on.
    """
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [st for st in out if st.strip() and not _is_only_comment(st)]


def _is_only_comment(statement: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--")
               for line in statement.splitlines())


class Database:
    """Thin wrapper over a single sqlite3 connection with the HireShire schema."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        # Anchor relative paths under the plugin data dir. Without this the
        # `mkdir` below would create a `data/` tree wherever cwd happens to be,
        # and a plugin's cwd is whatever project the user is sitting in.
        self.path = paths.resolve_data(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._apply_pragmas()
        self._init_schema()

    # -- setup ---------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()
        self._has_json1 = self._probe_json1()

    def _probe_json1(self) -> bool:
        """Whether this interpreter's SQLite can read inside a JSON column.

        JSON1 is compiled in by default from SQLite 3.38 (2022) but was an opt-in
        extension before that, and the interpreter here is whatever the user had —
        the launcher takes the first Python ≥ 3.10 it can run, not a version this
        project chose. Probed rather than assumed, because the alternative is a
        query that raises mid-sweep on somebody else's machine.
        """
        try:
            self._conn.execute("SELECT json_extract('{\"a\":1}', '$.a')").fetchone()
        except sqlite3.Error:
            logger.debug("SQLite has no JSON1; falling back to text matching")
            return False
        return True

    # Columns added to existing tables after the first release. `CREATE TABLE IF NOT
    # EXISTS` is a no-op on a database that already has the table, so a new column in
    # _SCHEMA above never reaches an existing file — the INSERT then fails with an
    # opaque "no such column". Listed here, they are added on connect instead.
    #
    # Four of these name tables the `postings` merge deleted, and they stay because
    # the **migration still has to read those tables** on a v1 file: it cannot select
    # `matches.rerank_score` or `applied.from_backlog` off a file old enough to lack
    # them. `_add_missing_columns` skips a table that is not there, so on a migrated
    # file they are simply no-ops.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("matches", "encoder_score", "REAL"),
        ("matches", "rerank_score_wide", "REAL"),
        # `matches` never had this one: the rerank score used to live only in
        # pipeline_results and inside raw_json, so it is new here too.
        ("matches", "rerank_score", "REAL"),
        # Years the posting asks for, read by funnel/experience.py. Distinct from the
        # LLM's own `years_experience_required`, which lives in raw_json.
        ("matches", "yoe_required", "REAL"),
        ("pipeline_results", "encoder_score", "REAL"),
        ("pipeline_results", "rerank_score_wide", "REAL"),
        # Applications made off the backlog. Rows written before this column existed
        # take the default, which reads as "not known to be backlog" — the fact was
        # never recorded for them and cannot be reconstructed.
        ("applied", "from_backlog", "INTEGER NOT NULL DEFAULT 0"),
        # The title gate's verdict, for the jobs that never get a `matches` row.
        # NULL on rows written before it existed, which the overview renders as an
        # em dash: the reason was never stored and cannot be reconstructed.
        ("jobs", "gate_reason", "TEXT"),
        # The FIRST entry here naming `postings` rather than a table the merge
        # deleted. `CREATE TABLE IF NOT EXISTS` is a no-op on an already-migrated v2
        # file, so a column added to `_POSTINGS_DDL` alone never reaches one. Nullable
        # and additive, so `SCHEMA_VERSION` stays 2: bumping it would make every older
        # install refuse the file, and nothing about the shape has changed.
        ("postings", "apply_deferred_at", "TEXT"),
    )

    def _init_schema(self) -> None:
        """Bring the file to the current schema, in an order that matters.

        1. **Refuse a file from the future** before touching it. Nothing else in this
           project reads `meta.schema_version`; this is the first thing that does, and
           reading a newer shape with these queries would produce wrong answers rather
           than errors.
        2. Create what is missing — the surviving per-run tables, then `postings`.
           On a v1 file the `postings` script is the only part that does anything.
        3. `_add_missing_columns`, which must run **before** the migration: it is what
           guarantees the v1 columns the migration selects actually exist.
        4. Migrate, gated on the shape rather than the version (see `SCHEMA_VERSION`).
        5. Stamp the version. `INSERT OR REPLACE`, not `OR IGNORE` — a v1 file already
           has the row, and leaving it at 1 would re-arm step 4 on every connect.

        **`postings` is created here only when there is nothing to migrate.** On a v1
        file the migration creates it, under a staging name it then renames, and the
        index names in `_POSTINGS_DDL` are fixed — so an empty `postings` made here
        first would own `idx_postings_job` and the staging table could not have it.
        Leaving it to the migration also means a refusal (a live sweep, no disk) leaves
        the file exactly as it was, with no half-made table in it.
        """
        self._check_schema_version()
        legacy = self._table_exists("jobs")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if not legacy:
                self._conn.executescript(_POSTINGS_DDL.format(table="postings"))
            self._add_missing_columns()
            self._conn.commit()
        if legacy:
            self._migrate_to_postings()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _check_schema_version(self) -> None:
        """Refuse a file written by a newer HireShire than this one.

        A downgrade is the one case where carrying on is worse than stopping: the
        queries below would run without error against a shape they do not understand.
        A missing `meta` table, a missing row or an unparseable value all mean "no
        claim", which is what every file before this release looks like.
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return  # no `meta` table yet — a new file, or one older than it
        if row is None:
            return
        try:
            stored = int(str(row["value"]).strip())
        except (TypeError, ValueError):
            return
        if stored > SCHEMA_VERSION:
            raise SchemaMigrationBlocked(
                f"{self.path.name} was written by a newer version of HireShire "
                f"(database schema {stored}, this build understands {SCHEMA_VERSION}). "
                "Update the plugin, or point it at a different data directory."
            )

    def _add_missing_columns(self) -> None:
        """Bring an older database file up to the current column set.

        Deliberately additive only: this never drops or retypes a column, so it
        cannot lose data. Called under the lock from _init_schema."""
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table not created yet — _SCHEMA above already has it
            if column not in existing:
                logger.info("Adding column %s.%s to %s", table, column, self.path.name)
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _table_exists(self, name: str) -> bool:
        try:
            return bool(
                self._conn.execute(f"PRAGMA table_info({name})").fetchone()
            )
        except sqlite3.Error:
            return False

    # -- the one-time merge into `postings` -----------------------------------
    #
    # The four tables this folds together (`jobs`, `matches`, `applied`, `seen_jobs`)
    # are dropped at the end of it, so everything below runs once per database, ever.
    # It is kept here rather than in a `migrations/` tree because it is the only one,
    # and because `_init_schema` has to be able to run it before any reader sees the
    # file: no reader can straddle both shapes, so there is no lazy option.

    #: What the migration wants free on the volume before it starts: one more copy of
    #: the file. The new rows are only ~20% of it, but `BEGIN IMMEDIATE` means the WAL
    #: carries every page the insert touches, and `SQLITE_FULL` several minutes in —
    #: safe, since it rolls back, but with a message nobody can act on — is the failure
    #: this avoids. Deliberately generous: being refused with a number is recoverable.
    _MIGRATION_HEADROOM = 1.0

    def _migrate_to_postings(self) -> None:
        """Collapse `jobs`/`matches`/`applied`/`seen_jobs` into one row per posting.

        **One transaction, including the version stamp's companion `DROP`s.** SQLite's
        DDL is transactional, so an interrupted sweep — or `--stop`, which is
        `taskkill /F` and runs no `finally` — leaves a clean v1 file with every
        original row, and the next open simply retries. `postings_migrating` therefore
        cannot be left behind and there is no cleanup path to get wrong.

        **The transaction is driven by hand, not with `with self._conn:`.** This is the
        easiest thing here to get wrong: Python's `sqlite3` at the default
        `isolation_level=""` opens a transaction for DML only, so the context manager
        would leave every `CREATE`/`DROP`/`ALTER` in autocommit — non-atomic while
        *looking* atomic. `BEGIN IMMEDIATE` also takes the write lock up front, so a
        competing writer fails fast rather than after the expensive insert.

        What it must preserve, in order of how loudly it would be noticed:

        * **`gate_reason`** — `MAX` over the non-empty values for a posting, which is
          literally what `load_unmatched_jobs` used to compute across runs. So no
          page's `Reason` column changes across the upgrade, and that column is
          thousands of rows on a real sweep.
        * **the newest sighting's payload** — title, location, url, `updated_at`,
          `job_json`. Newest is the one still true, the same rule every lifetime read
          already applied to `matches`.
        * **a description a later failed hydration would have lost** — `content_text`
          falls back to the newest non-NULL older sighting, mirroring `insert_jobs`'
          `COALESCE`. Writer and migration have to agree, or one of them loses text.
        * **the newest verdict**, which is the one-time execution of the
          `MAX(scored_at)` rule that `_canonical_matches_sql` used to run on every read.
        * **the application and the retirement**, which have no `run_id` to resolve.
        """
        started = time.monotonic()
        self._refuse_migration_if_unsafe()
        legacy_rows = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        logger.info(
            "Upgrading %s: merging %d job sightings into one row per posting. "
            "This runs once and may take a few minutes.",
            self.path.name, legacy_rows,
        )
        with self._lock:
            # Hand-rolled, for the reason in the docstring. `isolation_level = None`
            # puts the connection in autocommit so BEGIN/COMMIT mean exactly what they
            # say; it is restored either way.
            previous_isolation = self._conn.isolation_level
            self._conn.isolation_level = None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                collisions, orphans = self._run_migration_statements()
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('vacuum_pending', '1')"
                )
                self._conn.execute("COMMIT")
            except BaseException:
                # Includes KeyboardInterrupt on purpose: a half-applied merge is the
                # one outcome there is no recovering from.
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._conn.isolation_level = previous_isolation

        kept = self._conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
        logger.info(
            "Upgraded %s in %.1fs: %d sightings -> %d postings. "
            "%d job ids are claimed by more than one board; %d legacy rows could not "
            "be matched to a posting. Disk space is reclaimed separately.",
            self.path.name, time.monotonic() - started, legacy_rows, kept,
            collisions, orphans,
        )

    def _refuse_migration_if_unsafe(self) -> None:
        """Both reasons not to start: a live sweep, and not enough disk.

        The sweep check is the same `sweep_pid` + `is_alive` pair `bootstrap.py` uses
        to refuse swapping torch under a running sweep, and it is trustworthy for the
        same reason: that pid is one the sweeper wrote about *itself*, so no identity
        is being guessed at — the mistake that cost two earlier designs a user's work.

        **No ancestor walk is needed**, because of the order in
        `run_orchestration._loop`: its duplicate guard establishes that no sweeper is
        alive, it connects to the database next, and only *then* records its own pid.
        So a new sweeper always migrates with the pid file empty or stale, and every
        later connect — its own, and any `run_engine` child's — finds a migrated file
        and never reaches this check. The `os.getpid()` clause is belt for a second
        connection inside a sweeper that did record itself first.
        """
        import os

        from hireshire import sweep_pid
        from hireshire.process_liveness import is_alive

        pid = sweep_pid.read()
        if pid is not None and pid != os.getpid() and is_alive(pid):
            raise SchemaMigrationBlocked(
                f"HireShire needs a one-time database upgrade, and a sweep is running "
                f"(pid {pid}) that was started before this update. Stop it first with "
                f"`sh scripts/hireshire.sh --stop`, then run this again."
            )

        try:
            size = self.path.stat().st_size
            free = shutil.disk_usage(self.path.parent).free
        except OSError:
            return  # cannot tell; the transaction's own rollback is the fallback
        if free < size * self._MIGRATION_HEADROOM:
            raise SchemaMigrationBlocked(
                "HireShire needs a one-time database upgrade and there is not enough "
                f"free disk space: it needs about {size / 2**30:.1f} GB free on "
                f"{self.path.parent}, and {free / 2**30:.1f} GB is available. The "
                "upgrade reclaims more than that once it finishes."
            )

    def _run_migration_statements(self) -> tuple[int, int]:
        """The merge proper. Returns `(cross-board ids, unmatchable legacy rows)`.

        Called inside the open transaction, under the lock.
        """
        conn = self._conn
        # An empty `postings` can only exist if a previous attempt was interrupted
        # after the rename but before the stamp, which the single transaction makes
        # impossible — but dropping an *empty* one costs nothing and keeps the index
        # names free, and a non-empty one means the shape detection was wrong, which
        # must not be papered over.
        if self._table_exists("postings"):
            if conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]:
                raise SchemaMigrationBlocked(
                    f"{self.path.name} holds both the old `jobs` table and a non-empty "
                    "`postings` table. Restore a backup, or move this file aside."
                )
            conn.execute("DROP TABLE postings")

        # How many ids two boards both claim. Reported, not fixed: the composite key
        # is the fix, and this is the number that says whether it ever mattered here.
        collisions = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT job_id FROM jobs GROUP BY job_id"
            "   HAVING COUNT(DISTINCT COALESCE(board_token,'')) > 1)"
        ).fetchone()["n"]

        # Fill in a blank `board_token` on the two tables that are about to be joined
        # by it, from the posting's own newest sighting. `upsert_match` and
        # `record_applied` have always written one, so this is for rows old enough to
        # predate that; a row still blank afterwards is counted as an orphan below.
        # Where two boards claim the id this picks the newer, which is the same guess
        # the old job-id-only keys made implicitly.
        for table in ("matches", "applied"):
            if not self._table_exists(table):
                continue
            conn.execute(
                f"UPDATE {table} SET board_token = ("
                "    SELECT j.board_token FROM jobs j WHERE j.job_id = "
                f"      {table}.job_id AND j.board_token IS NOT NULL "
                "      AND j.board_token <> '' ORDER BY j.run_id DESC LIMIT 1) "
                "WHERE board_token IS NULL OR board_token = ''"
            )

        # **Not `executescript`**, which issues an implicit COMMIT when a transaction
        # is pending and would therefore end the one this depends on, silently. See
        # `_statements` for why the split is not `str.split(";")`.
        for statement in _statements(_POSTINGS_DDL.format(table="postings_migrating")):
            conn.execute(statement)

        # A legacy table the file never had contributes nothing rather than failing
        # the whole merge: `_SCHEMA` always created all four, but a hand-built or
        # partially restored file is exactly when a migration must not be brittle.
        matches_join = (
            "LEFT JOIN (SELECT COALESCE(board_token,'') AS bt, job_id, run_id,"
            "                  source_run_id, MAX(scored_at) AS scored_at,"
            "                  relevance_score, encoder_score, rerank_score,"
            "                  rerank_score_wide, yoe_required, shortlisted, skipped,"
            "                  skip_reason, raw_json"
            "             FROM matches GROUP BY bt, job_id) m"
            "  ON m.bt = COALESCE(j.board_token,'') AND m.job_id = j.job_id "
            if self._table_exists("matches") else
            "LEFT JOIN (SELECT NULL AS run_id, NULL AS source_run_id,"
            "                  NULL AS scored_at, NULL AS relevance_score,"
            "                  NULL AS encoder_score, NULL AS rerank_score,"
            "                  NULL AS rerank_score_wide, NULL AS yoe_required,"
            "                  NULL AS shortlisted, NULL AS skipped,"
            "                  NULL AS skip_reason, NULL AS raw_json) m ON 0 "
        )
        applied_join = (
            "LEFT JOIN applied a"
            "  ON COALESCE(a.board_token,'') = COALESCE(j.board_token,'')"
            " AND a.job_id = j.job_id"
            if self._table_exists("applied") else
            # No `applied` table: every apply column stays NULL, which is what "no
            # application record" means everywhere else.
            "LEFT JOIN (SELECT NULL AS status, NULL AS applied_at, NULL AS error,"
            "                  NULL AS screenshot, 0 AS from_backlog,"
            "                  NULL AS job_id) a ON 0"
        )

        # One pass, one row per posting. The aggregate over `jobs` is its own subquery
        # because `MIN` and `MAX` in a single group void SQLite's bare-column
        # guarantee: the payload therefore comes from the join to the newest sighting
        # (`r.last_run_id = j.run_id`), which the `(run_id, job_id)` primary key makes
        # unique, so the join cannot tie. The `matches` aggregate *does* keep that
        # guarantee -- `MAX(scored_at)` is the only aggregate in it -- which is what
        # lets the verdict's twelve columns come back in one pass.
        conn.execute(
            "INSERT INTO postings_migrating ("
            "  board_token, job_id, source, title, location, url, updated_at,"
            "  scraped_at, first_run_id, last_run_id, gate_reason,"
            "  scored_run_id, source_run_id, scored_at, encoder_score, rerank_score,"
            "  rerank_score_wide, yoe_required, relevance_score, shortlisted, skipped,"
            "  skip_reason, apply_status, applied_at, apply_error, screenshot,"
            "  from_backlog, job_json, match_json, content_text) "
            "SELECT COALESCE(j.board_token,''), j.job_id, j.source, j.title,"
            "       j.location, j.url, j.updated_at, j.scraped_at,"
            "       r.first_run_id, r.last_run_id, r.gate_reason,"
            "       m.run_id, m.source_run_id, m.scored_at, m.encoder_score,"
            "       m.rerank_score, m.rerank_score_wide, m.yoe_required,"
            "       m.relevance_score, COALESCE(m.shortlisted, 0),"
            "       COALESCE(m.skipped, 0), m.skip_reason,"
            "       a.status, a.applied_at, a.error, a.screenshot,"
            "       COALESCE(a.from_backlog, 0),"
            "       j.raw_json, m.raw_json,"
            "       COALESCE(j.content_text, ("
            "           SELECT c.content_text FROM jobs c"
            "            WHERE c.job_id = j.job_id"
            "              AND COALESCE(c.board_token,'') = COALESCE(j.board_token,'')"
            "              AND c.content_text IS NOT NULL"
            "            ORDER BY c.run_id DESC LIMIT 1)) "
            "FROM jobs j "
            "JOIN (SELECT COALESCE(board_token,'') AS bt, job_id,"
            "             MIN(run_id) AS first_run_id, MAX(run_id) AS last_run_id,"
            "             MAX(CASE WHEN gate_reason <> '' THEN gate_reason END)"
            "               AS gate_reason"
            "        FROM jobs GROUP BY bt, job_id) r"
            "  ON r.bt = COALESCE(j.board_token,'') AND r.job_id = j.job_id"
            " AND r.last_run_id = j.run_id "
            + matches_join
            + applied_join
        )

        # `seen_jobs` has no board token at all, so an id two boards claim cannot be
        # resolved. Retire NEITHER: retiring both could permanently discard a posting
        # nothing ever judged, while retiring neither re-judges one posting once, which
        # costs a few LLM calls. The correlated count rides `idx_postings_job`, which
        # the DDL above already created on the staging table.
        if self._table_exists("seen_jobs"):
            conn.execute(
            "UPDATE postings_migrating SET retired_at = ("
            "    SELECT s.first_seen FROM seen_jobs s WHERE s.job_id ="
            "      postings_migrating.job_id) "
            "WHERE EXISTS (SELECT 1 FROM seen_jobs s"
            "              WHERE s.job_id = postings_migrating.job_id)"
            "  AND (SELECT COUNT(*) FROM postings_migrating q"
            "       WHERE q.job_id = postings_migrating.job_id) = 1"
            )

        # Legacy rows with nothing to attach to: a verdict or an application whose
        # posting has no surviving `jobs` row (its sweeps were pruned). Counted rather
        # than rescued -- there is no posting left to render them against -- and
        # reported, because silently dropping an application record would be the worst
        # thing this migration could do.
        orphans = 0
        for table, alias in (("matches", "m"), ("applied", "a")):
            if not self._table_exists(table):
                continue
            orphans += conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} {alias} WHERE NOT EXISTS ("
                "   SELECT 1 FROM postings_migrating p"
                f"   WHERE p.job_id = {alias}.job_id"
                f"     AND p.board_token = COALESCE({alias}.board_token,''))"
            ).fetchone()["n"]

        for table in ("jobs", "matches", "applied", "seen_jobs"):
            if self._table_exists(table):
                conn.execute(f"DROP TABLE {table}")
        # The indexes were created on the staging table under their canonical names
        # and follow it through the rename, so nothing re-creates them here.
        conn.execute("ALTER TABLE postings_migrating RENAME TO postings")
        return collisions, orphans

    def vacuum(self) -> int:
        """Reclaim the space the merge freed. Returns bytes recovered.

        **Deliberately not called on connect.** `DROP TABLE` only moves pages to the
        freelist, so the file stays its old size until this runs — but `VACUUM` needs
        another copy's worth of temp space and minutes of work, and
        `Database.__init__` is on the path of every process, including the report
        refresh and `--stop`. So the merge sets `meta.vacuum_pending` and this is a
        deliberate step the user takes (`scripts/jobs_cli.py compact`).

        `VACUUM` cannot run inside a transaction, which is why this sets
        `isolation_level = None` rather than using the usual context manager.
        """
        before = self.path.stat().st_size
        with self._lock:
            previous_isolation = self._conn.isolation_level
            self._conn.isolation_level = None
            try:
                self._conn.execute("VACUUM")
            finally:
                self._conn.isolation_level = previous_isolation
            self._conn.execute("DELETE FROM meta WHERE key = 'vacuum_pending'")
            self._conn.commit()
        return max(0, before - self.path.stat().st_size)

    def vacuum_pending(self) -> bool:
        """Whether the merge has freed space nothing has reclaimed yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'vacuum_pending'"
            ).fetchone()
        return row is not None and str(row["value"]) == "1"

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- runs ----------------------------------------------------------------

    def latest_run(self, phase: str) -> Optional[str]:
        """Most recent completed run_id for a phase, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id FROM runs WHERE phase=? ORDER BY started_at DESC LIMIT 1",
                (phase,),
            ).fetchone()
        return row["run_id"] if row else None

    def run_exists(self, run_id: str, phase: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM runs WHERE run_id=? AND phase=?", (run_id, phase)
            ).fetchone()
        return row is not None

    def finalise_run(
        self,
        run_id: str,
        phase: str,
        started_at: str,
        finished_at: str | None = None,
        stats: dict | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, phase, started_at, finished_at, stats_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, phase, started_at, finished_at or now_iso(),
                 json.dumps(stats or {}, default=str)),
            )

    # -- reporting reads -----------------------------------------------------
    #
    # The four below back `hireshire.reporting`. They are counts rather than row
    # loads on purpose: they are called repeatedly *during* a sweep to refresh a
    # live page, so none of them may pull a run's worth of rows into memory.
    # `load_all_matches` remains the row-level read, and is called once per
    # refresh only after the matches actually exist.

    def scrape_counts(self, run_id: str) -> dict[str, int]:
        """How far the sweep has got. Cheap enough to poll every few seconds.

        `companies` counts what has been *recorded*, which grows through the run —
        that is the point, and it is why this is not read out of the scrape phase's
        `stats_json`, which does not exist until the phase finishes.

        **`jobs` is the sum of `run_companies.job_count`, not a count of rows.** It
        used to be `COUNT(*) FROM jobs WHERE run_id=?`, which `postings` can no longer
        answer: a posting has one row however many sweeps found it, so there is no
        per-run sighting count in that table by design. `record_company` already
        writes `len(jobs)` per employer per run, so the figure comes from the one place
        that still knows it — and it costs a primary-key range scan instead of the
        39.6s full count that query had become on a real install.

        Two differences worth knowing. It counts **sightings per employer**, so one
        `job_id` appearing in two employers' batches counts twice where the old
        `(run_id, job_id)` key collapsed it to one; nothing downstream divides by this,
        and `run_snapshot`'s `gated_out` is already clamped at zero. And
        `record_company` runs *before* `insert_jobs` in `RunStore.save_company`, so
        mid-sweep the figure now runs one employer ahead of the rows rather than one
        behind.
        """
        with self._lock:
            companies = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "       SUM(CASE WHEN job_count > 0 THEN 1 ELSE 0 END) AS with_jobs, "
                "       SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS errors, "
                "       COALESCE(SUM(job_count), 0) AS jobs "
                "FROM run_companies WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "companies": companies["n"] or 0,
            "companies_with_jobs": companies["with_jobs"] or 0,
            "errors": companies["errors"] or 0,
            "jobs": companies["jobs"] or 0,
        }

    def match_counts(self, run_id: str) -> dict[str, int]:
        """The funnel's lower half, grouped by what happened to each job.

        `by_reason` is keyed by `skip_reason` with the empty string standing in for
        NULL, so callers can tell "scored" from "dropped for reason X" without a
        second query. Note that title-gate rejections are deliberately absent —
        they carry `gate_reason` instead and are never counted here, so the top of
        the funnel comes from `scrape_counts`.

        `scored_run_id = ?` is what "this run's matches" means now: a posting holds
        one verdict, stamped with the sweep that reached it. A posting this sweep
        scraped but an earlier sweep judged is therefore absent, which is right — this
        sweep did no scoring work on it.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(skip_reason, '') AS reason, COUNT(*) AS n "
                "FROM postings WHERE scored_run_id=? GROUP BY reason",
                (run_id,),
            ).fetchall()
            totals = self._conn.execute(
                "SELECT COUNT(*) AS rows_total, "
                "       SUM(CASE WHEN shortlisted=1 THEN 1 ELSE 0 END) AS shortlisted, "
                "       SUM(CASE WHEN relevance_score IS NOT NULL AND "
                "                     (skip_reason IS NULL OR skip_reason='') "
                "                THEN 1 ELSE 0 END) AS scored, "
                "       MAX(CASE WHEN skip_reason IS NULL OR skip_reason='' "
                "                THEN relevance_score END) AS top_score "
                "FROM postings WHERE scored_run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "rows_total": totals["rows_total"] or 0,
            "scored": totals["scored"] or 0,
            "shortlisted": totals["shortlisted"] or 0,
            "top_score": totals["top_score"],
            "by_reason": {r["reason"]: r["n"] for r in rows},
        }

    def _sibling_sql(self) -> str:
        """SQL for "this row inherited its verdict from a cluster representative".

        There is no column for it: `cluster_representative` lives inside `match_json`.
        And `skip_reason` cannot stand in for it — a sibling inherits the
        *representative's* reason, so a cluster whose representative failed carries
        `backend_unavailable` on all of its members and only the lucky ones say
        `duplicate_of_cluster`. Testing the reason instead dropped six judged jobs
        into the never-scored table on a real database.

        `json_extract` is the correct test and ships enabled in SQLite 3.38+, but
        this runs on whatever interpreter the user has, so it is probed once at
        connect. The fallback matches the key with a *string* value, which `null`
        cannot satisfy — sound against the compact `model_dump_json` output that
        writes every one of these rows.

        **It takes no alias any more, and that is the point of `match_json` having
        its own name.** It used to, because `jobs` and `matches` each had a `raw_json`
        column and this predicate was ambiguous in any query joining the two. One
        table with two differently-named blobs removes the ambiguity rather than
        parameterising around it — and `job_json` must never be renamed back.
        """
        if self._has_json1:
            return "json_extract(match_json, '$.cluster_representative') IS NOT NULL"
        return "match_json LIKE '%\"cluster_representative\":\"%'"

    def _relevant_sql(self) -> str:
        """Rows that survived every free gate — the overview page's "relevant" figure.

        Both LLM-free verdicts are excluded: `rerank_below_cutoff` (the cross-encoder
        read the description and said no) and `yoe_below_requirement` (the posting
        asks for more years than the resume shows). Cluster siblings are out too, for
        the reason the matcher leaves them out: they were grouped after the rerank and
        never competed for a slot.

        What stays in is deliberate. A row that cleared both gates and then failed for
        a reason that is not about relevance — `llm_call_cap_reached` (a deferral; the
        job returns next sweep), `api_error`, `no_content_text` — *is* relevant. The
        run merely ran out of calls or broke.

        This is computed from rows rather than read out of the phase blob because that
        blob is only written when the matcher finalises and the page has to be right
        mid-sweep. Note it therefore **no longer reproduces the matcher's own
        `stages["above_cutoff"]`**, which counts YoE drops in on purpose — see the YoE
        section of CLAUDE.md. The tile and `matcher.py`'s `above cutoff → judged`
        console line are answering different questions and will disagree.
        """
        return (
            "COALESCE(skip_reason,'') "
            "NOT IN ('rerank_below_cutoff','yoe_below_requirement') "
            f"AND NOT ({self._sibling_sql()})"
        )

    def _judged_sql(self) -> str:
        """A SQL mirror of `hireshire.reporting.data._never_scored`, negated.

        "A standing LLM verdict backs this row's relevance_score" — either the row
        was scored directly, or it is a cluster sibling and was scored by proxy. The
        two implementations must agree; `tests/test_overview.py` pins them together
        against a fixture that includes a sibling with an inherited drop reason.
        """
        return (
            "relevance_score IS NOT NULL AND "
            f"(skipped = 0 OR skipped IS NULL OR {self._sibling_sql()})"
        )

    def _new_work_sql(self, run_id: str | None, ids: Sequence[str],
                      run_id_set_sql: str | None = None) -> str:
        """`COUNT(*)` over the postings a scope did work on.

        The `Jobs in scope` tile and the matcher bar's denominator are the same number
        by contract (see `run_progress`), so the rule is written once. "Did work on"
        means **first saw**, or **reached a verdict on**, and both are now columns:
        `first_run_id` and `scored_run_id`.

        **The two halves must stay a top-level `OR`, never a conjunct.** The old form
        read `FROM jobs j WHERE j.run_id = ? AND (first_sighting OR judged-in-scope)`,
        where the scope restricted the whole predicate because the verdict half needed
        a `jobs` row in that same run to hang off. With one row per posting that shape
        collapses to `first_run_id = ?` and the second half disappears silently. It is
        there because a job the call cap deferred in sweep #1 and sweep #5 finally
        judged is real work #5 did, and #5's Relevant and Jobs Filtered sections list
        it; counting only first sightings would put the top of the funnel *below* the
        lists beneath it, which is the one thing a funnel tile must never do.

        It is also what the matcher bar's numerator counts: `matcher.py` bumps
        `jobs_processed` with the jobs not already retired, which is this set clause for
        clause -- a title-gate verdict retires a posting, a cap drop deliberately does
        not. The two must change together.

        `COUNT(*)` rather than `COUNT(DISTINCT job_id)`: the row is unique per posting,
        so the day-scope case the `DISTINCT` existed for -- one posting qualifying
        through its first-sighting row in one of the day's sweeps and its match row in
        another -- cannot arise. Both halves are index-backed
        (`idx_postings_first_run`, `idx_postings_scored_run`).

        At lifetime scope with no filter every posting qualifies through its first
        sighting, so it short-circuits to a bare count of the table -- the number the
        lifetime page printed before, unchanged by construction. `run_id_set_sql` is
        the third scope, a parameterless subquery (`lifetime_progress`'s tracked
        sweeps); it replaced a `.replace("WHERE 1=1", …)` on the rendered string.

        The caller owns the parameters, and the scope ids appear **twice** -- once per
        half -- which is why every call site passes `params + params`.
        """
        if run_id:
            first, judged = "first_run_id = ?", "scored_run_id = ?"
        elif ids:
            first = self._in_clause(ids, "first_run_id")
            judged = self._in_clause(ids, "scored_run_id")
        elif run_id_set_sql:
            first = f"first_run_id IN ({run_id_set_sql})"
            judged = f"scored_run_id IN ({run_id_set_sql})"
        else:
            return "SELECT COUNT(*) AS n FROM postings"
        return f"SELECT COUNT(*) AS n FROM postings WHERE {first} OR {judged}"

    @staticmethod
    def _in_clause(run_ids: Sequence[str], column: str = "scored_run_id") -> str:
        """``column IN (?,?,…)`` for a day's worth of run ids. Spelled once.

        The caller has already established that `run_ids` is neither None nor empty;
        an empty list must never reach here, because `IN ()` is a SQLite syntax error
        and every reader short-circuits on it instead.
        """
        return f"{column} IN ({','.join('?' * len(run_ids))})"

    # `_canonical_matches_sql` used to live here, and the merge deleted it. It wrapped
    # every lifetime read in `MAX(m.scored_at) … GROUP BY m.job_id`, because `matches`
    # was keyed `(run_id, job_id)` and a job dropped on a deferral — the call cap, a
    # scoring failure — kept that row when a later sweep judged it. Every reader then
    # had to choose, and choosing wrong was expensive: counting "any row that ever said
    # so" listed 381 jobs twice on the lifetime page and left 8 more in the `Relevant
    # jobs` tile on a reading the cross-encoder had since overturned.
    #
    # One row per posting *is* that choice, made once at write time, and it is the same
    # choice: newest wins, because the matcher retires a judged job, so a verdict is
    # always the last word and only a later sweep can supersede a deferral. The
    # refinement that looked safer — prefer a row with a standing verdict — stays
    # rejected, and is now unrepresentable: `_judged_sql` is also true of a cluster
    # sibling whose representative *failed*, which carries a placeholder 0 and no
    # verdict behind it.
    #
    # Two rules it carried are gone with it rather than relocated, and neither should
    # be reinvented when something looks asymmetric: predicates on the outer select,
    # scope filters inside the aggregate.

    def overview_counts(self, run_id: str | None = None,
                        run_ids: Sequence[str] | None = None) -> dict[str, int]:
        """The overview page's four figures, at one of the three scopes.

        One posting is one row, so these are counts of postings at every scope — a job
        that resurfaced in several sweeps is one job on the lifetime page, as it was
        before, but now by construction rather than through an aggregate.

        `seen` — the `Jobs in scope` tile — counts the postings this scope did **work**
        on, through `_new_work_sql`: first saw, or reached a verdict on. It is
        deliberately **wider** than the last section's list, which shows first sightings
        only — see `load_unmatched_jobs`, and do not reconcile the two.

        **The three scopes now ask the same question of the same row**, which is the
        whole point of the merge: the per-scope divergence this method used to carry —
        a run-scoped `COUNT(DISTINCT job_id)` over `matches` versus a lifetime count
        over `_canonical_matches_sql` — existed only because `matches` held one row per
        sweep per job, so "did any row ever say so" and "does the current row say so"
        were different answers. Now the tile and the list below it read one row, and
        cannot disagree about a job's state.

        The verdict figures filter on `scored_run_id`, the sweep whose verdict this is.
        The sections on a run page therefore list what *that* sweep judged — so a job
        it deferred and a later sweep judged has moved on to the later sweep's page.

        None of this changes what `shortlisted = 1` means: an `excluded` or `expired`
        job keeps it, and its place in the tile.

        `run_ids` and `run_id` are mutually exclusive. `run_ids=[]` means "a day with no
        sweeps" and must read zero — not the whole install, which is what a truthiness
        test on the list would silently give.
        """
        if run_ids is not None and not run_ids:
            return {"seen": 0, "relevant": 0, "shortlisted": 0, "applied": 0}

        ids: tuple = tuple(run_ids or ())
        scored_filter = (
            " AND scored_run_id = ?" if run_id
            else f" AND {self._in_clause(ids, 'scored_run_id')}" if ids
            else ""
        )
        params: tuple = (run_id,) if run_id else ids
        # The tile counts postings this scope did WORK on, which is wider than the
        # verdict filter below. See `_new_work_sql`; the scope ids are passed twice
        # because the predicate names them twice, once per half.
        seen_sql = self._new_work_sql(run_id, ids)
        seen_params: tuple = params + params
        # `scored_at IS NOT NULL` is what keeps an unjudged posting — a title-gate
        # rejection, or one nothing has reached yet — out of both verdict figures. The
        # old queries got that from the existence of a `matches` row; here the row
        # always exists, so the test has to be explicit or every scraped posting would
        # count as relevant.
        relevant_sql = ("SELECT COUNT(*) AS n FROM postings "
                        f"WHERE scored_at IS NOT NULL AND {self._relevant_sql()}"
                        + scored_filter)
        shortlisted_sql = ("SELECT COUNT(*) AS n FROM postings "
                           "WHERE shortlisted = 1" + scored_filter)
        with self._lock:
            seen = self._conn.execute(seen_sql, seen_params).fetchone()
            relevant = self._conn.execute(relevant_sql, params).fetchone()
            shortlisted = self._conn.execute(shortlisted_sql, params).fetchone()
            # An application is a fact about a job, not about the sweep that surfaced
            # it — there is no run_id on it — so run scope means "applications to jobs
            # this sweep judged" rather than "applications made during it". That is the
            # same reading as before, now expressed against the posting's own columns.
            #
            # Submissions only. An `error` row is an attempt that stopped short — a
            # sign-in gate, a question nothing could answer — and counting it here made
            # the tile promise applications that never reached the employer. Those
            # rows are the page's Needs Attention section instead.
            applied = self._conn.execute(
                "SELECT COUNT(*) AS n FROM postings "
                "WHERE apply_status = 'submitted'" + scored_filter,
                params,
            ).fetchone()
        return {
            "seen": seen["n"] or 0,
            "relevant": relevant["n"] or 0,
            "shortlisted": shortlisted["n"] or 0,
            "applied": applied["n"] or 0,
        }

    # The columns every overview loader selects, so the records they return are the
    # same shape `load_all_matches` returns and the renderer cannot tell them apart.
    # `location` and `updated_at` used to come off a joined `jobs` row and were blank
    # whenever the match row's run had no sighting of the posting; on one row they
    # always resolve.
    _MATCH_COLUMNS = (
        "match_json, relevance_score, encoder_score, rerank_score_wide, "
        "rerank_score, yoe_required, skipped, skip_reason, shortlisted, "
        "scored_at, location, updated_at, apply_deferred_at"
    )

    #: "A verdict has been reached on this posting." The merge made this test necessary
    #: where the existence of a `matches` row used to imply it: a posting now always has
    #: a row, so every reader of the scoring columns has to say so explicitly or it
    #: would count the whole scrape as judged.
    _SCORED = "scored_at IS NOT NULL"

    @staticmethod
    def _match_record(row: sqlite3.Row) -> dict:
        record = json.loads(row["match_json"])
        record["location"] = row["location"] or record.get("location") or ""
        record["posted_at"] = row["updated_at"] or ""
        record["shortlisted"] = bool(row["shortlisted"])
        # Carried as a column rather than out of the blob: the blob is the judge's
        # verdict, written once, while this is set and cleared by the applier long
        # after. Selecting it without copying it here would drop it silently, which is
        # the one failure mode a sub-line cannot survive.
        record["apply_deferred_at"] = row["apply_deferred_at"] or ""
        return record

    def load_lifetime_matches(self, limit: int,
                              run_ids: Sequence[str] | None = None) -> list[dict]:
        """Every judged posting in a set of runs, best first.

        One row per posting, which is the table's own shape rather than something a
        `GROUP BY` has to produce. It used to be two calls, one for rows carrying a
        standing verdict and one for the rest, each deduping only *within* itself — so
        a job holding both kinds of row satisfied both queries and the page listed it
        twice, under contradicting labels; filed as issue R1, 381 jobs on a real
        install. Then one call over `_canonical_matches_sql`. Now neither is needed.

        The ranking that pair produced survives, because it is the one the page wants:
        judged jobs first by the verdict they got, then everything else by the
        cross-encoder logit that decided whether they were worth a call. `IS NULL`
        first keeps a job that never reached the reranker at the bottom rather than
        the top.

        `run_ids` narrows it to one day, by `scored_run_id` — the sweep whose verdict
        the row carries. `_SCORED` is what keeps the scrape out: without it every
        unjudged posting in the table would be listed with no scores at all.
        """
        if run_ids is not None and not run_ids:
            return []
        ids = tuple(run_ids or ())
        judged = self._judged_sql()
        rank = f"CASE WHEN {judged} THEN relevance_score ELSE rerank_score END"
        scope = f" AND {self._in_clause(ids, 'scored_run_id')}" if ids else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._MATCH_COLUMNS} FROM postings "
                f"WHERE {self._SCORED}" + scope +
                f" ORDER BY ({judged}) DESC, {rank} IS NULL, {rank} DESC LIMIT ?",
                (*ids, int(limit)),
            ).fetchall()
        return [self._match_record(r) for r in rows]

    def load_unmatched_jobs(self, run_id: str | None, limit: int,
                            run_ids: Sequence[str] | None = None) -> list[dict]:
        """Postings nothing ever scored, at any of the three scopes.

        These are the title-gate rejections — `title_excluded` and
        `title_low_relevance` — which `matcher.py` deliberately never scores, because
        there can be tens of thousands of them per run. They carry no score of any
        kind: nothing read their descriptions. What they do carry is `gate_reason`, the
        gate's own verdict, which `record_gate_reasons` writes onto the row the scraper
        had already made.

        **One row per posting, filed under the sweep that FIRST saw it.** That is
        `first_run_id`, a stored column. It replaced an anti-join
        (`NOT EXISTS (… e.run_id < j.run_id)`) that had to derive the same fact from
        7.7 rows per posting, plus the covering index without which one run-scope read
        measured **31.8 s** against 7 ms. The rule it enforced is unchanged and still
        load-bearing: without it the seventh sweep's page re-lists every posting the
        first sweep's title gate threw out, as though it had just found it — on a mature
        install, most of the section.

        **`match_json IS NULL` is the other half, and it is deliberately not scoped to
        a run.** The question is *has anything ever scored this posting* — a job an
        earlier sweep judged, which the matcher has since retired, must not be listed
        here with a blank score as though nothing had. Pairing an unscoped state test
        with a scoped sighting test used to look like a contradiction; with one row it
        is simply a column that is either filled or not.

        It is also deliberately **not** narrowed further at day scope: a posting a
        day's 9am sweep first saw appears exactly once on that day's page, because
        `first_run_id` names one sweep.

        The price, accepted twice over now, is that on second and later sweeps the five
        sections no longer sum to the `Jobs in scope` tile — that tile counts everything
        the scope did work on, which includes a job an earlier sweep first saw and this
        one re-judged after a deferral. Do not reconcile them.

        Note "first seen" means *the first sighting still on record*: `prune_runs`
        rewrites `first_run_id` to the oldest surviving sweep when it prunes the one a
        posting was filed under, so a survivor re-files onto whichever page then holds
        its earliest sighting. Self-correcting, and honest — but it is now a property of
        `prune_runs` rather than of a query, so the two have to be read together.

        **`gate_reason` is a bare column**, and the rule it used to need a
        `COALESCE(…, MAX(…))` across runs for — *any run that recorded a reason wins* —
        is now structural. The reason was not always on the earliest row: a sweep killed
        outright (`--stop` is `taskkill /F`, which runs no `finally`) left `jobs` rows
        the matcher never gated, so the *next* sweep gated the job and held the reason
        on a later row. Both sweeps now write the same row.

        Index-backed at every scope by `idx_postings_first_run`, with no table scan.
        That matters because the reports rebuild on a clock for the length of a sweep,
        not on funnel events.
        """
        if run_ids is not None and not run_ids:
            return []
        ids = tuple(run_ids or ())
        scope = (
            " AND first_run_id = ?" if run_id
            else f" AND {self._in_clause(ids, 'first_run_id')}" if ids
            else ""
        )
        params: tuple = (
            (run_id, int(limit)) if run_id else (*ids, int(limit))
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, board_token, title, location, url, gate_reason "
                "FROM postings WHERE match_json IS NULL"
                + scope +
                " LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "job_id": r["job_id"],
                "board_token": r["board_token"] or "",
                "title": r["title"] or "",
                "location": r["location"] or "",
                "absolute_url": r["url"] or "",
                "gate_reason": r["gate_reason"] or "",
            }
            for r in rows
        ]

    def load_applied_matches(self, run_id: str | None = None,
                             run_ids: Sequence[str] | None = None) -> list[dict]:
        """Every application, carrying the posting's verdict where it has one.

        **Two joins and a row-picking subquery went away here.** It used to join
        `applied` to the newest of a job's `matches` rows (`m.rowid = (SELECT rowid …
        ORDER BY scored_at DESC)`) and then to `jobs` for the location, because an
        application outlives the sweep that found it while `matches` was per-run. One
        row holds all three facts, so an application can no longer be rendered from a
        row the page has stopped believing elsewhere — there is no other row.

        A posting with an application but no verdict is still possible and still
        matters: `exclude_companies` writes its `excluded` record before anything
        scores the job. `match_json IS NULL` is what the fallback branch below reads,
        and those rows keep their title and company from the posting itself.

        Ordered by the LLM's verdict, best first, so the applied accordion ranks the
        same way every other list on the overview page does. An application with no
        verdict sorts last rather than first, and the timestamp breaks ties — nothing
        is lost by demoting it from the primary key, because `_job_entry` prints it in
        the meta line either way.

        The scope filter is `scored_run_id`, and an unjudged application is **kept at
        every scope**: it has no sweep to be filed under, and dropping it would hide
        exactly the rows Needs Attention exists for. An application is a fact about a
        job, not about a sweep — the same reading `overview_counts` applies.
        """
        if run_ids is not None and not run_ids:
            return []
        ids = tuple(run_ids or ())
        scope = (
            " AND (scored_run_id = ? OR scored_run_id IS NULL)" if run_id
            else (f" AND ({self._in_clause(ids, 'scored_run_id')}"
                  " OR scored_run_id IS NULL)") if ids
            else ""
        )
        params: tuple = (run_id,) if run_id else ids
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, board_token, url AS absolute_url, "
                "       apply_status, apply_error, applied_at, from_backlog, title, "
                f"       {self._MATCH_COLUMNS} "
                "FROM postings WHERE apply_status IS NOT NULL"
                + scope +
                " ORDER BY relevance_score IS NULL, relevance_score DESC,"
                " applied_at DESC",
                params,
            ).fetchall()

        out: list[dict] = []
        for r in rows:
            record = self._match_record(r) if r["match_json"] else {
                "job_id": r["job_id"],
                "board_token": r["board_token"],
                "title": r["title"],
                "absolute_url": r["absolute_url"],
                "location": "",
            }
            record["applied_at"] = r["applied_at"]
            record["applied_status"] = r["apply_status"]
            record["applied_error"] = r["apply_error"]
            record["applied_from_backlog"] = bool(r["from_backlog"])
            out.append(record)
        return out

    def calibration_rows(self, run_id: str | None = None) -> list[dict]:
        """(encoder_score, rerank_score, relevance_score) for every genuinely judged job.

        The input to `scripts/calibrate_cutoffs.py`: each row pairs the funnel scores
        that let a job through with the verdict that came back, which is what makes a
        cutoff derivable rather than guessable.

        `skipped = 0` is doing more work than it looks. It excludes cluster siblings,
        which are written skipped with their representative's score copied onto them —
        counting those would weight a single LLM verdict by however many locations the
        requisition was posted in, the exact distortion clustering exists to remove.
        It also excludes rows whose scoring call failed, whose relevance_score is a
        placeholder 0 rather than a judgement.
        """
        sql = (
            "SELECT encoder_score, rerank_score, relevance_score FROM postings "
            "WHERE relevance_score IS NOT NULL AND rerank_score IS NOT NULL "
            "AND (skipped = 0 OR skipped IS NULL)"
        )
        params: tuple = ()
        if run_id:
            sql += " AND scored_run_id = ?"
            params = (run_id,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def run_phase_stats(self, run_id: str) -> dict[str, dict]:
        """The `stats_json` blob for each phase of a run, keyed by phase.

        Only populated once a phase calls `finalise_run`, so a live page must treat
        every key as absent rather than zero.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT phase, started_at, finished_at, stats_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            try:
                stats = json.loads(r["stats_json"] or "{}")
            except json.JSONDecodeError:
                stats = {}
            stats["started_at"] = r["started_at"]
            stats["finished_at"] = r["finished_at"]
            out[r["phase"]] = stats
        return out

    # -- progress ------------------------------------------------------------
    #
    # Written from inside the sweep, once per company, batch and application, so a
    # failure here must never reach the caller: the bars are a diagnostic, and a
    # locked database is not worth a sweep. Only a misspelt column raises, because
    # that is a bug in this codebase rather than a condition at runtime.
    #
    # Every write is an UPDATE against a row `start_progress` created, so the phases
    # run standalone (`python scraper.py`) simply record nothing — only an
    # orchestrated sweep has an overview page to feed.

    _PROGRESS_COLUMNS = frozenset({
        "companies_total", "companies_done", "jobs_processed",
        "apply_enabled", "apply_queued", "apply_handled",
    })

    def _progress_write(self, run_id: str, cols: dict[str, int], add: bool) -> None:
        unknown = set(cols) - self._PROGRESS_COLUMNS
        if unknown:
            raise ValueError(f"not a run_progress column: {sorted(unknown)}")
        if not cols:
            return
        sets = ", ".join(f"{c} = {c} + ?" if add else f"{c} = ?" for c in cols)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    f"UPDATE run_progress SET {sets}, updated_at = ? WHERE run_id = ?",
                    (*(int(v) for v in cols.values()), now_iso(), run_id),
                )
        except sqlite3.Error as exc:
            logger.warning("Could not record progress for %s: %s", run_id, exc)

    def start_progress(self, run_id: str, apply_enabled: bool) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO run_progress(run_id, apply_enabled, updated_at) "
                    "VALUES (?, ?, ?)",
                    (run_id, int(bool(apply_enabled)), now_iso()),
                )
        except sqlite3.Error as exc:
            logger.warning("Could not start progress for %s: %s", run_id, exc)

    def set_progress(self, run_id: str, **cols: int) -> None:
        """Absolute values — the scraper's company total."""
        self._progress_write(run_id, cols, add=False)

    def bump_progress(self, run_id: str, **deltas: int) -> None:
        """Increments — one per company swept, batch matched, job applied."""
        self._progress_write(run_id, deltas, add=True)

    def run_progress(self, run_id: str) -> dict | None:
        """The counters plus the three figures derived from other tables, or None
        for a run made before progress was recorded.

        `jobs_in_scope` is the matcher bar's denominator and the same number as the
        page's `Jobs in scope` tile — the same `_new_work_sql` call, so the two cannot
        drift. It counts what this sweep had work to do on, which is what `matcher.py`
        bumps `jobs_processed` against; the two halves of this bar are defined against
        each other and must change together.
        `submitted` and `attention` split the applier bar; they count applications to
        this run's shortlisted *representatives*, because siblings are never sent to the
        worker and would hold the bar short.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_progress WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            jobs = self._conn.execute(
                self._new_work_sql(run_id, ()), (run_id, run_id)
            ).fetchone()
            applied = self._conn.execute(
                "SELECT SUM(CASE WHEN apply_status = 'submitted' THEN 1 ELSE 0 END) AS ok, "
                "       SUM(CASE WHEN apply_status != 'submitted' THEN 1 ELSE 0 END) AS bad "
                "FROM postings WHERE apply_status IS NOT NULL "
                "  AND scored_run_id = ? AND shortlisted = 1 "
                f"  AND NOT ({self._sibling_sql()})",
                (run_id,),
            ).fetchone()
        out = dict(row)
        out["jobs_in_scope"] = jobs["n"] or 0
        out["submitted"] = applied["ok"] or 0
        out["attention"] = applied["bad"] or 0
        return out

    def abandoned_runs(self) -> list[dict]:
        """Orchestrated sweeps that never wrote their pipeline `runs` row, oldest first.

        Only a sweep killed outright (`--stop`, a killed shell task, Task Manager)
        leaves one: the row is written from `run_pipeline`'s `finally`, which a
        forced kill never reaches. Its pages then read as live for good, because
        that row is their only signal the sweep is over. `run_progress` is written by
        orchestrated sweeps only, so a phase run standalone is never mistaken for one.

        The caller must know no sweep is running — the in-flight sweep matches too.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.run_id, p.updated_at FROM run_progress p "
                "WHERE NOT EXISTS (SELECT 1 FROM runs r "
                "  WHERE r.run_id = p.run_id AND r.phase = ?) "
                "ORDER BY p.run_id",
                (PHASE_PIPELINE,),
            ).fetchall()
        return [dict(r) for r in rows]

    def lifetime_progress(self, run_ids: Sequence[str] | None = None) -> dict:
        """The aggregate bars: two sums and one backlog. Lifetime, or one day.

        The scraper and matcher figures are sums over every tracked sweep, because
        nothing else can count them. `jobs_in_scope` is scoped to those same sweeps,
        so jobs from runs that predate progress tracking cannot hold the matcher bar
        short.

        Both `jobs` figures go through `_new_work_sql` rather than counting rows, so a
        posting several of a day's sweeps scraped is one posting. `jobs_processed` is a
        sum of per-sweep counters that now count the same thing (`matcher.py` bumps it
        with `unseen`), so the matcher bar's two halves still answer each other. At day
        scope `jobs_in_scope` and `unique_jobs` collapse to one number — their two scopes
        coincide there — and that collapse *is* the double count going away.

        The applier is different on purpose: every distinct shortlisted
        representative this install has ever had, against how many have an
        application. A sum of per-sweep counters reads ~100% whenever no sweep is
        running; the backlog keeps meaning something between sweeps, and it covers
        runs made before tracking began. Siblings are out for the reason they are out
        of `run_progress` — nothing ever applies to them.

        The shortlist half reads canonical rows, for the reason `overview_counts` does:
        this bar's total *is* the `Jobs shortlisted` tile, and the two must be the same
        number by construction rather than by coincidence.

        Groups whole tables at lifetime scope, so it belongs on the slower throttle —
        and the day page rides that same throttle, though for a different reason (see
        `reporting.refresh`).

        `run_ids` narrows every one of these reads to one day. Three of them are
        deliberately unfiltered at lifetime scope and each justification is a
        *lifetime* argument that does not transfer, so each takes the filter: the
        `unique_jobs` count (every run, tracked or not), the shortlist half (every
        representative the install has ever had), and `jobs_in_scope` (which must
        intersect the day's ids rather than take every tracked sweep). What does not
        change is the applier half's shape: it is still not a sum, because a sum of
        per-sweep counters reads ~100% whenever no sweep is running.
        """
        if run_ids is not None and not run_ids:
            return {
                "sweeps": 0, "companies_total": 0, "companies_done": 0,
                "jobs_processed": 0, "jobs_in_scope": 0, "unique_jobs": 0,
                "shortlisted": 0, "submitted": 0, "attention": 0,
            }
        ids = tuple(run_ids or ())
        progress_scope = f" WHERE {self._in_clause(ids, 'run_id')}" if ids else ""
        # Both `jobs` figures count new work rather than rows, for the reason
        # `_new_work_sql` gives. They keep their two different scopes: `jobs_in_scope`
        # over the tracked sweeps only, so jobs from runs predating progress tracking
        # cannot hold the matcher bar short, and `unique_jobs` over every run. At day
        # scope those two scopes coincide and the figures collapse to one number, which
        # is the per-sweep double count going away.
        # The tracked-sweeps scope is a named argument now. It used to be a
        # `.replace("WHERE 1=1", …)` on the rendered string, which was fragile in a way
        # that mattered: it also left the *verdict* half unscoped, so a posting judged
        # by any sweep at all counted. Both halves take the filter now, which is
        # coherent with `jobs_processed` being a sum over `run_progress` rows.
        in_scope_sql = (
            self._new_work_sql(None, ids) if ids
            else self._new_work_sql(
                None, (), run_id_set_sql="SELECT run_id FROM run_progress")
        )
        unique_sql = self._new_work_sql(None, ids)
        shortlist_scope = (
            f" AND {self._in_clause(ids, 'scored_run_id')}" if ids else ""
        )
        shortlist = (
            "SELECT job_id FROM postings "
            f"WHERE shortlisted = 1 AND NOT ({self._sibling_sql()})"
            + shortlist_scope
        )
        with self._lock:
            sums = self._conn.execute(
                "SELECT COUNT(*) AS sweeps, "
                "       COALESCE(SUM(companies_total), 0) AS companies_total, "
                "       COALESCE(SUM(companies_done), 0) AS companies_done, "
                "       COALESCE(SUM(jobs_processed), 0) AS jobs_processed "
                "FROM run_progress" + progress_scope,
                ids,
            ).fetchone()
            jobs = self._conn.execute(in_scope_sql, ids + ids).fetchone()
            # What the scraper bar prints. Each posting once however many sweeps found
            # it — the `Jobs in scope` rule, over whatever set of runs is in scope.
            unique = self._conn.execute(unique_sql, ids + ids).fetchone()
            shortlisted = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM ({shortlist})", ids
            ).fetchone()
            # Still not a sum of per-sweep counters: every distinct shortlisted
            # representative the install has ever had, against how many were applied
            # to. `job_id IN (…)` rather than a second predicate on the same row,
            # because the shortlist subquery carries its own scope.
            applied = self._conn.execute(
                "SELECT SUM(CASE WHEN apply_status = 'submitted' THEN 1 ELSE 0 END) AS ok, "
                "       SUM(CASE WHEN apply_status != 'submitted' THEN 1 ELSE 0 END) AS bad "
                "FROM postings WHERE apply_status IS NOT NULL "
                f"  AND job_id IN ({shortlist})",
                ids,
            ).fetchone()
        return {
            "sweeps": sums["sweeps"] or 0,
            "companies_total": sums["companies_total"],
            "companies_done": sums["companies_done"],
            "jobs_processed": sums["jobs_processed"],
            "jobs_in_scope": jobs["n"] or 0,
            "unique_jobs": unique["n"] or 0,
            "shortlisted": shortlisted["n"] or 0,
            "submitted": applied["ok"] or 0,
            "attention": applied["bad"] or 0,
        }

    def recent_runs(self, limit: int = 30) -> list[dict]:
        """Newest-first run index: run_id and its time span."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, MIN(started_at) AS started_at, MAX(finished_at) AS finished_at "
                "FROM runs GROUP BY run_id ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def known_run_ids(self) -> list[str]:
        """Every run id this database has heard of, newest first.

        The union is not belt-and-braces: `run_progress` holds one row per
        *orchestrated* sweep and nothing else, while `runs` holds a row per finished
        phase — so a sweep still in flight is only in the first, and a phase run
        standalone (`python scraper.py`) is only in the second. The day scope picks its
        runs out of this list, so a sweep missing from it would be missing from its own
        day's page.

        One row per sweep, so this is a small scan however long the user has been at
        it, and it rides the day page's throttle rather than the fast one.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM run_progress "
                "UNION SELECT run_id FROM runs "
                "ORDER BY run_id DESC"
            ).fetchall()
        return [r["run_id"] for r in rows]

    def pipeline_spans(self, run_ids: Sequence[str]) -> list[dict]:
        """`(started_at, finished_at)` for each named sweep's pipeline phase.

        Deliberately returns the raw strings and does no arithmetic. `runs.started_at`
        arrives from three producers in three shapes — `datetime.isoformat()` with an
        offset, `now_isoformat()` with fractional seconds, and an orphan's re-derived
        instant — and `julianday()` is particular about all three, so averaging in SQL
        would fail silently on a subset of rows. `reporting.data.day_summary` does the
        arithmetic with the same tolerant parse `render.duration` uses, so there is one
        parsing rule rather than two.

        A sweep still in flight has no row here at all: `_finalise_pipeline` writes it
        at the end. That is deliberate, and the caller must not read it as a zero.
        """
        ids = tuple(run_ids or ())
        if not ids:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, started_at, finished_at FROM runs "
                f"WHERE phase = ? AND {self._in_clause(ids, 'run_id')}",
                (PHASE_PIPELINE, *ids),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- scraper -------------------------------------------------------------

    def record_company(
        self,
        run_id: str,
        board_token: str,
        platform: str | None,
        status: str,
        job_count: int,
        fetch_time_s: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_companies"
                "(run_id, board_token, platform, status, job_count, fetch_time_s, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, board_token, platform, status, job_count, fetch_time_s, error),
            )

    def record_companies(self, run_id: str, companies: list[dict]) -> None:
        """Bulk-insert many `run_companies` rows in one transaction. No-op if empty.

        Each dict may carry: board_token (required), platform, status, job_count,
        fetch_time_s, error. Used by the JSON backfill to avoid one transaction
        per company across thousands of legacy runs.
        """
        rows = [
            (
                run_id,
                c["board_token"],
                c.get("platform"),
                c.get("status"),
                int(c.get("job_count", 0) or 0),
                c.get("fetch_time_s"),
                c.get("error"),
            )
            for c in companies
        ]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO run_companies"
                "(run_id, board_token, platform, status, job_count, fetch_time_s, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def insert_jobs(self, run_id: str, jobs: list[Job]) -> None:
        """Upsert one company's postings in a single transaction. No-op if empty.

        The scraper's writer, and the only thing that creates a `postings` row. Every
        other writer here is an `UPDATE` against a row this one made.

        Four things about the statement, each of which has cost a bug once already:

        1. **An upsert naming its own columns, never `INSERT OR REPLACE`.** `OR REPLACE`
           *deletes the row and inserts a new one*, so a column this writer does not
           name comes back as its default rather than being left alone. That cost the
           title gate's verdict when `jobs` had exactly one such column; this row now
           also carries `retired_at`, every score, `shortlisted`, `skip_reason`,
           `match_json` and all five apply columns — **twenty-odd columns owned by the
           matcher, the gates and the applier, which a re-sighting must not touch.**
           Each is absent from the column list *and* from the `DO UPDATE SET`, and
           those are two separate requirements.
        2. **`MIN`/`MAX` on the run ids, not "leave `first_run_id` alone".** The latter
           is idempotent but order-dependent: any writer inserting an older run after a
           newer one — a test, a standalone re-scrape — would record a later first
           sighting permanently. The two-argument scalars make the statement
           commutative, which is what makes it safe to call twice, and
           `matcher._persist_hydrated_details` does call it again mid-run on these same
           rows. Lexicographic over a fixed-width UTC stamp is chronological by
           construction, the same property `prune_runs` relies on.
        3. **`COALESCE` on `content_text`.** `_persist_hydrated_details` passes Workday
           and BambooHR rows whose hydration *failed*, carrying `content_text=None`; a
           plain `excluded.content_text` would let this sweep's failure erase a
           description an earlier sweep fetched — a loss the old per-run rows made
           structurally impossible. The composite key is what makes this safe without a
           board check: a conflict on `(board_token, job_id)` is the same posting by
           construction, so there is no other employer's text to keep by mistake.
        4. **Everything else is last-writer-wins**, which is right here: the only
           writers are the live sweep, whose run ids only go forward, and that same
           run's hydration pass.

        Accepted wart from (3): `job_json` still takes the new value, so a row can
        carry a description beside `detail_fetch_failed: true`. Harmless — only the
        funnel reads that flag, in memory — and cheaper than a second `CASE`.
        """
        if not jobs:
            return
        rows = []
        for job in jobs:
            # The description lives in its own `content_text` column; keep it (and
            # the never-read raw HTML) out of job_json to avoid storing it 2-3x.
            raw = job.model_dump(mode="json", exclude={"content_html", "content_text"})
            rows.append((
                job.board_token or "",
                job.job_id,
                job.source,
                job.title,
                job.location.name,
                str(job.absolute_url),
                job.updated_at.isoformat(),
                job.scraped_at.isoformat(),
                run_id,
                run_id,
                json.dumps(raw, default=str),
                job.content_text,
            ))
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO postings"
                "(board_token, job_id, source, title, location, url, "
                " updated_at, scraped_at, first_run_id, last_run_id, "
                " job_json, content_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(board_token, job_id) DO UPDATE SET "
                "  source = excluded.source, title = excluded.title, "
                "  location = excluded.location, url = excluded.url, "
                "  updated_at = excluded.updated_at, "
                "  scraped_at = excluded.scraped_at, "
                "  job_json = excluded.job_json, "
                "  content_text = COALESCE(excluded.content_text, "
                "                          postings.content_text), "
                "  first_run_id = MIN(postings.first_run_id, excluded.first_run_id), "
                "  last_run_id = MAX(postings.last_run_id, excluded.last_run_id)",
                rows,
            )

    def record_gate_reasons(self, run_id: str,
                            triples: list[tuple[str, str, str]]) -> None:
        """Why the free title gate dropped these postings. One statement, no-op if empty.

        The title gate rejects tens of thousands of postings a sweep, which
        `matcher.py` deliberately never scores — leaving its verdict recorded nowhere
        at all, and the overview's last section listing thousands of jobs with no way
        to say why any of them was there. This puts the reason on the row the scraper
        had already made: one UPDATE per rejected posting in a single batched
        statement, rather than rows in a table the reports group.

        **`insert_jobs` must never name this column**, and that is the whole reason
        this is a separate writer — see the first note on that method.

        **Blank reasons are skipped.** `matcher._record_gate_reasons` passes
        `r.skip_reason or ""`, and one row per posting means a later sweep writes the
        same row an earlier one did — so without this filter an empty string could
        overwrite a real `title_excluded`. This filter is now the whole of what a
        cross-run `COALESCE(…, MAX(gate_reason))` lookup used to buy: the rule *any
        sweep that recorded a reason wins* becomes "the last sweep to record a real one
        wins", and nothing can blank it.

        `run_id` is taken and deliberately unused: the verdict is a fact about the
        posting, not about the sweep. It stays in the signature because every other
        writer here takes one and the caller has it to hand.

        An `UPDATE` rather than an upsert because the row is guaranteed to exist: the
        scraper writes it (`storage/json_store.py`) before the batch ever reaches the
        matcher. A triple naming a row that is somehow gone updates nothing and is not
        an error — the reason is a label on a posting, not a fact the sweep depends on.
        """
        rows = [
            (reason, board_token or "", job_id)
            for board_token, job_id, reason in triples
            if reason
        ]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE postings SET gate_reason = ? "
                "WHERE board_token = ? AND job_id = ?",
                rows,
            )

    def load_jobs(self, run_id: str) -> list[Job]:
        """The postings whose **newest sighting** is this run.

        A narrower contract than this had, and the narrowing is forced rather than
        chosen: it used to mean "every posting this run scraped", and no surviving
        table records that — it is the fact the merge deletes. For any run that is not
        the newest, postings a later sweep re-scraped are missing, and for an old
        enough run the result is empty.

        Which is safe for the only production caller: `matcher.py`'s standalone mode
        (`python matcher.py`) reads `RunStore.latest_run`, the newest scrape, where
        `last_run_id` is that run for everything it scraped. An orchestrated sweep
        never comes here — the scraper hands it batches over a queue.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_text, job_json FROM postings WHERE last_run_id=?",
                (run_id,),
            ).fetchall()
        jobs: list[Job] = []
        for row in rows:
            try:
                jobs.append(self._row_to_job(row))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping malformed job row: %s", exc)
        return jobs

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        """Rebuild a Job, re-injecting the description from its column since
        job_json no longer carries content_text (or content_html)."""
        data = json.loads(row["job_json"])
        data["content_text"] = row["content_text"]
        return Job(**data)

    def get_jobs(self, job_ids: Iterable[str]) -> dict[str, Job]:
        """Postings by bare `job_id`. No `run_id` parameter any more.

        Dropping it is deliberate rather than tidying: with one row per posting a run
        filter here would have to mean `last_run_id`, which is a different question
        from the one every caller was asking, and a parameter that silently changes
        meaning is worse than one that is gone. (There is no production caller today
        in any case.)
        """
        ids = list(job_ids)
        if not ids:
            return {}
        out: dict[str, Job] = {}
        with self._lock:
            # chunk to stay under SQLite's variable limit
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT job_id, content_text, job_json FROM postings "
                    f"WHERE job_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    try:
                        out[row["job_id"]] = self._row_to_job(row)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Skipping malformed job row %s: %s", row["job_id"], exc)
        return out

    # -- matcher -------------------------------------------------------------

    def upsert_match(
        self,
        run_id: str,
        job_id: str,
        board_token: str,
        title: str,
        relevance_score: int,
        shortlisted: bool,
        skipped: bool,
        skip_reason: str | None,
        source_run_id: str,
        scored_at: str,
        raw_json: str,
        encoder_score: float | None = None,
        rerank_score_wide: float | None = None,
        rerank_score: float | None = None,
        yoe_required: float | None = None,
    ) -> int:
        """Write the funnel's scores and the judge's verdict onto the posting.

        **A named-column `UPDATE`, and it must never go back to `INSERT OR REPLACE`.**
        It was exactly that, against a `matches` table of its own, writing all fifteen
        columns it owned. Against the merged row that statement is destructive in a way
        no test would notice until a user's dashboard went blank: `OR REPLACE` deletes
        and reinserts, so `job_json`, `content_text`, `first_run_id`, `gate_reason`,
        `retired_at` and every apply column — the scrape and the application — would
        come back as defaults. Same hazard as `insert_jobs`, from the other side.

        It also must not insert. The scraper writes the row before the batch ever
        reaches the matcher, which is the same guarantee `record_gate_reasons` relies
        on, so a missed row means something is wrong upstream rather than that a row
        needs making. Returns the rowcount so a caller can notice.

        `title` is accepted and deliberately **not written**: the scraper owns it, and
        the judge has no better version. It stays in the signature because
        `MatchStore.append_result` has it and dropping it would churn that call site
        for nothing.

        `scored_at` is what every reader tests to mean "this posting has a verdict"
        (`_SCORED`), so it must never be written NULL.
        """
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE postings SET "
                "  scored_run_id = ?, source_run_id = ?, scored_at = ?, "
                "  relevance_score = ?, encoder_score = ?, rerank_score = ?, "
                "  rerank_score_wide = ?, yoe_required = ?, "
                "  shortlisted = ?, skipped = ?, skip_reason = ?, match_json = ? "
                "WHERE board_token = ? AND job_id = ?",
                (run_id, source_run_id, scored_at, relevance_score, encoder_score,
                 rerank_score, rerank_score_wide, yoe_required, int(shortlisted),
                 int(skipped), skip_reason, raw_json, board_token or "", job_id),
            ).rowcount

    def load_matches(self, run_id: str) -> list[dict]:
        """Every verdict this sweep reached, as the raw MatchResult dicts."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT match_json FROM postings WHERE scored_run_id=? "
                f"AND {self._SCORED}",
                (run_id,),
            ).fetchall()
        return [json.loads(r["match_json"]) for r in rows]

    def load_all_matches(self, run_id: str) -> list[dict]:
        """Every verdict this sweep reached, with the posting's location and post date.

        Backs the results CSV. The `LEFT JOIN jobs` this used to need is gone with the
        merge, and so is the reason it had to be a LEFT JOIN — a verdict could outlive
        the sighting it was joined to, and a partial export beat one that silently
        dropped rows. One row carries both.

        Ordered best-first: LLM score, then the cross-encoder logit, then the old
        wide-pass column. The two rerank columns are sorted in sequence rather than
        merged because on rows old enough to carry both they came from different
        models and were never comparable.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT match_json, relevance_score, encoder_score, "
                "       rerank_score_wide, rerank_score, yoe_required, "
                "       skipped, skip_reason, "
                "       shortlisted, scored_at, location, updated_at, "
                "       apply_deferred_at "
                "FROM postings "
                f"WHERE scored_run_id=? AND {self._SCORED} "
                "ORDER BY relevance_score IS NULL, relevance_score DESC, "
                "         rerank_score IS NULL, rerank_score DESC, "
                "         rerank_score_wide DESC",
                (run_id,),
            ).fetchall()

        out: list[dict] = []
        for r in rows:
            record = json.loads(r["match_json"])
            record["location"] = r["location"] or record.get("location") or ""
            record["posted_at"] = r["updated_at"] or ""
            record["shortlisted"] = bool(r["shortlisted"])
            # Same reason as `_match_record`, which this loader deliberately does not
            # share: a run page must show the retry line too, or the sub-line would
            # appear at lifetime and day scope and silently vanish at run scope.
            record["apply_deferred_at"] = r["apply_deferred_at"] or ""
            out.append(record)
        return out

    def load_shortlisted(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT match_json FROM postings "
                "WHERE scored_run_id=? AND shortlisted=1 "
                f"AND {self._SCORED} "
                "ORDER BY relevance_score DESC",
                (run_id,),
            ).fetchall()
        return [json.loads(r["match_json"]) for r in rows]

    def mark_not_shortlisted(self, job_id: str, reason: str,
                             location: str | None = None) -> int:
        """Retire a shortlisted job on a verdict reached after it was scored.

        The applier's location check is the only caller: the posting page states a
        location outside the user's list, which is a fact about the job and will be
        the same on every future sweep. Clearing `shortlisted` is what takes it out of
        `load_pending_applications`, which re-queued it every sweep for the whole
        `backlog_hours` window — one browser session each time, to re-read a location
        that cannot change.

        **`skipped` is deliberately left alone.** It is what `_judged_sql` and both
        copies of `_never_scored` read to decide whether a verdict stands behind
        `relevance_score`, and this job *was* judged — it has a real LLM score. Setting
        it would blank the score in the results CSV and file the row among the
        never-scored ones, the same misreading the CSV leaves `llm_score` empty to
        avoid.

        **Keyed on `job_id` alone, deliberately, and this is the one place the merge
        left a loose end worth naming.** The applier reaches a job from the backlog
        with its board token to hand, but the overview's decline button has only the
        bare id — so this stays id-keyed and updates every posting that matches. Two
        boards sharing an id is the one case where that touches a second posting;
        un-shortlisting it is a conservative outcome (it keeps its score and its place
        in Jobs Filtered), unlike marking it applied, which is why
        `mark_applied_by_hand` refuses an ambiguous id instead.

        It used to be id-keyed for a different reason that is now gone: `matches` was
        `(run_id, job_id)`, so a backlog job scored in an earlier sweep needed every
        one of its rows updated, or the lifetime page rendered it twice under
        contradicting labels.

        `AND shortlisted = 1` makes it idempotent and a no-op for a job already
        retired. Returns how many rows changed, so the caller can log a miss.

        Note this lowers the `Jobs shortlisted` tile and the applier bar's denominator
        (`overview_counts`, `lifetime_progress`), retroactively and on both scopes.
        That is correct — the job is no longer waiting to be applied to — and is not a
        discrepancy to reconcile.
        """
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT board_token, match_json FROM postings "
                "WHERE job_id = ? AND shortlisted = 1",
                (job_id,),
            ).fetchall()
            for row in rows:
                # The column and the blob both, because they are read from different
                # places: `_match_record` overrides `shortlisted` from the column but
                # takes `skip_reason` straight out of `match_json`. `applier_location`
                # is an extra key rather than a `MatchResult` field — the blob is
                # loaded as a plain dict, and nothing else needs to know about it.
                #
                # A shortlisted posting always has a verdict, so `match_json` is never
                # NULL here; guarded anyway, because this runs off a user's button.
                raw = json.loads(row["match_json"]) if row["match_json"] else {}
                raw["skip_reason"] = reason
                if location:
                    raw["applier_location"] = location
                self._conn.execute(
                    "UPDATE postings SET shortlisted = 0, skip_reason = ?, "
                    "match_json = ? WHERE board_token = ? AND job_id = ?",
                    (reason, json.dumps(raw), row["board_token"], job_id),
                )
        return len(rows)

    # -- seen ----------------------------------------------------------------
    #
    # `retired_at` on the posting replaced a `seen_jobs` table keyed on `job_id`
    # alone. The set the matcher holds in memory is therefore `(board_token, job_id)`
    # pairs now — see `hireshire/matcher/seen.py` — which is what stops one board's
    # verdict retiring another board's posting of the same id.
    #
    # A column rather than a derivation from `skip_reason`: retirement follows
    # `_RETRYABLE_SKIP_REASONS`, and that rule stays in `matcher.py` where the funnel
    # can read it, instead of being restated in SQL where the two could drift.

    def seen_ids(self) -> set[tuple[str, str]]:
        """Every retired posting, as `(board_token, job_id)` pairs."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT board_token, job_id FROM postings "
                "WHERE retired_at IS NOT NULL"
            ).fetchall()
        return {(r["board_token"], r["job_id"]) for r in rows}

    def mark_seen(self, keys: Iterable[tuple[str, str]]) -> None:
        """Retire these postings. `keys` are `(board_token, job_id)` pairs.

        `WHERE retired_at IS NULL` is what makes this the `INSERT OR IGNORE` it
        replaced: re-retiring an already-retired posting must not move its stamp,
        which is the install's record of when the funnel first finished with it.
        """
        retired_at = now_iso()
        rows = [(retired_at, board_token or "", job_id) for board_token, job_id in keys]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE postings SET retired_at = ? "
                "WHERE board_token = ? AND job_id = ? AND retired_at IS NULL",
                rows,
            )

    def forget_seen_scoring_errors(self, reasons: Iterable[str]) -> int:
        """Un-retire postings whose only recorded outcome was a scoring failure.

        A posting is retired once it has an outcome, so a broken backend used to
        retire everything it failed on — permanently, and invisibly, since fixing the
        backend could not bring them back. This releases exactly those: a skip for one
        of `reasons` standing as the posting's verdict. Returns how many were freed.

        **The `EXCEPT` went away with the merge rather than being dropped.** It read
        `… EXCEPT SELECT job_id FROM matches WHERE skipped = 0`, because `matches` held
        one row per sweep per job and a later successful score sat beside the failure;
        the posting had to stay retired on the strength of that other row. One row
        means the failure either is the current verdict or has already been overwritten
        by the success, so there is nothing to subtract.
        """
        reasons = list(reasons)
        if not reasons:
            return 0
        placeholders = ",".join("?" for _ in reasons)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE postings SET retired_at = NULL "
                "WHERE retired_at IS NOT NULL AND skipped = 1 "
                f"AND skip_reason IN ({placeholders})",
                reasons,
            )
            return cur.rowcount

    # -- pipeline ------------------------------------------------------------

    def load_pipeline_results(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, company, title, location, posted_at, job_url, "
                "relevance_score, encoder_score, rerank_score_wide, rerank_score, "
                "found_at "
                "FROM pipeline_results WHERE run_id=? ORDER BY found_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_pipeline_result(self, run_id: str, record: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pipeline_results"
                "(run_id, job_id, company, title, location, posted_at, job_url, "
                " relevance_score, encoder_score, rerank_score_wide, rerank_score, "
                " found_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, record.get("job_id"), record.get("company"), record.get("title"),
                 record.get("location"), record.get("posted_at"), record.get("job_url"),
                 record.get("relevance_score"), record.get("encoder_score"),
                 record.get("rerank_score_wide"), record.get("rerank_score"),
                 record.get("found_at")),
            )

    # -- applier -------------------------------------------------------------

    def applied_ids(self) -> set[tuple[str, str]]:
        """Every posting with an application record, as `(board_token, job_id)` pairs.

        Pairs rather than bare ids, for the reason `seen_ids` returns pairs: the id
        alone is not unique across boards, and this set is what stops a second
        application to a job already applied to. Its two callers — the apply worker's
        re-read before every launch, and the results CSV's `applied` column — both
        have the board token to hand.

        Every status counts, not just `submitted`: the question is "has this posting
        been attempted", and an attempt that stopped short must not be attempted again
        by the same sweep.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT board_token, job_id FROM postings "
                "WHERE apply_status IS NOT NULL"
            ).fetchall()
        return {(r["board_token"], r["job_id"]) for r in rows}

    def recent_submissions(self, since_iso: str,
                           company: str | None = None) -> dict[str, list[str]]:
        """`submitted` stamps since `since_iso`, keyed by lowercased, trimmed company.

        What the per-company cap counts (`hireshire/applier/limits.py`). `company`
        narrows it to one key, which is how the worker asks before each launch; the
        overview page asks for every company at once. Only `submitted` counts, which
        includes a job marked applied by hand — that writes plain `submitted` too.
        Install-wide on purpose: an employer does not care which sweep applied.
        """
        sql = ("SELECT LOWER(TRIM(board_token)) AS company, applied_at FROM postings "
               "WHERE apply_status = 'submitted' AND applied_at >= ?")
        args: tuple = (since_iso,)
        if company is not None:
            sql += " AND LOWER(TRIM(board_token)) = ?"
            args += (company.strip().lower(),)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["company"] or "", []).append(r["applied_at"])
        return out

    def load_applied(self) -> list[dict]:
        """Every application record, oldest first, in the old `applied` row shape.

        The column names are mapped back (`apply_status` -> `status`, `apply_error` ->
        `error`, `url` -> `absolute_url`) rather than exposed raw, because this is a
        row shape two callers already read and renaming its keys would be churn with
        no reader asking for it. The legacy `dry_run` column is gone: it was written
        as 0 and selected by nothing.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, board_token, title, url AS absolute_url, applied_at, "
                "       apply_status AS status, screenshot, apply_error AS error "
                "FROM postings WHERE apply_status IS NOT NULL ORDER BY applied_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_applied(
        self,
        job_id: str,
        board_token: str,
        title: str,
        absolute_url: str,
        applied_at: str,
        status: str,
        screenshot: str | None,
        error: str | None,
        *,
        from_backlog: bool = False,
    ) -> int:
        """Write the outcome of one application onto the posting.

        **A named-column `UPDATE`, never `INSERT OR REPLACE`.** It was that, against an
        `applied` table of its own; against the merged row it would delete the scrape
        and the verdict and put back defaults. The same hazard as `upsert_match`, and
        the reason both are listed on `insert_jobs`' first note.

        `title` and `absolute_url` are accepted and **not written**: the scraper owns
        both, and they were only ever on `applied` so that an application could outlive
        the `matches` rows it pointed at. One row makes that impossible, which is the
        same reasoning that kept `mark_applied_by_hand` off this writer. They stay in
        the signature because all three call sites in `applier/worker.py` pass them.

        `from_backlog` says the job came off an earlier sweep's shortlist rather than
        the sweep that found it. It is **stored** because it cannot be derived later:
        there is no `run_id` here, so nothing downstream can tell which sweep did the
        applying, and `applied_at` against `scored_at` is a guess rather than a fact.
        Keyword-only with a default, so the writers that have no opinion — the
        `excluded` row and the expiry pass — stay as they were.

        Returns the rowcount, so a caller can notice a posting that is somehow gone.
        """
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE postings SET apply_status = ?, applied_at = ?, "
                "  apply_error = ?, screenshot = ?, from_backlog = ?, "
                # A verdict supersedes any deferral before it, so the overview's
                # "being retried" line cannot outlive the retrying.
                "  apply_deferred_at = NULL "
                "WHERE board_token = ? AND job_id = ?",
                (status, applied_at, error, screenshot, int(from_backlog),
                 board_token or "", job_id),
            ).rowcount

    def record_apply_deferral(self, job_id: str, board_token: str,
                              when_iso: str) -> int:
        """Note that a session gave up on this job without reaching a verdict.

        An `UPDATE` that touches **one** column and never inserts, like every other
        verdict writer here. It deliberately does **not** set `apply_status`: a
        deferral is not an application, so the job must stay visible to both backlog
        loaders (`_unapplied` keys on `apply_status IS NULL`) and keep being retried
        exactly as before. The column exists only so the overview can say a shortlisted
        job is mid-retry rather than merely queued.

        A timestamp and not a count, for the reason the DDL gives: a count here could
        be used to retire a job after N failures, which the deferral rule forbids.

        Returns the rowcount, which is 0 for a `job_id` no board mints — the same
        signal `record_applied` gives, and the same tolerance: a lost line on a page is
        not worth failing a sweep over.
        """
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE postings SET apply_deferred_at = ? "
                "WHERE board_token = ? AND job_id = ?",
                (when_iso, board_token or "", job_id),
            ).rowcount

    # -- outcomes the user records by hand -----------------------------------
    #
    # The applier reaches a verdict for most jobs, but three of its verdicts hand the
    # job back: `error` (a session stopped short), `excluded` (the portal needs an
    # account login) and `EXPIRED_STATUS` (the backlog's window closed). All three mean
    # "do this one yourself", and until these two methods existed there was no way to
    # say that it had been done — the row sat under Needs Attention for good, and a
    # shortlisted job applied to by hand was applied to again by the next sweep.
    #
    # The two outcomes are deliberately asymmetric, and the asymmetry is the whole
    # design: an application is a row in `applied`, and a decision not to apply is not.
    # See `DECLINED_BY_USER`.

    def mark_applied_by_hand(self, job_id: str, applied_at: str) -> str:
        """Record that the user applied to this job themselves. Idempotent.

        Returns `"updated"` when a posting was marked, `"unknown"` when nothing on
        record names this job — the only honest answer to a job_id the database has
        never seen, and why this does not blindly insert — or `"ambiguous"`, below.

        **One `UPDATE`, where there used to be three sources for the job's identity.**
        It tried an existing `applied` row, then the canonical `matches` row, then
        `jobs`; the last was not a nicety, because a title-gate rejection has neither
        of the first two and is exactly the job a user is likeliest to have applied to
        behind the funnel's back. One row per posting means one lookup, and the
        `"inserted"` return value collapses into `"updated"` — there is nothing left
        to insert.

        **`"ambiguous"` is new, and it is the one place the composite key shows through
        to a user.** The page's buttons and `scripts/jobs_cli.py` pass a bare `job_id`,
        which can name two postings when two boards mint the same id. Marking both
        applied would be a lie about one of them, so this refuses and says so. The
        alternative — widening the command to carry a board token — would ripple into
        `scripts/approve.py`, which matches the exact command string, the
        `mark-applied` skill and the button's copied command, for a case that is
        vanishingly rare. (`mark_not_shortlisted` makes the opposite trade, for the
        opposite reason: un-shortlisting the wrong posting is recoverable.)

        `apply_error` is cleared because it is the reason the job needed attention and
        that reason is now discharged; `screenshot` is kept, because a partial capture
        of the form is still the user's own record of the attempt.

        The status written is plain `submitted`, which makes a hand-marked application
        indistinguishable from an automatic one on the page. That is accepted rather
        than overlooked: a second "counts as applied" status would have to be added to
        every site that tests the literal — `overview_counts`, `run_progress`,
        `lifetime_progress`, `data.overview_snapshot` — and each omission would be a
        silent undercount. Provenance, if it is ever wanted, belongs in a new column.
        """
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT board_token FROM postings WHERE job_id = ?", (job_id,)
            ).fetchall()
            if not rows:
                return "unknown"
            if len(rows) > 1:
                return "ambiguous"
            self._conn.execute(
                "UPDATE postings SET apply_status = 'submitted', applied_at = ?, "
                "  apply_error = NULL "
                "WHERE board_token = ? AND job_id = ?",
                (applied_at, rows[0]["board_token"], job_id),
            )
        return "updated"

    def decline_job(self, job_id: str) -> dict:
        """Record that the user is not pursuing this job. Idempotent.

        Returns `{"deleted": bool, "unshortlisted": int}` — what actually changed, so
        the caller can tell a real decision from a repeat.

        Two writes, and both are needed:

        * **Clear the application record.** A failed attempt is what puts the job under
          Needs Attention, and `applied_ids` is what keeps it out of every other
          section. Giving it a new status instead would not work: any status that is
          not `submitted` renders under Needs Attention by design. So `apply_status`
          goes back to NULL, which is exactly what deleting the `applied` row used to
          mean — and is why a NULL there has to keep meaning "no record at all".
        * **Un-shortlist it** with `DECLINED_BY_USER`, which is what stops
          `load_pending_applications` handing it to the applier again and what files it
          under Jobs Filtered with a reason the user can read. `mark_not_shortlisted`
          leaves `skipped` at 0, so the LLM score it earned survives.

        The two run in sequence and **must not be nested**: `mark_not_shortlisted` takes
        `self._lock` itself and the lock is a plain `threading.Lock`, so holding it
        across that call would deadlock. Nothing depends on the pair being atomic — each
        write is independently idempotent, and a crash between them leaves a job that is
        un-shortlisted but still attention-listed, which the next call finishes.

        `mark_not_shortlisted` matches only `shortlisted = 1`, so it returns 0 for a job
        already retired. That is reported rather than treated as a failure: clearing the
        application record is on its own enough to clear Needs Attention.

        Keyed on the bare `job_id`, like `mark_not_shortlisted` and for the same
        reason — the button has nothing else — and conservative in the same way: the
        worst an ambiguous id can do is retire a posting the user did not mean, which
        keeps its score and its place in Jobs Filtered.
        """
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "UPDATE postings SET apply_status = NULL, applied_at = NULL, "
                "  apply_error = NULL, screenshot = NULL, from_backlog = 0, "
                "  apply_deferred_at = NULL "
                "WHERE job_id = ? AND apply_status IS NOT NULL",
                (job_id,),
            ).rowcount
        unshortlisted = self.mark_not_shortlisted(job_id, DECLINED_BY_USER)
        return {"deleted": bool(deleted), "unshortlisted": unshortlisted}

    def _unapplied(self, having: str, stamp_iso: str) -> list[dict]:
        """Shortlisted representatives with no `applied` row, split on their age.

        The body of `load_pending_applications` and `load_expired_applications`, which
        are the two halves of one set and must partition it exactly: a job the backlog
        can no longer see is a job the applier will never be handed again, and that is
        the whole basis for retiring it.

        **The age test is a plain `WHERE` on the row now, and that is only safe because
        the row is unique.** It had to be `HAVING MAX(scored_at)` over a group:
        `matches` was keyed `(run_id, job_id)`, so a job scored in one sweep and
        rescored in a later one had two rows, and a row-level `scored_at <` would match
        the stale one and report a job as expired while its fresh row still sat in the
        backlog — the applier retiring a job it was actively retrying. One row per
        posting carries the newest verdict by construction, so the two predicates are
        complements again, which is what the two loaders need: a job the backlog can no
        longer see is a job the applier will never be handed again, and that is the
        whole basis for retiring it.

        `job_url` comes off the posting's own `url` column rather than out of the JSON
        blob, which is the one behaviour change here: the blob's `absolute_url` was the
        only copy `matches` had, and the scraper's column is the better source.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT board_token, job_id, title, relevance_score, url, scored_at "
                "FROM postings "
                "WHERE shortlisted = 1 "
                f"AND NOT ({self._sibling_sql()}) "
                "AND apply_status IS NULL "
                f"AND scored_at {having} ? "
                "ORDER BY relevance_score DESC",
                (stamp_iso,),
            ).fetchall()
        return [
            {
                "job_id": r["job_id"],
                "company": r["board_token"],
                "title": r["title"],
                "job_url": r["url"] or "",
                "relevance_score": r["relevance_score"],
                "scored_at": r["scored_at"],
            }
            for r in rows
        ]

    def load_pending_applications(self, since_iso: str) -> list[dict]:
        """Shortlisted jobs scored since `since_iso` with no `applied` row, best first.

        The apply worker's backlog. It exists because the matcher retires a job once it is judged:
        a job whose apply session failed to launch is never streamed again, so this
        is the only road back to it.

        Cluster siblings are excluded — only the representative is ever applied to,
        the same rule the stream follows. One row per job, from its newest match.
        Rows come back in the pipeline-record shape the stream uses (`company`,
        `job_url`), so the worker treats both sources identically.
        """
        return self._unapplied(">=", since_iso)

    def load_expired_applications(self, before_iso: str) -> list[dict]:
        """The backlog's other end: shortlisted jobs it can no longer reach.

        Same set, same shape, opposite side of `before_iso` — jobs last scored before
        the window opened, still shortlisted, still never applied to. Nothing will
        stream them again (the matcher retires a judged job) and the backlog has
        stopped looking at them, so without a record they sit under Jobs Shortlisted
        for good, reading as work the applier still has to do.

        `worker.run_apply_worker` is the only caller: it writes each one a terminal
        `applied` row, which is what takes it out of this set permanently.
        """
        return self._unapplied("<", before_iso)

    # -- retention (manual, via `scripts/jobs_cli.py prune`) -----------------

    def all_run_ids(self) -> list[str]:
        """Distinct run_ids ordered newest-first by their earliest start time."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, MAX(started_at) AS ts FROM runs "
                "GROUP BY run_id ORDER BY ts DESC"
            ).fetchall()
        return [r["run_id"] for r in rows]

    #: SQLite's default variable limit is 999, so an id list goes in in chunks. A
    #: `prune_runs(before=…)` on a long-lived install can name hundreds of sweeps.
    _PRUNE_CHUNK = 900

    def prune_runs(self, keep: int | None = None, before: str | None = None) -> list[str]:
        """Forget old sweeps, and the postings nothing came of. Returns the run ids.

        `keep` retains the N most-recent runs; `before` deletes runs whose run_id (an
        ISO-timestamp string, so lexicographic order is chronological) sorts before the
        given date.

        **The posting half is the dangerous part of this method, and the predicate on
        it is not optional.** `jobs`, `matches`, `applied` and `seen_jobs` all used to
        be in the table list below, and `applied` was explicitly excluded because an
        application is a fact about a job rather than about a sweep — a user who prunes
        old runs should still see what they applied to. The merge makes the posting
        *be* the application record, screenshot path and all, so a bare
        `DELETE … WHERE last_run_id IN (…)` would destroy exactly what the old code
        went out of its way to keep. Hence `apply_status IS NULL AND shortlisted = 0`:
        retention means "forget the postings nothing came of", and an applied or
        still-shortlisted posting is never prunable by age.

        **Keyed on `last_run_id`, not `first_run_id`.** A posting first seen by a
        pruned sweep is still live if a kept sweep found it again, and deleting it
        would empty the kept sweep's own pages.

        The `UPDATE` after it is what keeps `load_unmatched_jobs`' documented caveat
        true: "first seen" means the first sighting *still on record*, so a survivor
        filed under a sweep that is going away re-files onto its oldest surviving one.
        Without it, such a posting would appear on no run or day page at all and only
        at lifetime scope — a third behaviour nobody has reasoned about. It runs after
        the delete so it touches fewer rows.

        Note what this deliberately does **not** do: express the rule as
        `NOT EXISTS (SELECT 1 FROM runs …)`. An abandoned sweep never writes its
        pipeline `runs` row — that is what `abandoned_runs` exists for — so that form
        would delete live postings.
        """
        run_ids = self.all_run_ids()
        to_delete: list[str] = []
        if keep is not None:
            to_delete.extend(run_ids[keep:])
        if before is not None:
            to_delete.extend(r for r in run_ids if r < before)
        to_delete = sorted(set(to_delete))
        if not to_delete:
            return []
        tables = ("runs", "run_companies", "pipeline_results", "run_progress")
        with self._lock, self._conn:
            for rid in to_delete:
                for table in tables:
                    self._conn.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
            for i in range(0, len(to_delete), self._PRUNE_CHUNK):
                chunk = to_delete[i:i + self._PRUNE_CHUNK]
                holes = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"DELETE FROM postings WHERE last_run_id IN ({holes}) "
                    "AND apply_status IS NULL AND shortlisted = 0",
                    tuple(chunk),
                )
                self._conn.execute(
                    "UPDATE postings SET first_run_id = last_run_id "
                    f"WHERE first_run_id IN ({holes})",
                    tuple(chunk),
                )
        return to_delete


# ---------------------------------------------------------------------------
# Process-wide connection cache — one Database per resolved path so every phase
# in a single process shares one connection (writes serialize on one lock).
# ---------------------------------------------------------------------------

_INSTANCES: dict[str, Database] = {}
_INSTANCES_LOCK = threading.Lock()


def get_db(path: str | Path = DEFAULT_DB_PATH) -> Database:
    # Resolve the same way Database.__init__ does, so "hireshire.db" and the
    # absolute form share one cache entry (and therefore one connection).
    key = str(paths.resolve_data(path).resolve())
    with _INSTANCES_LOCK:
        db = _INSTANCES.get(key)
        if db is None:
            db = Database(path)
            _INSTANCES[key] = db
        return db
