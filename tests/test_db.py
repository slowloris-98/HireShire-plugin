"""Unit tests for the shared SQLite storage layer (hireshire.storage.db)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from hireshire.models.job import Job, Location
from hireshire.storage.db import DECLINED_BY_USER, PHASE_SCRAPE, Database


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


# -- outcomes the user records by hand ---------------------------------------


def _shortlisted(db: Database, job_id: str, run_id: str, *, score: int = 85,
                 url: str = "https://example.com/j") -> None:
    """One shortlisted, judged match row — what the applier is handed.

    `raw_json` carries the score and `skipped` because `load_all_matches` reads the
    record out of the blob and only overrides a few columns, so a minimal blob would
    hide exactly the keys these tests are about.
    """
    raw = json.dumps({
        "job_id": job_id, "absolute_url": url, "board_token": "acme",
        "title": "Backend Engineer", "relevance_score": score, "skipped": False,
        "skip_reason": None,
    })
    db.upsert_match(run_id, job_id, "acme", "Backend Engineer", score, True, False,
                    None, run_id, "2026-07-07T00:00:00+00:00", raw)


def _attempt(db: Database, job_id: str, status: str = "error") -> None:
    """An application that stopped short — a Needs Attention row."""
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


def test_an_older_database_gains_the_backlog_column_and_reads_false(tmp_path):
    """`_ADDED_COLUMNS`, not `_SCHEMA`: `CREATE TABLE IF NOT EXISTS` is a no-op on a
    file that already has the table, so a column added to the schema alone never
    reaches an existing install and the next INSERT fails with "no such column".

    False is the right reading for a row written before the column existed. The fact
    was never recorded and cannot be reconstructed, so the page shows such rows as
    ordinary applications rather than guessing."""
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE applied (job_id TEXT PRIMARY KEY, board_token TEXT, "
            "title TEXT, absolute_url TEXT, applied_at TEXT, status TEXT, "
            "dry_run INTEGER, screenshot TEXT, error TEXT)")
        conn.execute("INSERT INTO applied VALUES ('j1', 'acme', 'Backend Engineer', "
                     "'https://example.com/j', '2026-07-08T09:00:00+00:00', "
                     "'submitted', 0, NULL, NULL)")

    db = Database(path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id)
    assert db.load_applied_matches(run_id)[0]["applied_from_backlog"] is False

    # And the widened writer works against the migrated file.
    db.record_applied("j1", "acme", "Backend Engineer", "https://example.com/j",
                      "2026-07-09T09:00:00+00:00", "submitted", None, None,
                      from_backlog=True)
    assert db.load_applied_matches(run_id)[0]["applied_from_backlog"] is True


def test_marking_an_attempt_applied_promotes_the_row_it_already_has(tmp_path):
    """The Needs Attention case: the columns a pruned install depends on survive.

    `record_applied` is INSERT OR REPLACE, so re-recording through it would blank
    board_token, title and absolute_url — which are exactly what
    `load_applied_matches` falls back on once the job's `matches` rows are gone.
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
    assert (row["board_token"], row["title"]) == ("acme", "Backend Engineer")
    assert row["absolute_url"] == "https://example.com/j"


def test_marking_a_shortlisted_job_applied_builds_its_row_from_the_match(tmp_path):
    """The Shortlisted case: no attempt exists, so identity comes from `matches`."""
    db = _db(tmp_path)
    run_id = "2026-07-07T00-00-00Z"
    _shortlisted(db, "j1", run_id, url="https://example.com/jobs/j1")

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "inserted"

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
    db.record_gate_reasons(run_id, [("j1", "title_excluded"),
                                    ("j2", "title_low_relevance")])

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
    db.record_gate_reasons("2026-07-07T00-00-00Z", [("j1", "title_excluded")])
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
    db.record_gate_reasons(first, [("j1", "title_excluded")])
    db.insert_jobs(later, [_job("j1"), _job("j2")])
    db.record_gate_reasons(later, [("j2", "title_low_relevance")])

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
    db.record_gate_reasons(morning, [("j1", "title_excluded")])
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
    db.record_gate_reasons(next_sweep, [("j1", "title_excluded")])

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
    db.record_gate_reasons(first, [("j1", "title_excluded")])
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
    db.record_gate_reasons(run_id, [("j1", "title_excluded")])

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "inserted"

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

    assert db.mark_applied_by_hand("j1", "2026-07-08T09:00:00+00:00") == "inserted"
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
