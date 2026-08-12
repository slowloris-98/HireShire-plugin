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
