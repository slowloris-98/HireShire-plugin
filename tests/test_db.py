"""Unit tests for the shared SQLite storage layer (hireshire.storage.db)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hireshire.models.job import Job, Location
from hireshire.storage.db import (
    DECLINED_BY_USER,
    PHASE_SCRAPE,
    Database,
    SchemaMigrationBlocked,
)


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
    db.insert_jobs(run_id, [_job("j1"), _job("j2")])
    db.upsert_match(run_id, "j1", "acme", "Eng", 85, True, False, None,
                    run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}')
    db.upsert_match(run_id, "j2", "acme", "Eng", 40, False, False, None,
                    run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j2"}')

    shortlisted = db.load_shortlisted(run_id)
    assert [r["job_id"] for r in shortlisted] == ["j1"]

    # Pairs, not bare ids: `job_id` is only unique per board, and an id-keyed set is
    # what let one employer's verdict retire another employer's posting for good.
    db.mark_seen([("acme", "j1"), ("acme", "j2")])
    assert db.seen_ids() == {("acme", "j1"), ("acme", "j2")}


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


def test_funnel_scores_roundtrip_on_the_posting(tmp_path):
    """The three funnel scores get their own columns so the export can sort and
    filter on them without parsing a JSON blob for every row."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("j1")])
    db.upsert_match(
        run_id, "j1", "acme", "Eng", 72, True, False, None,
        run_id, "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}',
        encoder_score=0.61, rerank_score_wide=-3.2, rerank_score=-1.4,
    )
    row = db._conn.execute(
        "SELECT encoder_score, rerank_score_wide, rerank_score FROM postings "
        "WHERE board_token=? AND job_id=?", ("acme", "j1"),
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


def test_a_verdict_cannot_lose_the_posting_it_was_reached_on(tmp_path):
    """The export's `location` and `posted_at` can no longer come back blank.

    This replaces a test about a `LEFT JOIN` surviving a missing `jobs` row — the
    export joined a per-run verdict to a per-run sighting, and the two could be in
    different runs, so a partial export beat one that silently dropped rows. A verdict
    is written onto the posting itself now, so there is nothing left to join and
    nothing to go missing.
    """
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("j1")])
    db.upsert_match(run_id, "j1", "acme", "Eng", 50, False, False, None, run_id,
                    "2026-07-07T00:00:00+00:00", '{"job_id": "j1"}')

    rows = db.load_all_matches(run_id)
    assert [r["job_id"] for r in rows] == ["j1"]
    assert rows[0]["location"] == "Remote"

    # And a verdict written against a posting nothing scraped records nothing at all,
    # rather than a row with no posting behind it. The writer says so in its rowcount,
    # which is what lets `MatchStore` log the miss instead of losing it silently.
    assert db.upsert_match(run_id, "orphan", "acme", "Eng", 50, False, False, None,
                           run_id, "2026-07-07T00:00:00+00:00", "{}") == 0
    assert [r["job_id"] for r in db.load_all_matches(run_id)] == ["j1"]


