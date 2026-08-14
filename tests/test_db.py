"""Unit tests for the shared SQLite storage layer (hireshire.storage.db)."""
from __future__ import annotations

from datetime import datetime, timezone

from hireshire.models.job import Job, Location
from hireshire.storage.db import PHASE_SCRAPE, Database


def _job(job_id: str, token: str = "acme") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=token,
        job_id=job_id,
        title="Backend Engineer",
        location=Location(name="Remote"),
        absolute_url="https://example.com/jobs/" + job_id,  # type: ignore[arg-type]
        updated_at=now,
        scraped_at=now,
        content_text="We need a backend engineer.",
    )


def _db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def test_zero_job_company_writes_no_job_rows(tmp_path):
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"

    # A company that fetched successfully with zero jobs: metadata row only.
    db.record_company(run_id, "emptyco", "greenhouse", "ok", 0, 0.1, None)
    db.insert_jobs(run_id, [])  # no-op

    assert db.load_jobs(run_id) == []

    # A company with jobs writes rows.
    db.record_company(run_id, "acme", "greenhouse", "ok", 2, 0.2, None)
    db.insert_jobs(run_id, [_job("j1"), _job("j2")])

    jobs = db.load_jobs(run_id)
    assert {j.job_id for j in jobs} == {"j1", "j2"}


def test_latest_run(tmp_path):
    db = _db(tmp_path)
    assert db.latest_run(PHASE_SCRAPE) is None

    db.finalise_run("2026-07-01T00-00-00Z", PHASE_SCRAPE, "2026-07-01T00:00:00+00:00")
    db.finalise_run("2026-07-05T00-00-00Z", PHASE_SCRAPE, "2026-07-05T00:00:00+00:00")

    assert db.latest_run(PHASE_SCRAPE) == "2026-07-05T00-00-00Z"


def test_shortlisted_and_seen_roundtrip(tmp_path):
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.upsert_match(run_id, "j1", "acme", "Eng", 85, True, False, None,
                    run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}')
    db.upsert_match(run_id, "j2", "acme", "Eng", 40, False, False, None,
                    run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j2"}')

    shortlisted = db.load_shortlisted(run_id)
    assert [r["job_id"] for r in shortlisted] == ["j1"]

    db.mark_seen(["j1", "j2"])
    assert db.seen_ids() == {"j1", "j2"}


def test_prune_keeps_recent(tmp_path):
    db = _db(tmp_path)
    for day in ("01", "02", "03"):
        rid = f"2026-07-{day}T00-00-00Z"
        db.finalise_run(rid, PHASE_SCRAPE, f"2026-07-{day}T00:00:00+00:00")
        db.insert_jobs(rid, [_job(f"j{day}")])

    deleted = db.prune_runs(keep=1)
    assert deleted == ["2026-07-01T00-00-00Z", "2026-07-02T00-00-00Z"]
    assert db.all_run_ids() == ["2026-07-03T00-00-00Z"]
    assert db.load_jobs("2026-07-01T00-00-00Z") == []


def test_funnel_scores_roundtrip_on_matches(tmp_path):
    """The three funnel scores get their own columns so the export can sort and
    filter on them without parsing raw_json for every row."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.upsert_match(
        run_id, "j1", "acme", "Eng", 72, True, False, None,
        run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}',
        encoder_score=0.61, rerank_score_wide=-3.2, rerank_score=-1.4,
    )
    row = db._conn.execute(
        "SELECT encoder_score, rerank_score_wide, rerank_score FROM matches "
        "WHERE run_id=? AND job_id=?", (run_id, "j1"),
    ).fetchone()
    assert (row["encoder_score"], row["rerank_score_wide"], row["rerank_score"]) == (
        0.61, -3.2, -1.4,
    )


def test_load_all_matches_returns_every_row_best_first(tmp_path):
    """Unlike load_shortlisted, this keeps the drops — they are the whole point of
    the all-jobs export. Ordering puts LLM-scored rows first, then refined, then
    wide, because the three are not comparable to one another."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("j1"), _job("j2"), _job("j3")])

    db.upsert_match(run_id, "j1", "acme", "Eng", 40, False, False, None, run_id,
                    "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}',
                    rerank_score=-2.0, rerank_score_wide=-4.0)
    db.upsert_match(run_id, "j2", "acme", "Eng", 72, True, False, None, run_id,
                    "2026-07-07T00:00:00+00:00", '{"job_id": "j2"}',
                    rerank_score=-9.0, rerank_score_wide=-9.5)
    # A budget drop: never LLM-scored, so it sorts below both regardless of score.
    db.upsert_match(run_id, "j3", "acme", "Eng", None, False, True,
                    "rerank_below_top_k", run_id,
                    "2026-07-07T00:00:00+00:00", '{"job_id": "j3"}',
                    rerank_score_wide=-1.0)

    rows = db.load_all_matches(run_id)
    assert [r["job_id"] for r in rows] == ["j2", "j1", "j3"]
    # The join fills in what the match row does not carry.
    assert rows[0]["location"] == "Remote"
    assert rows[0]["posted_at"]
    assert rows[0]["shortlisted"] is True


def test_load_all_matches_survives_a_missing_jobs_row(tmp_path):
    """LEFT JOIN: a partial export beats one that silently drops rows."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.upsert_match(run_id, "orphan", "acme", "Eng", 50, False, False, None, run_id,
                    "2026-07-07T00:00:00+00:00", '{"job_id": "orphan"}')
    rows = db.load_all_matches(run_id)
    assert [r["job_id"] for r in rows] == ["orphan"]
    assert rows[0]["location"] == ""


def test_columns_are_added_to_a_database_that_predates_them(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing file, so a new column
    would never reach it and the next INSERT would fail with 'no such column'."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE matches (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "board_token TEXT, title TEXT, relevance_score INTEGER, "
        "shortlisted INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, skip_reason TEXT, "
        "source_run_id TEXT, scored_at TEXT, raw_json TEXT NOT NULL, "
        "PRIMARY KEY (run_id, job_id))"
    )
    old.commit()
    old.close()

    db = Database(path)
    columns = {r["name"] for r in db._conn.execute("PRAGMA table_info(matches)")}
    assert {"encoder_score", "rerank_score_wide"} <= columns

    # And the upsert that would previously have failed now works.
    db.upsert_match("r", "j1", "acme", "Eng", 10, False, False, None, "r",
                    "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}',
                    encoder_score=0.5)
    assert db.load_all_matches("r")[0]["job_id"] == "j1"
