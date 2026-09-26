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
    -- Funnel scores on different scales; see MatchResult for why they are never
    -- combined. encoder_score is a 0-1 cosine over the title; rerank_score is a
    -- cross-encoder logit. rerank_score_wide is written only by runs made under the
    -- old two-model cascade, where it came from a DIFFERENT model and was never
    -- comparable to rerank_score. Kept so those rows still render.
    encoder_score     REAL,
    rerank_score_wide REAL,
    rerank_score      REAL,
    -- Years of experience the POSTING asks for, read from its description by
    -- funnel/experience.py. Not the LLM's own reading of the same question, which
    -- stays in raw_json as years_experience_required.
    yoe_required      REAL,
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
-- The overview page groups by job_id across every run, and joins `applied` to it.
-- Unlike a new column, an index in this script does reach an existing database:
-- `executescript` runs on every connect and CREATE INDEX IF NOT EXISTS is not a
-- no-op the way CREATE TABLE IF NOT EXISTS is on a table that already exists.
CREATE INDEX IF NOT EXISTS idx_matches_job ON matches(job_id);

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
        """
        with self._lock:
            companies = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "       SUM(CASE WHEN job_count > 0 THEN 1 ELSE 0 END) AS with_jobs, "
                "       SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS errors "
                "FROM run_companies WHERE run_id=?",
                (run_id,),
            ).fetchone()
            jobs = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE run_id=?", (run_id,)
            ).fetchone()
        return {
            "companies": companies["n"] or 0,
            "companies_with_jobs": companies["with_jobs"] or 0,
            "errors": companies["errors"] or 0,
            "jobs": jobs["n"] or 0,
        }

    def match_counts(self, run_id: str) -> dict[str, int]:
        """The funnel's lower half, grouped by what happened to each job.

        `by_reason` is keyed by `skip_reason` with the empty string standing in for
        NULL, so callers can tell "scored" from "dropped for reason X" without a
        second query. Note that title-gate rejections are deliberately absent —
        they are never written per-row (there can be tens of thousands), so the
        top of the funnel comes from `scrape_counts` instead.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(skip_reason, '') AS reason, COUNT(*) AS n "
                "FROM matches WHERE run_id=? GROUP BY reason",
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
                "FROM matches WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "rows_total": totals["rows_total"] or 0,
            "scored": totals["scored"] or 0,
            "shortlisted": totals["shortlisted"] or 0,
            "top_score": totals["top_score"],
            "by_reason": {r["reason"]: r["n"] for r in rows},
        }

    def _sibling_sql(self, alias: str = "") -> str:
        """SQL for "this row inherited its verdict from a cluster representative".

        There is no column for it: `cluster_representative` lives inside `raw_json`.
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

        `alias` is not optional decoration either: `jobs` has a `raw_json` column
        too, so this predicate is ambiguous in any query that joins the two.
        """
        col = f"{alias}.raw_json" if alias else "raw_json"
        if self._has_json1:
            return f"json_extract({col}, '$.cluster_representative') IS NOT NULL"
        return f"{col} LIKE '%\"cluster_representative\":\"%'"

    def _relevant_sql(self, alias: str = "") -> str:
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
            f"AND NOT ({self._sibling_sql(alias)})"
        )

    def _judged_sql(self, alias: str = "") -> str:
        """A SQL mirror of `hireshire.reporting.data._never_scored`, negated.

        "A standing LLM verdict backs this row's relevance_score" — either the row
        was scored directly, or it is a cluster sibling and was scored by proxy. The
        two implementations must agree; `tests/test_overview.py` pins them together
        against a fixture that includes a sibling with an inherited drop reason.
        """
        return (
            "relevance_score IS NOT NULL AND "
            f"(skipped = 0 OR skipped IS NULL OR {self._sibling_sql(alias)})"
        )

    def _canonical_matches_sql(self, cols: str, joins: str = "") -> str:
        """One row per `job_id` across every run: the job's **newest** match row.

        `matches` is keyed `(run_id, job_id)`, so a job dropped on a deferral — the
        call cap, a scoring failure — and judged in a later sweep keeps *both* rows.
        Every lifetime read has to choose one, and the newest is the one still true:
        the matcher retires a judged job, so a verdict is always the last word, and
        only a later sweep can supersede a deferral. Counting "any row that ever said
        so" instead listed 381 jobs twice on the lifetime page and left 8 more in the
        `Relevant jobs` tile on a reading the cross-encoder had since overturned.

        **Newest, not "judged first".** The obvious refinement — prefer a row with a
        standing verdict — is wrong, because `_judged_sql` is also true of a cluster
        sibling whose representative *failed*: that row carries a placeholder 0 and no
        verdict behind it, and preferring it suppresses a genuine `rerank_below_cutoff`
        written weeks later. Measured on a real database, no job's newest row loses a
        real verdict, and both rules produce identical tiles.

        `MAX(m.scored_at)` over bare columns is the same trick `_unapplied` uses on the
        backlog window, for the same reason: SQLite fills the other columns from the
        row that produced the maximum, so the whole row comes back in one pass — no
        window function, no correlated subquery. It is aliased `canonical` rather than
        `scored_at` so it cannot collide with the bare column of that name, which holds
        the identical value.

        Predicates belong on the **outer** select, where `_judged_sql()`,
        `_relevant_sql()` and `_sibling_sql()` run unaliased against the row this has
        already chosen. Inside the aggregate they would be answered by rows the group
        is in the middle of discarding.
        """
        return (
            f"SELECT {cols}, MAX(m.scored_at) AS canonical "
            f"FROM matches m {joins} GROUP BY m.job_id"
        )

    def overview_counts(self, run_id: str | None = None) -> dict[str, int]:
        """The overview page's four figures, at run scope or across the install.

        Counted one job at a time rather than one row at a time, so a job that
        resurfaced in several sweeps is one job on the lifetime page — unlike a per-run
        total's totals, which sum per-run counts and say so.

        The two `matches` figures differ by scope in *which* row they ask. A run-scoped
        query needs no choosing: `(run_id, job_id)` is the primary key, so the run holds
        exactly one row per job. Across the install a job can hold rows from several
        sweeps, and `COUNT(DISTINCT job_id)` over them answers "did any row ever say
        so", which keeps a superseded reading alive for good. The lifetime counts
        therefore run over `_canonical_matches_sql` — the same row the page's sections
        render, so the tile and the list below it cannot disagree about a job's state.

        This changes only which row is consulted, never what `shortlisted = 1` means: an
        `excluded` or `expired` job keeps its shortlist row and its place in the tile.
        """
        run_filter = " AND run_id = ?" if run_id else ""
        params: tuple = (run_id,) if run_id else ()
        if run_id:
            relevant_sql = ("SELECT COUNT(DISTINCT job_id) AS n FROM matches "
                            f"WHERE {self._relevant_sql()}" + run_filter)
            shortlisted_sql = ("SELECT COUNT(DISTINCT job_id) AS n FROM matches "
                               "WHERE shortlisted = 1" + run_filter)
        else:
            canonical = self._canonical_matches_sql(
                "m.job_id, m.raw_json, m.skip_reason, m.shortlisted"
            )
            relevant_sql = (f"SELECT COUNT(*) AS n FROM ({canonical}) "
                            f"WHERE {self._relevant_sql()}")
            shortlisted_sql = (f"SELECT COUNT(*) AS n FROM ({canonical}) "
                               "WHERE shortlisted = 1")
        with self._lock:
            seen = self._conn.execute(
                "SELECT COUNT(DISTINCT job_id) AS n FROM jobs WHERE 1=1" + run_filter,
                params,
            ).fetchone()
            relevant = self._conn.execute(relevant_sql, params).fetchone()
            shortlisted = self._conn.execute(shortlisted_sql, params).fetchone()
            # `applied` has no run_id — an application is a fact about a job, not
            # about the sweep that surfaced it — so run scope means "applications to
            # jobs this sweep saw" rather than "applications made during it".
            #
            # Submissions only. An `error` row is an attempt that stopped short — a
            # sign-in gate, a question nothing could answer — and counting it here made
            # the tile promise applications that never reached the employer. Those
            # rows are the page's Needs Attention section instead.
            applied = self._conn.execute(
                "SELECT COUNT(*) AS n FROM applied a WHERE a.status = 'submitted'"
                + (
                    " AND EXISTS (SELECT 1 FROM matches m WHERE m.job_id = a.job_id"
                    " AND m.run_id = ?)" if run_id else ""
                ),
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
    _MATCH_COLUMNS = (
        "m.raw_json, m.relevance_score, m.encoder_score, m.rerank_score_wide, "
        "m.rerank_score, m.yoe_required, m.skipped, m.skip_reason, m.shortlisted, "
        "m.scored_at, j.location, j.updated_at"
    )

    @staticmethod
    def _match_record(row: sqlite3.Row) -> dict:
        record = json.loads(row["raw_json"])
        record["location"] = row["location"] or record.get("location") or ""
        record["posted_at"] = row["updated_at"] or ""
        record["shortlisted"] = bool(row["shortlisted"])
        return record

    def load_lifetime_matches(self, limit: int) -> list[dict]:
        """One row per job_id across every run — its canonical row, best first.

        Row selection is `_canonical_matches_sql`: the job's newest match row, because
        an older one may have been superseded by a later sweep. This used to be two
        calls, one for rows carrying a standing verdict and one for the rest, each
        deduping only *within* itself — so a job holding both kinds of row satisfied
        both queries and the page listed it twice, under contradicting labels — filed
        as issue R1, 381 jobs on a real install.

        The ranking that pair produced survives, because it is the one the page wants:
        judged jobs first by the verdict they got, then everything else by the
        cross-encoder logit that decided whether they were worth a call. `IS NULL`
        first keeps a job that never reached the reranker at the bottom rather than
        the top.
        """
        judged = self._judged_sql()
        rank = f"CASE WHEN {judged} THEN relevance_score ELSE rerank_score END"
        canonical = self._canonical_matches_sql(
            self._MATCH_COLUMNS,
            "LEFT JOIN jobs j ON j.run_id = m.run_id AND j.job_id = m.job_id",
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM ({canonical}) "
                f"ORDER BY ({judged}) DESC, {rank} IS NULL, {rank} DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._match_record(r) for r in rows]

    def load_unmatched_jobs(self, run_id: str | None, limit: int) -> list[dict]:
        """Jobs the funnel never wrote a `matches` row for, at either scope.

        These are the title-gate rejections — `title_excluded` and
        `title_low_relevance` — which `matcher.py` deliberately keeps out of `matches`
        because there can be tens of thousands of them per run. They exist only in
        `jobs`, so this is the only way onto the overview page, and they carry no
        score of any kind: nothing read their descriptions.

        The `NOT EXISTS` is **not** correlated on `run_id`, and that is the whole
        subtlety. A job the `SeenStore` skipped this sweep because an earlier one
        already judged it has no `matches` row for *this* run, and correlating would
        list it here with a blank score as though nothing had ever read it. The price
        is that on second and later sweeps the page's four sections no longer sum to
        the `Jobs in scope` tile. That is the lesser of the two lies.

        Index-backed both ways: `idx_matches_job` serves the subquery and
        `idx_jobs_run` the run-scope filter — which matters because the reports now
        rebuild on a clock for the length of a sweep, not on funnel events.
        """
        scope = " AND j.run_id = ?" if run_id else ""
        params: tuple = (run_id, int(limit)) if run_id else (int(limit),)
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.job_id, j.board_token, j.title, j.location, j.url "
                "FROM jobs j "
                "WHERE NOT EXISTS ("
                "    SELECT 1 FROM matches m WHERE m.job_id = j.job_id)"
                + scope +
                " GROUP BY j.job_id LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "job_id": r["job_id"],
                "board_token": r["board_token"] or "",
                "title": r["title"] or "",
                "location": r["location"] or "",
                "absolute_url": r["url"] or "",
            }
            for r in rows
        ]

    def load_applied_matches(self, run_id: str | None = None) -> list[dict]:
        """Every application, carrying the job's best match row where one exists.

        LEFT JOIN because an application can outlive the sweep that found it: the
        `matches` rows for a run are per-run, `applied` is forever, and a user who
        prunes old runs should still see what they applied to. Rows with no match
        keep their title and company from `applied` and simply have no rationales.

        The joined row is the job's **newest** match, the same one
        `_canonical_matches_sql` picks for every other lifetime read, so an application
        cannot be rendered from a row the page has stopped believing elsewhere. It used
        to be the row with the best `relevance_score`, which differs only for a job
        judged more than once.

        Ordered by the LLM's verdict, best first, so the applied accordion ranks the
        same way every other list on the overview page does. An application with no
        match row left to point at sorts last rather than first, and the timestamp
        breaks ties — nothing is lost by demoting it from the primary key, because
        `_job_entry` prints it in the meta line either way.
        """
        scope = (
            " WHERE EXISTS (SELECT 1 FROM matches mm WHERE mm.job_id = a.job_id"
            " AND mm.run_id = ?)" if run_id else ""
        )
        params: tuple = (run_id,) if run_id else ()
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.job_id, a.board_token, a.title, a.absolute_url, "
                "       a.applied_at, a.status, a.error, "
                f"       {self._MATCH_COLUMNS} "
                "FROM applied a "
                "LEFT JOIN matches m ON m.rowid = ("
                "    SELECT rowid FROM matches WHERE job_id = a.job_id "
                "    ORDER BY scored_at DESC, rowid DESC LIMIT 1) "
                "LEFT JOIN jobs j ON j.run_id = m.run_id AND j.job_id = m.job_id "
                + scope +
                " ORDER BY m.relevance_score IS NULL, m.relevance_score DESC,"
                " a.applied_at DESC",
                params,
            ).fetchall()

        out: list[dict] = []
        for r in rows:
            record = self._match_record(r) if r["raw_json"] else {
                "job_id": r["job_id"],
                "board_token": r["board_token"],
                "title": r["title"],
                "absolute_url": r["absolute_url"],
                "location": "",
            }
            record["applied_at"] = r["applied_at"]
            record["applied_status"] = r["status"]
            record["applied_error"] = r["error"]
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
            "SELECT encoder_score, rerank_score, relevance_score FROM matches "
            "WHERE relevance_score IS NOT NULL AND rerank_score IS NOT NULL "
            "AND (skipped = 0 OR skipped IS NULL)"
        )
        params: tuple = ()
        if run_id:
            sql += " AND run_id = ?"
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
        page's `Jobs in scope` tile. `submitted` and `attention` split the applier
        bar; they count applications to this run's shortlisted *representatives*,
        because siblings are never sent to the worker and would hold the bar short.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_progress WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            jobs = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE run_id = ?", (run_id,)
            ).fetchone()
            applied = self._conn.execute(
                "SELECT SUM(CASE WHEN a.status = 'submitted' THEN 1 ELSE 0 END) AS ok, "
                "       SUM(CASE WHEN a.status != 'submitted' THEN 1 ELSE 0 END) AS bad "
                "FROM applied a WHERE EXISTS (SELECT 1 FROM matches m "
                "  WHERE m.job_id = a.job_id AND m.run_id = ? AND m.shortlisted = 1 "
                f"  AND NOT ({self._sibling_sql('m')}))",
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

    def lifetime_progress(self) -> dict:
        """The lifetime page's bars: two sums and one backlog.

        The scraper and matcher figures are sums over every tracked sweep, because
        nothing else can count them. `jobs_in_scope` is scoped to those same sweeps,
        so jobs from runs that predate progress tracking cannot hold the matcher bar
        short.

        The applier is different on purpose: every distinct shortlisted
        representative this install has ever had, against how many have an
        application. A sum of per-sweep counters reads ~100% whenever no sweep is
        running; the backlog keeps meaning something between sweeps, and it covers
        runs made before tracking began. Siblings are out for the reason they are out
        of `run_progress` — nothing ever applies to them.

        The shortlist half reads canonical rows, for the reason `overview_counts` does:
        this bar's total *is* the `Jobs shortlisted` tile, and the two must be the same
        number by construction rather than by coincidence.

        Groups whole tables, so it belongs on the lifetime page's slower throttle.
        """
        canonical = self._canonical_matches_sql("m.job_id, m.raw_json, m.shortlisted")
        shortlist = (
            f"SELECT job_id FROM ({canonical}) "
            f"WHERE shortlisted = 1 AND NOT ({self._sibling_sql()})"
        )
        with self._lock:
            sums = self._conn.execute(
                "SELECT COUNT(*) AS sweeps, "
                "       COALESCE(SUM(companies_total), 0) AS companies_total, "
                "       COALESCE(SUM(companies_done), 0) AS companies_done, "
                "       COALESCE(SUM(jobs_processed), 0) AS jobs_processed "
                "FROM run_progress"
            ).fetchone()
            jobs = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs "
                "WHERE run_id IN (SELECT run_id FROM run_progress)"
            ).fetchone()
            # What the scraper bar prints. Every run, tracked or not, and each posting
            # once however many sweeps found it — the lifetime `Jobs in scope` rule.
            unique = self._conn.execute(
                "SELECT COUNT(DISTINCT job_id) AS n FROM jobs"
            ).fetchone()
            shortlisted = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM ({shortlist})"
            ).fetchone()
            applied = self._conn.execute(
                "SELECT SUM(CASE WHEN a.status = 'submitted' THEN 1 ELSE 0 END) AS ok, "
                "       SUM(CASE WHEN a.status != 'submitted' THEN 1 ELSE 0 END) AS bad "
                f"FROM applied a WHERE a.job_id IN ({shortlist})"
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
        yoe_required: float | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO matches"
                "(run_id, job_id, board_token, title, relevance_score, encoder_score, "
                " rerank_score_wide, rerank_score, yoe_required, shortlisted, "
                " skipped, skip_reason, source_run_id, scored_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, job_id, board_token, title, relevance_score, encoder_score,
                 rerank_score_wide, rerank_score, yoe_required, int(shortlisted),
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

        Backs the results CSV. LEFT JOIN because a match row must survive even if
        its jobs row is missing — a partial export beats an export that silently
        drops rows.

        Ordered best-first: LLM score, then the cross-encoder logit, then the old
        wide-pass column. The two rerank columns are sorted in sequence rather than
        merged because on rows old enough to carry both they came from different
        models and were never comparable.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.raw_json, m.relevance_score, m.encoder_score, "
                "       m.rerank_score_wide, m.rerank_score, m.yoe_required, "
                "       m.skipped, m.skip_reason, "
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

        **No `run_id` filter.** `matches` is keyed `(run_id, job_id)` and a job reached
        from the backlog belongs to an earlier sweep, so every row for it is updated —
        which also avoids leaving the stale duplicate the lifetime page would then
        render twice, once under its real verdict and once under this one.

        `AND shortlisted = 1` makes it idempotent and a no-op for a job already
        retired. Returns how many rows changed, so the caller can log a miss.

        Note this lowers the `Jobs shortlisted` tile and the applier bar's denominator
        (`overview_counts`, `lifetime_progress`), retroactively and on both scopes.
        That is correct — the job is no longer waiting to be applied to — and is not a
        discrepancy to reconcile.
        """
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT run_id, raw_json FROM matches "
                "WHERE job_id = ? AND shortlisted = 1",
                (job_id,),
            ).fetchall()
            for row in rows:
                raw = json.loads(row["raw_json"])
                # The column and the blob both, because they are read from different
                # places: `_match_record` overrides `shortlisted` from the column but
                # takes `skip_reason` straight out of `raw_json`. `applier_location`
                # is an extra key rather than a `MatchResult` field — the blob is
                # loaded as a plain dict, and nothing else needs to know about it.
                raw["skip_reason"] = reason
                if location:
                    raw["applier_location"] = location
                self._conn.execute(
                    "UPDATE matches SET shortlisted = 0, skip_reason = ?, "
                    "raw_json = ? WHERE run_id = ? AND job_id = ?",
                    (reason, json.dumps(raw), row["run_id"], job_id),
                )
        return len(rows)

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

    def recent_submissions(self, since_iso: str,
                           company: str | None = None) -> dict[str, list[str]]:
        """`submitted` stamps since `since_iso`, keyed by lowercased, trimmed company.

        What the per-company cap counts (`hireshire/applier/limits.py`). `company`
        narrows it to one key, which is how the worker asks before each launch; the
        overview page asks for every company at once. Only `submitted` counts, which
        includes a job marked applied by hand — that writes plain `submitted` too.
        Install-wide on purpose: an employer does not care which sweep applied.
        """
        sql = ("SELECT LOWER(TRIM(board_token)) AS company, applied_at FROM applied "
               "WHERE status = 'submitted' AND applied_at >= ?")
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
        # `dry_run` is deliberately not selected. The column survives in the schema
        # because rows written before the applier became on/off carry real values and
        # SQLite makes dropping a column awkward, but nothing reads it any more.
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, board_token, title, absolute_url, applied_at, status, "
                "screenshot, error FROM applied ORDER BY applied_at"
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
    ) -> None:
        # The legacy `dry_run` column is written as 0 rather than left NULL, so old
        # readers that still coerce it with bool() see "not a rehearsal" instead of
        # tripping over None.
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO applied"
                "(job_id, board_token, title, absolute_url, applied_at, status, "
                " dry_run, screenshot, error) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (job_id, board_token, title, absolute_url, applied_at, status,
                 screenshot, error),
            )

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

        Returns `"updated"` when an existing attempt was promoted, `"inserted"` when a
        shortlisted job had no `applied` row yet, or `"unknown"` when nothing on record
        names this job — which is the only honest answer to a job_id the database has
        never seen, and is why this does not blindly insert.

        **A narrow `UPDATE`, not `record_applied`.** That writer is `INSERT OR REPLACE`
        on the `job_id` primary key, so re-recording through it would blank
        `board_token`, `title` and `absolute_url` — the three columns
        `load_applied_matches` falls back on when the job's `matches` rows have been
        pruned, i.e. exactly the old applications this feature exists to tidy up.

        `error` is cleared because it is the reason the job needed attention and that
        reason is now discharged; `screenshot` is kept, because a partial capture of the
        form is still the user's own record of the attempt.

        The status written is plain `submitted`, which makes a hand-marked application
        indistinguishable from an automatic one on the page. That is accepted rather
        than overlooked: a second "counts as applied" status would have to be added to
        every site that tests the literal — `overview_counts`, `run_progress`,
        `lifetime_progress`, `data.overview_snapshot` — and each omission would be a
        silent undercount. Provenance, if it is ever wanted, belongs in a new column.
        """
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE applied SET status = 'submitted', applied_at = ?, error = NULL "
                "WHERE job_id = ?",
                (applied_at, job_id),
            ).rowcount
            if changed:
                return "updated"
            # No attempt on record, so this is a shortlisted job the user got to first.
            # Its identity comes from the canonical match row for the same reason every
            # other lifetime read uses that rule: `matches` is keyed `(run_id, job_id)`
            # and a job that was deferred once carries more than one row.
            canonical = self._canonical_matches_sql(
                "m.job_id, m.board_token, m.title, m.raw_json"
            )
            row = self._conn.execute(
                f"SELECT * FROM ({canonical}) WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return "unknown"
            try:
                url = (json.loads(row["raw_json"]) or {}).get("absolute_url") or ""
            except (TypeError, ValueError):
                url = ""
            self._conn.execute(
                "INSERT OR REPLACE INTO applied"
                "(job_id, board_token, title, absolute_url, applied_at, status, "
                " dry_run, screenshot, error) "
                "VALUES (?, ?, ?, ?, ?, 'submitted', 0, NULL, NULL)",
                (job_id, row["board_token"] or "", row["title"] or "", url, applied_at),
            )
        return "inserted"

    def decline_job(self, job_id: str) -> dict:
        """Record that the user is not pursuing this job. Idempotent.

        Returns `{"deleted": bool, "unshortlisted": int}` — what actually changed, so
        the caller can tell a real decision from a repeat.

        Two writes, and both are needed:

        * **Delete any `applied` row.** A failed attempt is what puts the job under
          Needs Attention, and `applied_ids` is what keeps it out of every other
          section. Leaving the row and giving it a new status would not work: any status
          that is not `submitted` renders under Needs Attention by design.
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
        already retired. That is reported rather than treated as a failure: deleting the
        `applied` row is on its own enough to clear Needs Attention.
        """
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "DELETE FROM applied WHERE job_id = ?", (job_id,)
            ).rowcount
        unshortlisted = self.mark_not_shortlisted(job_id, DECLINED_BY_USER)
        return {"deleted": bool(deleted), "unshortlisted": unshortlisted}

    def _unapplied(self, having: str, stamp_iso: str) -> list[dict]:
        """Shortlisted representatives with no `applied` row, split on their age.

        The body of `load_pending_applications` and `load_expired_applications`, which
        are the two halves of one set and must partition it exactly: a job the backlog
        can no longer see is a job the applier will never be handed again, and that is
        the whole basis for retiring it.

        **The age test is a `HAVING` on the aggregate, not a `WHERE` on the row**, and
        that is load-bearing. `matches` is keyed `(run_id, job_id)`, so a job scored in
        one sweep and rescored in a later one has two rows — the same fact
        `_canonical_matches_sql` exists for, resolved the same way. A row-level
        `scored_at <` would match the stale row and report a job as expired
        while its fresh row still sits in the backlog — the applier would retire a job
        it is actively retrying. Against `MAX(scored_at)` the two predicates are
        complements by construction.

        Filtering after the group does not move the bare columns: SQLite takes them
        from the row that produced the `MAX()`, which is the newest match either way.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.job_id, m.board_token, m.title, m.relevance_score, m.raw_json, "
                "MAX(m.scored_at) AS scored_at "
                "FROM matches m "
                "WHERE m.shortlisted = 1 "
                f"AND NOT ({self._sibling_sql('m')}) "
                "AND NOT EXISTS (SELECT 1 FROM applied a WHERE a.job_id = m.job_id) "
                "GROUP BY m.job_id "
                f"HAVING MAX(m.scored_at) {having} ? "
                "ORDER BY m.relevance_score DESC",
                (stamp_iso,),
            ).fetchall()
        out = []
        for r in rows:
            raw = json.loads(r["raw_json"])
            out.append({
                "job_id": r["job_id"],
                "company": r["board_token"],
                "title": r["title"],
                "job_url": raw.get("absolute_url") or "",
                "relevance_score": r["relevance_score"],
                "scored_at": r["scored_at"],
            })
        return out

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
        tables = ("runs", "run_companies", "jobs", "matches", "pipeline_results",
                  "run_progress")
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