def test_columns_are_added_to_a_database_that_predates_them(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing file, so a new column
    would never reach it and the next INSERT would fail with 'no such column'.

    Tested on `pipeline_results`, which is one of the four tables the `postings` merge
    kept. The mechanism still matters for those; for the merged tables it only has to
    hold long enough for the migration to read them, which
    `test_the_merge_reads_columns_a_v1_file_gained_late` covers.
    """
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE pipeline_results (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "company TEXT, title TEXT, location TEXT, posted_at TEXT, job_url TEXT, "
        "relevance_score INTEGER, rerank_score REAL, found_at TEXT, "
        "PRIMARY KEY (run_id, job_id))"
    )
    old.commit()
    old.close()

    db = Database(path)
    columns = {r["name"]
               for r in db._conn.execute("PRAGMA table_info(pipeline_results)")}
    assert {"encoder_score", "rerank_score_wide"} <= columns

    # And the insert that would previously have failed now works.
    db.record_pipeline_result("r", {"job_id": "j1", "encoder_score": 0.5})
    assert db.load_pipeline_results("r")[0]["job_id"] == "j1"


# -- outcomes the user records by hand ---------------------------------------


def _shortlisted(db: Database, job_id: str, run_id: str, *, score: int = 85,
                 url: str = "https://example.com/j") -> None:
    """One shortlisted, judged posting — what the applier is handed.

    The scrape comes first, because `upsert_match` is an UPDATE onto the row the
    scraper made. `match_json` carries the score and `skipped` because
    `load_all_matches` reads the record out of the blob and only overrides a few
    columns, so a minimal blob would hide exactly the keys these tests are about.
    """
    raw = json.dumps({
        "job_id": job_id, "absolute_url": url, "board_token": "acme",
        "title": "Backend Engineer", "relevance_score": score, "skipped": False,
        "skip_reason": None,
    })
    if not db.get_jobs([job_id]):
        db.insert_jobs(run_id, [_job(job_id)])
    db.upsert_match(run_id, job_id, "acme", "Backend Engineer", score, True, False,
                    None, run_id, "2026-07-07T00:00:00+00:00", raw)


def _attempt(db: Database, job_id: str, status: str = "error") -> None:
    """An application that stopped short — a Needs Attention row."""
    if not db.get_jobs([job_id]):
        db.insert_jobs("2026-07-07T00-00-00Z", [_job(job_id)])
    db.record_applied(job_id, "acme", "Backend Engineer", "https://example.com/j",
                      "2026-07-07T01:00:00+00:00", status, "shot.png",
                      "Stuck on a required question — check whether it was submitted.")


def test_where_an_application_came_from_round_trips(tmp_path):
    """The backlog flag is stored because it cannot be derived: `applied` has no
    `run_id`, so after the fact nothing can tell which sweep did the applying."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)
    _shortlisted(db, "j2", run_id, url="https://example.com/k")
    db.record_applied("j1", "acme", "Backend Engineer", "https://example.com/j",
                      "2026-07-08T09:00:00+00:00", "submitted", None, None,
                      from_backlog=True)
    db.record_applied("j2", "acme", "Backend Engineer", "https://example.com/k",
                      "2026-07-08T09:05:00+00:00", "submitted", None, None)

    rows = {r["job_id"]: r["applied_from_backlog"]
            for r in db.load_applied_matches(run_id)}
    assert rows == {"j1": True, "j2": False}


def test_marking_an_attempt_applied_promotes_the_row_it_already_has(tmp_path):
    """The Needs Attention case: the columns a pruned install depends on survive.

    `record_applied` was `INSERT OR REPLACE`, so re-recording through it would blank
    board_token, title and absolute_url. It is a named-column `UPDATE` now and those
    columns belong to the scraper, so the hazard is gone at the root — but the reason
    this is a separate writer is unchanged, and so is what it must not disturb.
    """
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)
    _attempt(db, "j1")

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "updated"

    row = next(r for r in db.load_applied() if r["job_id"] == "j1")
    assert row["status"] == "submitted"
    assert row["applied_at"] == "2026-07-08T09:00:00+00:00"
    # The reason it needed attention is discharged; the capture of the form is not.
    assert row["error"] is None
    assert row["screenshot"] == "shot.png"
    # Identity comes off the posting the scraper wrote, which is the only copy now —
    # `applied` used to keep its own so that an application could outlive the
    # `matches` rows it pointed at.
    assert (row["board_token"], row["title"]) == ("acme", "Backend Engineer")
    assert row["absolute_url"] == "https://example.com/jobs/j1"


def test_marking_a_shortlisted_job_applied_records_it_on_the_posting(tmp_path):
    """The Shortlisted case: no attempt exists, and there is still only one row.

    This used to need the second of three identity sources — `applied`, then the
    canonical `matches` row, then `jobs` — and returned `"inserted"` because it had to
    create an `applied` row. One row per posting leaves one lookup and one verdict,
    `"updated"`, for every section the page offers this on.
    """
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id, url="https://example.com/jobs/j1")

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "updated"

    row = next(r for r in db.load_applied() if r["job_id"] == "j1")
    assert row["status"] == "submitted"
    assert row["title"] == "Backend Engineer"
    assert row["board_token"] == "acme"
    assert row["absolute_url"] == "https://example.com/jobs/j1"
    # And it is out of the applier's reach for good.
    assert db.load_pending_applications("1970-01-01T00:00:00+00:00") == []


