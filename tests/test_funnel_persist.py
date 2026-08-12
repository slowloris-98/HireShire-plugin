"""Persistence of funnel-hydrated descriptions back to the jobs table.

The funnel fills content_text for list-only Workday/BambooHR jobs in-memory only;
`_persist_hydrated_details` upserts those descriptions into the jobs table so
DB-backed readers (standalone tuner/apply, re-runs, exports) see them. No network.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from hireshire.models.job import Job
from hireshire.storage.db import Database
from matcher import _persist_hydrated_details

RUN_ID = "test-run"


def make_job(job_id, source, content_text=None, detail_fetch_failed=False) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source=source,
        board_token="acme",
        job_id=job_id,
        title=f"{source} job {job_id}",
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now,
        content_text=content_text,
        detail_fetch_failed=detail_fetch_failed,
        scraped_at=now,
    )


def test_persist_hydrated_details_upserts_content(tmp_path):
    db = Database(tmp_path / "test.db")

    # Scrape-time state: list-only Workday rows with no description yet.
    db.insert_jobs(RUN_ID, [
        make_job("wd1", "workday", content_text=None),
        make_job("wd2", "workday", content_text=None),
    ])
    before = {j.job_id: j for j in db.load_jobs(RUN_ID)}
    assert before["wd1"].content_text is None
    assert before["wd2"].content_text is None

    # After the funnel gate: wd1 hydrated OK, wd2 hydrate failed, plus a greenhouse
    # survivor that must NOT be written (already carries content; not a detail board).
    wd1 = before["wd1"].model_validate({
        **before["wd1"].model_dump(),
        "content_text": "<p>Build <b>backend</b> services</p>",
        "detail_fetch_failed": False,
    })
    wd2 = before["wd2"].model_validate({
        **before["wd2"].model_dump(), "detail_fetch_failed": True,
    })
    gh1 = make_job("gh1", "greenhouse", content_text="greenhouse desc")

    asyncio.run(_persist_hydrated_details(db, RUN_ID, [wd1, wd2, gh1]))

    after = {j.job_id: j for j in db.load_jobs(RUN_ID)}
    # wd1: description persisted (and HTML stripped by the model validator).
    assert after["wd1"].content_text == "Build backend services"
    # wd2: failure recorded, content still empty.
    assert after["wd2"].content_text is None
    assert after["wd2"].detail_fetch_failed is True
    # gh1: filtered out — non-detail board was never inserted.
    assert "gh1" not in after
    assert set(after) == {"wd1", "wd2"}


def test_persist_hydrated_details_noop_when_nothing_changed(tmp_path):
    db = Database(tmp_path / "test.db")
    # A detail-board survivor that was never hydrated (no content, not failed) and a
    # non-detail board: neither should be written.
    asyncio.run(_persist_hydrated_details(db, RUN_ID, [
        make_job("wd1", "workday", content_text=None, detail_fetch_failed=False),
        make_job("gh1", "greenhouse", content_text="x"),
    ]))
    assert db.load_jobs(RUN_ID) == []
