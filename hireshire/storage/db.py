"""Central SQLite storage for every HireShire phase.

One `data/hireshire.db` (WAL mode) holds all tabular data: scraped jobs, matcher
results, the cross-run seen-jobs set, pipeline results, tuned-job metadata, and
applier records. Genuine binary artifacts (tuned PDFs/tex, applier screenshots)
stay on disk and are referenced by path from the DB.

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
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from hireshire import paths
from hireshire.models.job import Job

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = paths.DB_PATH
SCHEMA_VERSION = 1

# Phase identifiers used in the `runs` table.
PHASE_SCRAPE = "scrape"
PHASE_MATCH = "match"
PHASE_TUNE = "tune"
PHASE_PIPELINE = "pipeline"

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

CREATE TABLE IF NOT EXISTS jobs (
    run_id       TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    board_token  TEXT,
    source       TEXT,
    title        TEXT,
    location     TEXT,
    url          TEXT,
    updated_at   TEXT,
    scraped_at   TEXT,
    content_text TEXT,
    raw_json     TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);

CREATE TABLE IF NOT EXISTS matches (
    run_id          TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    board_token     TEXT,
    title           TEXT,
    relevance_score INTEGER,
    -- Three funnel scores on three different scales; see MatchResult for why they
    -- are never combined. encoder_score is a 0-1 cosine over the title;
    -- rerank_score_wide and rerank_score are logits from two DIFFERENT models.
    encoder_score     REAL,
    rerank_score_wide REAL,
    rerank_score      REAL,
    shortlisted     INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,
    skip_reason     TEXT,
    source_run_id   TEXT,
    scored_at       TEXT,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_run ON matches(run_id);
CREATE INDEX IF NOT EXISTS idx_matches_shortlisted ON matches(run_id, shortlisted);

CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id     TEXT PRIMARY KEY,
    first_seen TEXT
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

CREATE TABLE IF NOT EXISTS applied (
    job_id       TEXT PRIMARY KEY,
    board_token  TEXT,
    title        TEXT,
    absolute_url TEXT,
    applied_at   TEXT,
    status       TEXT,
    dry_run      INTEGER,
    screenshot   TEXT,
    error        TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # Columns added to existing tables after the first release. `CREATE TABLE IF NOT
    # EXISTS` is a no-op on a database that already has the table, so a new column in
    # _SCHEMA above never reaches an existing file — the INSERT then fails with an
    # opaque "no such column". Listed here, they are added on connect instead.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("matches", "encoder_score", "REAL"),
        ("matches", "rerank_score_wide", "REAL"),
        # `matches` never had this one: the rerank score used to live only in
        # pipeline_results and inside raw_json, so it is new here too.
        ("matches", "rerank_score", "REAL"),
        ("pipeline_results", "encoder_score", "REAL"),
        ("pipeline_results", "rerank_score_wide", "REAL"),
    )

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

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
        """Batch-insert one company's jobs in a single transaction. No-op if empty."""
        if not jobs:
            return
        rows = []
        for job in jobs:
            # The description lives in its own `content_text` column; keep it (and
            # the never-read raw HTML) out of raw_json to avoid storing it 2-3x.
            raw = job.model_dump(mode="json", exclude={"content_html", "content_text"})
            rows.append((
                run_id,
                job.job_id,
                job.board_token,
                job.source,
                job.title,
                job.location.name,
                str(job.absolute_url),
                job.updated_at.isoformat(),
                job.scraped_at.isoformat(),
                job.content_text,
                json.dumps(raw, default=str),
            ))
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO jobs"
                "(run_id, job_id, board_token, source, title, location, url, "
                " updated_at, scraped_at, content_text, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def load_jobs(self, run_id: str) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_text, raw_json FROM jobs WHERE run_id=?", (run_id,)
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
        raw_json no longer carries content_text (or content_html)."""
        data = json.loads(row["raw_json"])
        data["content_text"] = row["content_text"]
        return Job(**data)

    def get_jobs(self, run_id: str, job_ids: Iterable[str]) -> dict[str, Job]:
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
                    f"SELECT job_id, content_text, raw_json FROM jobs "
                    f"WHERE run_id=? AND job_id IN ({placeholders})",
                    (run_id, *chunk),
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
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO matches"
                "(run_id, job_id, board_token, title, relevance_score, encoder_score, "
                " rerank_score_wide, rerank_score, shortlisted, "
                " skipped, skip_reason, source_run_id, scored_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, job_id, board_token, title, relevance_score, encoder_score,
                 rerank_score_wide, rerank_score, int(shortlisted),
                 int(skipped), skip_reason, source_run_id, scored_at, raw_json),
            )

    def load_matches(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT raw_json FROM matches WHERE run_id=?", (run_id,)
            ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    def load_all_matches(self, run_id: str) -> list[dict]:
        """Every match row for a run, enriched with the job's location and post date.

        Backs the all-jobs export. LEFT JOIN because a match row must survive even if
        its jobs row is missing — a partial export beats an export that silently
        drops rows.

        Ordered best-first by the same rule the budget uses: LLM score, then the
        refined rerank logit, then the wide one. The two rerank columns are sorted
        in sequence rather than merged because they come from different models.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.raw_json, m.relevance_score, m.encoder_score, "
                "       m.rerank_score_wide, m.rerank_score, m.skipped, m.skip_reason, "
                "       m.shortlisted, m.scored_at, j.location, j.updated_at "
                "FROM matches m LEFT JOIN jobs j "
                "  ON j.run_id = m.run_id AND j.job_id = m.job_id "
                "WHERE m.run_id=? "
                "ORDER BY m.relevance_score IS NULL, m.relevance_score DESC, "
                "         m.rerank_score IS NULL, m.rerank_score DESC, "
                "         m.rerank_score_wide DESC",
                (run_id,),
            ).fetchall()

        out: list[dict] = []
        for r in rows:
            record = json.loads(r["raw_json"])
            record["location"] = r["location"] or record.get("location") or ""
            record["posted_at"] = r["updated_at"] or ""
            record["shortlisted"] = bool(r["shortlisted"])
            out.append(record)
        return out

    def load_shortlisted(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT raw_json FROM matches WHERE run_id=? AND shortlisted=1 "
                "ORDER BY relevance_score DESC",
                (run_id,),
            ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    # -- seen ----------------------------------------------------------------

    def seen_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT job_id FROM seen_jobs").fetchall()
        return {r["job_id"] for r in rows}

    def mark_seen(self, job_ids: Iterable[str]) -> None:
        first_seen = now_iso()
        rows = [(jid, first_seen) for jid in job_ids]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_jobs(job_id, first_seen) VALUES (?, ?)", rows
            )

    def forget_seen_scoring_errors(self, reasons: Iterable[str]) -> int:
        """Un-retire jobs whose only recorded outcome was a scoring failure.

        A job is retired into `seen_jobs` once it has an outcome, so a broken backend
        used to retire everything it failed on — permanently, and invisibly, since
        fixing the backend could not bring them back. This releases exactly those
        jobs: ones with a skip row for one of `reasons` and no successful score in
        any run. Returns how many were freed.
        """
        reasons = list(reasons)
        if not reasons:
            return 0
        placeholders = ",".join("?" for _ in reasons)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM seen_jobs WHERE job_id IN ("
                "  SELECT job_id FROM matches"
                f"  WHERE skipped = 1 AND skip_reason IN ({placeholders})"
                "  EXCEPT"
                "  SELECT job_id FROM matches WHERE skipped = 0"
                ")",
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

    def applied_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT job_id FROM applied").fetchall()
        return {r["job_id"] for r in rows}

    def load_applied(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, board_token, title, absolute_url, applied_at, status, "
                "dry_run, screenshot, error FROM applied ORDER BY applied_at"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dry_run"] = bool(d["dry_run"])
            out.append(d)
        return out

    def record_applied(
        self,
        job_id: str,
        board_token: str,
        title: str,
        absolute_url: str,
        applied_at: str,
        status: str,
        dry_run: bool,
        screenshot: str | None,
        error: str | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO applied"
                "(job_id, board_token, title, absolute_url, applied_at, status, "
                " dry_run, screenshot, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, board_token, title, absolute_url, applied_at, status,
                 int(dry_run), screenshot, error),
            )

    # -- retention (manual, via scripts/prune_runs.py) -----------------------

    def all_run_ids(self) -> list[str]:
        """Distinct run_ids ordered newest-first by their earliest start time."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, MAX(started_at) AS ts FROM runs "
                "GROUP BY run_id ORDER BY ts DESC"
            ).fetchall()
        return [r["run_id"] for r in rows]

    def prune_runs(self, keep: int | None = None, before: str | None = None) -> list[str]:
        """Delete run-scoped rows for old runs. Returns the deleted run_ids.

        `keep` retains the N most-recent runs; `before` deletes runs whose
        run_id (ISO-timestamp string) sorts before the given date. Cross-run
        tables (seen_jobs, applied) are never touched.
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
        tables = ("runs", "run_companies", "jobs", "matches", "pipeline_results")
        with self._lock, self._conn:
            for rid in to_delete:
                for table in tables:
                    self._conn.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
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