def test_the_title_gates_verdict_survives_the_hydration_upsert(tmp_path):
    """`record_gate_reasons` writes onto a row `insert_jobs` owns, and that writer is
    `INSERT OR REPLACE` — so the one thing this has to prove is that re-inserting the
    job to attach its description does not blank the reason."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("j1"), _job("j2"), _job("j3")])
    db.record_gate_reasons(run_id, [("acme", "j1", "title_excluded"),
                                    ("acme", "j2", "title_low_relevance")])

    db.insert_jobs(run_id, [_job("j1")])  # the hydration upsert

    rows = {r["job_id"]: r for r in db.load_unmatched_jobs(run_id, 50)}
    assert rows["j1"]["gate_reason"] == "title_excluded"
    assert rows["j2"]["gate_reason"] == "title_low_relevance"
    # A job the gate let through carries no reason, and reads as one rather than as "".
    assert rows["j3"]["gate_reason"] == ""
    db.record_gate_reasons(run_id, [])  # no-op


def test_a_reason_recorded_on_any_sweep_wins_at_lifetime_scope(tmp_path):
    """The `SeenStore` skips a job an earlier sweep judged, so the later run's row has
    no reason at all. A bare column could hand back that NULL and lose the verdict."""
    db = _db(tmp_path)
    db.insert_jobs("2026-07-07T00-00-00Z", [_job("j1")])
    db.record_gate_reasons("2026-07-07T00-00-00Z", [("acme", "j1", "title_excluded")])
    db.insert_jobs("2026-07-08T00-00-00Z", [_job("j1")])

    rows = db.load_unmatched_jobs(None, 50)
    assert [r["gate_reason"] for r in rows] == ["title_excluded"]


def test_a_re_scraped_job_belongs_to_the_sweep_that_first_saw_it(tmp_path):
    """The headline rule for the overview's last section, at run scope.

    The scraper writes a fresh `jobs` row for every posting on every sweep, so without
    the first-sighting anti-join the seventh sweep's page re-lists every posting the
    first sweep's title gate already threw out — on a mature install, most of the
    section. `j2` is the control: it is new to the later sweep and must still appear.
    """
    db = _db(tmp_path)
    first, later = "2026-07-07T00-00-00Z", "2026-07-08T00-00-00Z"
    db.insert_jobs(first, [_job("j1")])
    db.record_gate_reasons(first, [("acme", "j1", "title_excluded")])
    db.insert_jobs(later, [_job("j1"), _job("j2")])
    db.record_gate_reasons(later, [("acme", "j2", "title_low_relevance")])

    assert [r["job_id"] for r in db.load_unmatched_jobs(first, 50)] == ["j1"]
    assert [r["job_id"] for r in db.load_unmatched_jobs(later, 50)] == ["j2"]


def test_a_day_lists_the_jobs_that_day_first_saw_exactly_once(tmp_path):
    """Day scope means new to the **day**, and the anti-join is deliberately not
    narrowed to the day's id set — which is what makes a posting the 9am sweep found and
    the 1pm sweep found again appear once rather than twice or not at all. Its earliest
    row is inside the day and survives; the later row is killed."""
    db = _db(tmp_path)
    morning, afternoon = "2026-07-07T09-00-00Z", "2026-07-07T13-00-00Z"
    next_day = "2026-07-08T09-00-00Z"
    db.insert_jobs(morning, [_job("j1")])
    db.record_gate_reasons(morning, [("acme", "j1", "title_excluded")])
    db.insert_jobs(afternoon, [_job("j1")])
    db.insert_jobs(next_day, [_job("j1")])

    day = db.load_unmatched_jobs(None, 50, run_ids=[morning, afternoon])
    assert [r["job_id"] for r in day] == ["j1"]
    assert db.load_unmatched_jobs(None, 50, run_ids=[next_day]) == []
    # Lifetime was always right — `GROUP BY job_id` collapsed the repeats — and the
    # anti-join must not change that.
    assert [r["job_id"] for r in db.load_unmatched_jobs(None, 50)] == ["j1"]


def test_a_reason_recorded_on_a_later_sweep_still_reaches_the_page(tmp_path):
    """The inverse of the test above this pair, and the one that fails if the
    `COALESCE` lookup is ever "simplified" back to a bare column.

    A sweep killed outright (`--stop` is `taskkill /F`, which runs no `finally`) leaves
    `jobs` rows the matcher never gated: no `gate_reason`, and no `seen_jobs` entry
    either, so the *next* sweep gates the job and holds the reason. The anti-join keeps
    the earlier NULL row, so the reason has to be fetched across runs rather than read
    off the row that survived.
    """
    db = _db(tmp_path)
    killed, next_sweep = "2026-07-07T00-00-00Z", "2026-07-08T00-00-00Z"
    db.insert_jobs(killed, [_job("j1")])  # scraped, never gated
    db.insert_jobs(next_sweep, [_job("j1")])
    db.record_gate_reasons(next_sweep, [("acme", "j1", "title_excluded")])

    assert [r["gate_reason"] for r in db.load_unmatched_jobs(None, 50)] == ["title_excluded"]
    # And at the scope the job is now filed under, which is the killed sweep.
    assert [r["gate_reason"] for r in db.load_unmatched_jobs(killed, 50)] == ["title_excluded"]
    assert db.load_unmatched_jobs(next_sweep, 50) == []


def test_the_jobs_in_scope_tile_counts_work_done_not_rows_held(tmp_path):
    """The tile and the matcher bar's denominator are one query, and it is deliberately
    **wider** than the last section's list: first sightings *plus* jobs this scope
    reached a verdict on.

    `j2` is the case that forces the second half. The call cap deferred it on the first
    sweep — no verdict, not retired into `seen_jobs` — and the later sweep judges it. A
    strict first-sighting tile would leave it out while the page's Relevant and Jobs
    Filtered sections list it, putting the top of the funnel below the lists beneath it.
    `j1` is the control: merely re-scraped, so it is work the first sweep did.
    """
    db = _db(tmp_path)
    first, later = "2026-07-07T00-00-00Z", "2026-07-08T00-00-00Z"
    db.insert_jobs(first, [_job("j1"), _job("j2")])
    db.record_gate_reasons(first, [("acme", "j1", "title_excluded")])
    db.insert_jobs(later, [_job("j1"), _job("j2")])
    db.upsert_match(later, "j2", "acme", "Eng", 70, False, False, None, later,
                    "2026-07-08T00:00:00+00:00", '{"job_id": "j2"}')

    assert db.overview_counts(run_id=first)["seen"] == 2
    assert db.overview_counts(run_id=later)["seen"] == 1  # j2 only, re-judged
    assert db.overview_counts(run_ids=[first, later])["seen"] == 2
    # Lifetime is unchanged by all of this: every posting's earliest row passes the
    # first-sighting half, so it still reads one per posting.
    assert db.overview_counts()["seen"] == 2

    # The bar reads the same number as the tile, by construction.
    db.start_progress(later, False)
    assert db.run_progress(later)["jobs_in_scope"] == 1


def test_marking_a_title_gated_job_applied_builds_its_row_from_jobs(tmp_path):
    """The case with no `matches` row at all: the free title gate threw it out, so it
    appears under Total Jobs Seen and nowhere else. Before `jobs` was the third source
    of identity this answered `unknown` and wrote nothing."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("j1")])
    db.record_gate_reasons(run_id, [("acme", "j1", "title_excluded")])

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "updated"

    row = next(r for r in db.load_applied() if r["job_id"] == "j1")
    assert row["status"] == "submitted"
    # The three columns `load_applied_matches` falls back on, none of them blanked.
    assert row["board_token"] == "acme"
    assert row["title"] == "Backend Engineer"
    assert row["absolute_url"] == "https://example.com/jobs/j1"
    # And it is out of the last section, which is what the user clicked the button for.
    assert db.load_unmatched_jobs(run_id, 50)[0]["job_id"] == "j1"  # still unmatched
    assert "j1" in {r["job_id"] for r in db.load_applied()}


def test_marking_applied_is_idempotent_and_refuses_a_job_it_cannot_find(tmp_path):
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "updated"
    assert db.mark_applied_by_hand("j1", "2026-07-09T09:00:00+00:00") == "updated"
    assert len([r for r in db.load_applied() if r["job_id"] == "j1"]) == 1

    # Nothing on record names this job, so nothing is invented for it.
    assert db.mark_applied_by_hand("nope", "2026-07-08T09:00:00+00:00") == "unknown"
    assert [r["job_id"] for r in db.load_applied()] == ["j1"]


def test_declining_a_job_clears_the_attempt_and_un_shortlists_it(tmp_path):
    """A decision not to apply is not an application, so it leaves no `applied` row.

    Any status other than 'submitted' renders under Needs Attention by design, so the
    row has to go rather than change — and the job has to stop being shortlisted, or
    the backlog hands it straight back to the applier.
    """
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)
    _attempt(db, "j1", status="excluded")

    assert db.decline_job("j1") == {"deleted": True, "unshortlisted": 1}

    assert db.applied_ids() == set()
    row = next(r for r in db.load_all_matches(run_id) if r["job_id"] == "j1")
    assert row["shortlisted"] in (0, False)
    assert row["skip_reason"] == DECLINED_BY_USER
    # It was judged, and that must survive: `skipped` is what blanks the score in the
    # results CSV and files a row among the never-scored ones.
    assert row["skipped"] in (0, False)
    assert row["relevance_score"] == 85
    assert db.load_pending_applications("1970-01-01T00:00:00+00:00") == []


def test_declining_a_shortlisted_job_with_no_attempt_still_retires_it(tmp_path):
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)

    assert db.decline_job("j1") == {"deleted": False, "unshortlisted": 1}
    # Idempotent: the second call finds nothing left to change.
    assert db.decline_job("j1") == {"deleted": False, "unshortlisted": 0}
    assert db.load_shortlisted(run_id) == []


# -- the one-time merge into `postings` ---------------------------------------
#
# These build a v1 file by hand — the four tables the merge folds together, in the
# shape they actually shipped in — and then open a `Database` on it, which is what
# runs the migration. Hand-built rather than fixtured from an older release because
# the schema no longer exists in the codebase to generate.

_V1_JOBS = (
    "CREATE TABLE jobs (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
    "board_token TEXT, source TEXT, title TEXT, location TEXT, url TEXT, "
    "updated_at TEXT, scraped_at TEXT, content_text TEXT, raw_json TEXT NOT NULL, "
    "gate_reason TEXT, PRIMARY KEY (run_id, job_id))"
)
_V1_MATCHES = (
    "CREATE TABLE matches (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
    "board_token TEXT, title TEXT, relevance_score INTEGER, encoder_score REAL, "
    "rerank_score_wide REAL, rerank_score REAL, yoe_required REAL, "
    "shortlisted INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, skip_reason TEXT, "
    "source_run_id TEXT, scored_at TEXT, raw_json TEXT NOT NULL, "
    "PRIMARY KEY (run_id, job_id))"
)
#: The shape before `from_backlog` was added, which is what `_ADDED_COLUMNS` exists
#: for and what the migration has to be able to read.
_V1_APPLIED_OLD = (
    "CREATE TABLE applied (job_id TEXT PRIMARY KEY, board_token TEXT, title TEXT, "
    "absolute_url TEXT, applied_at TEXT, status TEXT, dry_run INTEGER, "
    "screenshot TEXT, error TEXT)"
)
_V1_SEEN = "CREATE TABLE seen_jobs (job_id TEXT PRIMARY KEY, first_seen TEXT)"


def _v1_file(tmp_path, *, applied_ddl: str = _V1_APPLIED_OLD):
    """An empty v1 database, with the four tables the merge folds together."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    for ddl in (_V1_JOBS, _V1_MATCHES, applied_ddl, _V1_SEEN):
        conn.execute(ddl)
    conn.commit()
    return path, conn


def _v1_sighting(conn, run_id, job_id, *, token="acme", title="Backend Engineer",
                 url=None, content="We need a backend engineer.", gate_reason=None):
    url = url or f"https://example.com/jobs/{job_id}"
    raw = json.dumps({
        "source": "greenhouse", "board_token": token, "job_id": job_id,
        "title": title, "location": {"name": "Remote"}, "absolute_url": url,
        "updated_at": "2026-07-01T00:00:00+00:00",
        "scraped_at": "2026-07-01T00:00:00+00:00",
    })
    conn.execute(
        "INSERT INTO jobs (run_id, job_id, board_token, source, title, location, url,"
        " updated_at, scraped_at, content_text, raw_json, gate_reason)"
        " VALUES (?,?,?,?,?,'Remote',?,?,?,?,?,?)",
        (run_id, job_id, token, "greenhouse", title, url,
         "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00",
         content, raw, gate_reason),
    )
    conn.commit()


def test_the_merge_collapses_sightings_and_keeps_the_newest_payload(tmp_path):
    """The whole point of the upgrade, on the shape that made it necessary.

    Three sightings of one posting become one row. The payload comes from the newest,
    because newest is the one still true — but the description falls back to an older
    sighting, since a later *failed* hydration leaves `content_text` NULL and losing a
    description the install already had would be the worst outcome here. The gate's
    verdict survives whichever sighting recorded it, which is what keeps the overview's
    `Reason` column reading the same across the upgrade.
    """
    path, conn = _v1_file(tmp_path)
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j1", title="Old Title",
                 content="the description", gate_reason=None)
    _v1_sighting(conn, "2026-07-02T00-00-00Z", "j1", title="Mid Title",
                 content=None, gate_reason="title_excluded")
    _v1_sighting(conn, "2026-07-03T00-00-00Z", "j1", title="New Title",
                 url="https://example.com/new", content=None, gate_reason=None)
    conn.close()

    db = Database(path)

    rows = db._conn.execute("SELECT * FROM postings").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["board_token"], row["job_id"]) == ("acme", "j1")
    assert row["title"] == "New Title"                 # newest sighting's payload
    assert row["url"] == "https://example.com/new"
    assert row["content_text"] == "the description"    # recovered from an older one
    assert row["gate_reason"] == "title_excluded"      # whichever sweep recorded it
    assert row["first_run_id"] == "2026-07-01T00-00-00Z"
    assert row["last_run_id"] == "2026-07-03T00-00-00Z"

    # The old tables are gone, and the version says so, so the next open does nothing.
    names = {r["name"] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "matches", "applied", "seen_jobs"} & names == set()
    assert "postings_migrating" not in names
    version = db._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    assert version == "2"


def test_the_merge_carries_the_verdict_the_application_and_the_retirement(tmp_path):
    """Everything that was in another table has to arrive on the posting.

    The verdict is the *newest* `matches` row, which is the one-time execution of the
    `MAX(scored_at)` rule every lifetime read used to run. The application has no run
    to be filed under at all, and `from_backlog` reads False on a row written before
    that column existed — the fact was never recorded and must not be guessed.
    """
    path, conn = _v1_file(tmp_path)
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j1")
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j2")
    for run_id, score, scored_at, reason in (
        ("2026-07-01T00-00-00Z", 40, "2026-07-01T01:00:00+00:00", "llm_call_cap_reached"),
        ("2026-07-02T00-00-00Z", 88, "2026-07-02T01:00:00+00:00", None),
    ):
        conn.execute(
            "INSERT INTO matches (run_id, job_id, board_token, title,"
            " relevance_score, shortlisted, skipped, skip_reason, source_run_id,"
            " scored_at, raw_json) VALUES (?,?,'acme','Backend Engineer',?,1,0,?,?,?,?)",
            (run_id, "j1", score, reason, run_id, scored_at,
             json.dumps({"job_id": "j1", "relevance_score": score})),
        )
    conn.execute(
        "INSERT INTO applied (job_id, board_token, title, absolute_url, applied_at,"
        " status, dry_run, screenshot, error) VALUES"
        " ('j1','acme','Backend Engineer','https://example.com/jobs/j1',"
        "  '2026-07-03T09:00:00+00:00','submitted',0,'shot.png',NULL)")
    conn.execute("INSERT INTO seen_jobs (job_id, first_seen)"
                 " VALUES ('j2','2026-07-01T02:00:00+00:00')")
    conn.commit()
    conn.close()

    db = Database(path)

    row = db._conn.execute(
        "SELECT * FROM postings WHERE job_id='j1'").fetchone()
    assert (row["relevance_score"], row["skip_reason"]) == (88, None)
    assert row["scored_run_id"] == "2026-07-02T00-00-00Z"
    assert row["apply_status"] == "submitted"
    assert row["screenshot"] == "shot.png"
    assert row["from_backlog"] == 0

    # The retirement came across too, so the matcher will not re-judge j2.
    assert db.seen_ids() == {("acme", "j2")}
    # And the application reads as one, with its backlog origin honestly unknown.
    applied = db.load_applied_matches(None)
    assert [r["job_id"] for r in applied] == ["j1"]
    assert applied[0]["applied_from_backlog"] is False


def test_the_merge_retires_neither_side_of_a_cross_board_id(tmp_path):
    """`seen_jobs` had no board token, so an id two boards claim cannot be resolved.

    Retiring both could permanently discard a posting nothing ever judged; retiring
    neither re-judges one posting once, which costs a few LLM calls. The cheap mistake
    is the one to make.
    """
    # Different sweeps, because the v1 `jobs` key was `(run_id, job_id)` — two boards
    # sharing an id could not both hold a row in one run, which is half of why the
    # collision was invisible.
    path, conn = _v1_file(tmp_path)
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "12345", token="acme")
    _v1_sighting(conn, "2026-07-02T00-00-00Z", "12345", token="beta")
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j2", token="acme")
    conn.execute("INSERT INTO seen_jobs (job_id, first_seen)"
                 " VALUES ('12345','2026-07-01T02:00:00+00:00'),"
                 "        ('j2','2026-07-01T02:00:00+00:00')")
    conn.commit()
    conn.close()

    db = Database(path)

    # Two postings, because the composite key finally tells them apart.
    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM postings WHERE job_id='12345'"
    ).fetchone()["n"] == 2
    # Neither is retired; the unambiguous one is.
    assert db.seen_ids() == {("acme", "j2")}


def test_a_live_sweep_blocks_the_merge(tmp_path, monkeypatch):
    """An old-code sweeper is still running against this file, so it keeps the v1
    shape it understands and the user is told to stop it.

    Migrating underneath it would fail every statement it issues naming `jobs`,
    `matches` or `applied` — loudly, repeatedly, mid-sweep — and the merge holds the
    write lock for minutes against a 5,000 ms `busy_timeout` besides. Deferring
    silently is not an option either, because the new readers have no old SQL to fall
    back on, so the refusal has to reach the user.
    """
    from hireshire import process_liveness, sweep_pid

    path, conn = _v1_file(tmp_path)
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j1")
    conn.close()

    monkeypatch.setattr(sweep_pid, "read", lambda *a, **k: 4242)
    monkeypatch.setattr(process_liveness, "is_alive", lambda pid: True)

    with pytest.raises(SchemaMigrationBlocked) as caught:
        Database(path)
    assert "--stop" in str(caught.value)

    # Nothing was touched: the sweeper's file still reads exactly as it did.
    import sqlite3
    check = sqlite3.connect(str(path))
    names = {r[0] for r in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in names and "postings" not in names
    check.close()


def test_a_newer_schema_version_is_refused(tmp_path):
    """A downgrade is the one case where carrying on is worse than stopping: these
    queries would run without error against a shape they do not understand."""
    db = Database(tmp_path / "new.db")
    with db._lock, db._conn:
        db._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version','99')")
    db.close()

    with pytest.raises(SchemaMigrationBlocked) as caught:
        Database(tmp_path / "new.db")
    assert "newer version" in str(caught.value)


def test_the_merge_is_all_or_nothing(tmp_path):
    """An interrupted merge must leave a clean v1 file, and the next open must retry.

    `--stop` is `taskkill /F` and runs no `finally`, so "interrupted" is the normal
    case rather than the exotic one. The whole merge is one transaction — SQLite's DDL
    is transactional — which is also why `postings_migrating` can never be left behind
    and there is no cleanup path to get wrong.
    """
    import sqlite3

    path, conn = _v1_file(tmp_path)
    _v1_sighting(conn, "2026-07-01T00-00-00Z", "j1")
    conn.close()

    real = Database._run_migration_statements

    def boom(self):
        real(self)                      # do all the work, then fail before COMMIT
        raise RuntimeError("the power went out")

    Database._run_migration_statements = boom
    try:
        with pytest.raises(RuntimeError):
            Database(path)
    finally:
        Database._run_migration_statements = real

    check = sqlite3.connect(str(path))
    names = {r[0] for r in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in names, "the rollback lost the original table"
    assert "postings" not in names and "postings_migrating" not in names
    assert check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    check.close()

    # And a second open migrates cleanly.
    db = Database(path)
    assert db._conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"] == 1


def test_two_boards_sharing_a_job_id_are_two_postings(tmp_path):
    """The collision the composite key exists to fix, end to end.

    `job_id` is only unique per board: Greenhouse and BambooHR both mint bare
    integers, and Workday falls back to the job *title* when a posting has no req id
    and no `externalPath`. The damage was never in the old `jobs` table — its
    `(run_id, job_id)` key never separated two boards either — it was that
    `seen_jobs(job_id)` retired a posting **permanently** and `applied(job_id)`
    credited an application, both on the bare id. So one employer's verdict retired
    another employer's posting with no way back, and one application counted for two
    jobs.

    Both of those tables are columns on the posting now, so the fix is the key rather
    than a reminted id — which matters, because reminting would have had to rewrite
    `job_id` across every table, and an application that failed to remap means a
    duplicate application.
    """
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    db.insert_jobs(run_id, [_job("12345", token="acme"),
                            _job("12345", token="beta")])

    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM postings WHERE job_id='12345'"
    ).fetchone()["n"] == 2

    # Judged independently: acme's is shortlisted, beta's is cut by the cross-encoder.
    for token, score, shortlisted, reason in (("acme", 88, True, None),
                                              ("beta", 0, False, "rerank_below_cutoff")):
        assert db.upsert_match(
            run_id, "12345", token, "Backend Engineer", score, shortlisted,
            reason is not None, reason, run_id, "2026-07-07T01:00:00+00:00",
            json.dumps({"job_id": "12345", "board_token": token}),
        ) == 1
    scores = dict(db._conn.execute(
        "SELECT board_token, relevance_score FROM postings WHERE job_id='12345'"))
    assert scores == {"acme": 88, "beta": 0}

    # Retired independently: beta's verdict must not retire acme's posting.
    db.mark_seen([("beta", "12345")])
    assert db.seen_ids() == {("beta", "12345")}

    # Applied to independently, and the application credits exactly one of them.
    db.record_applied("12345", "acme", "Backend Engineer", "u",
                      "2026-07-08T09:00:00+00:00", "submitted", None, None)
    assert db.applied_ids() == {("acme", "12345")}
    statuses = dict(db._conn.execute(
        "SELECT board_token, apply_status FROM postings WHERE job_id='12345'"))
    assert statuses == {"acme": "submitted", "beta": None}
