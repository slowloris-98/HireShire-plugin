"""The overview page's three stage bars: scraper, matcher, applier.

Each bar is a counter rather than a derivation from the tables the rest of the page
reads, because none of those tables can give the number: the scraper's total lives
only in memory, a not-found slug writes no `run_companies` row, title-gate rejections
write no `matches` row, and an excluded application writes no `applied` row. What is
pinned here is that the counters round-trip, that the bar maths never divides by zero
or overruns, and that each page shows the bars exactly when it should.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hireshire.models.job import Job, Location
from hireshire.reporting import data, overview
from hireshire.storage.db import Database

RUN = "2026-09-19T10-00-00Z"


def _db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def _match(db: Database, job_id: str, *, shortlisted=True, rep=None) -> None:
    raw = {"job_id": job_id, "board_token": "acme", "title": "Engineer",
           "relevance_score": 80, "cluster_representative": rep}
    db.upsert_match(RUN, job_id, "acme", "Engineer", 80, shortlisted, rep is not None,
                    "duplicate_of_cluster" if rep else None, RUN,
                    "2026-09-19T10:05:00+00:00", json.dumps(raw, separators=(",", ":")))


def _progress(**over) -> dict:
    base = {"companies_total": 200, "companies_done": 50, "jobs_processed": 30,
            "jobs_in_scope": 120, "apply_enabled": 1, "apply_queued": 10,
            "apply_handled": 7, "submitted": 4, "attention": 1}
    base.update(over)
    return base


def _run(**over) -> dict:
    base = {"in_progress": True, "scrape_done": False, "matching_done": False,
            "jobs": 120, "scrape_errors": 0, "scored": 9, "shortlisted": 10}
    base.update(over)
    return base


def _bars(progress=None, run=None) -> dict[str, dict]:
    return {b["key"]: b for b in data.progress_bars(
        _progress() if progress is None else progress, run or _run())}


# --- the database ---------------------------------------------------------------


def test_counters_round_trip(tmp_path):
    db = _db(tmp_path)
    db.start_progress(RUN, apply_enabled=True)
    db.set_progress(RUN, companies_total=40)
    for _ in range(3):
        db.bump_progress(RUN, companies_done=1)
    db.bump_progress(RUN, jobs_processed=12, apply_queued=2)
    db.bump_progress(RUN, apply_handled=1)

    got = db.run_progress(RUN)
    assert got["companies_total"] == 40
    assert got["companies_done"] == 3
    assert got["jobs_processed"] == 12
    assert got["apply_enabled"] == 1
    assert (got["apply_queued"], got["apply_handled"]) == (2, 1)


def test_starting_twice_does_not_reset_the_counters(tmp_path):
    db = _db(tmp_path)
    db.start_progress(RUN, apply_enabled=False)
    db.bump_progress(RUN, companies_done=5)
    db.start_progress(RUN, apply_enabled=False)
    assert db.run_progress(RUN)["companies_done"] == 5


def test_a_misspelt_column_is_a_bug_not_a_silent_no_op(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        db.bump_progress(RUN, companies_dne=1)


def test_a_run_with_no_row_has_no_progress(tmp_path):
    """A standalone phase run, or a run made before the bars existed. Writes to it
    are no-ops rather than creating a half-filled row."""
    db = _db(tmp_path)
    db.bump_progress(RUN, companies_done=1)
    assert db.run_progress(RUN) is None


def test_the_applier_split_counts_representatives_only(tmp_path):
    """Siblings never reach the worker. Counting their applications would let the
    submitted segment outgrow the handled total."""
    db = _db(tmp_path)
    db.start_progress(RUN, apply_enabled=True)
    _match(db, "rep")
    _match(db, "sib", rep="rep")
    _match(db, "bad")
    _match(db, "unlisted", shortlisted=False)
    for job_id, status in (("rep", "submitted"), ("sib", "submitted"),
                           ("bad", "error"), ("unlisted", "submitted")):
        db.record_applied(job_id, "acme", "Engineer", "", "2026-09-19T11:00:00+00:00",
                          status, None, None)
    db.insert_jobs(RUN, [])

    got = db.run_progress(RUN)
    assert (got["submitted"], got["attention"]) == (1, 1)
    assert got["jobs_in_scope"] == 0


def test_pruning_a_run_removes_its_progress(tmp_path):
    db = _db(tmp_path)
    db.finalise_run(RUN, "pipeline", "2026-09-19T10:00:00+00:00")
    db.start_progress(RUN, apply_enabled=False)
    db.prune_runs(keep=0)
    assert db.run_progress(RUN) is None


# --- the bar maths ----------------------------------------------------------------


def test_a_run_without_progress_has_no_bars():
    assert data.progress_bars(None, _run()) == []


def test_a_mid_sweep_run_has_three_running_bars():
    bars = _bars()
    assert [bars[k]["state"] for k in ("scraper", "matcher", "applier")] == [
        "running", "running", "running"]
    assert bars["scraper"]["pct"] == pytest.approx(25.0)
    assert bars["matcher"]["pct"] == pytest.approx(25.0)
    assert bars["applier"]["pct"] == pytest.approx(70.0)


def test_nothing_divides_by_zero():
    bars = _bars(_progress(companies_total=None, companies_done=0, jobs_in_scope=0,
                           jobs_processed=0, apply_queued=0, apply_handled=0,
                           submitted=0, attention=0))
    for bar in bars.values():
        assert bar["pct"] == 0.0
        assert bar["state"] == "waiting"
    assert bars["matcher"]["note"] == "Waiting for the scraper"
    assert bars["applier"]["note"] == "Waiting for shortlisted jobs"


def test_a_count_past_its_total_is_clamped():
    bars = _bars(_progress(companies_done=260, jobs_processed=500, apply_handled=99))
    assert {b["pct"] for b in bars.values()} == {100.0}
    assert bars["scraper"]["done"] == 200


def test_the_applier_segments_add_up_to_its_fill():
    bar = _bars()["applier"]
    kinds = dict(bar["segments"])
    assert kinds["ok"] == pytest.approx(40.0)
    assert kinds["warn"] == pytest.approx(10.0)
    assert kinds["skip"] == pytest.approx(20.0)
    assert sum(kinds.values()) == pytest.approx(bar["pct"])
    assert bar["note"] == "4 applied · 1 need attention · 2 skipped"


def test_the_applier_switched_off_says_so():
    bar = _bars(_progress(apply_enabled=0))["applier"]
    assert bar["state"] == "off"
    assert bar["note"] == "Auto-apply is off"


def test_every_bar_is_done_once_the_sweep_is():
    """Even a partial fill: a sweep that died part-way keeps the bar where it
    stopped, and that shortfall is the thing worth showing."""
    bars = _bars(run=_run(in_progress=False))
    assert {b["state"] for b in bars.values()} == {"done"}
    assert bars["scraper"]["pct"] == pytest.approx(25.0)


def test_the_matcher_is_not_done_before_the_scrape():
    bars = _bars(_progress(jobs_processed=120), _run(scrape_done=False))
    assert bars["matcher"]["state"] == "running"
    assert bars["matcher"]["pct"] == 100.0


# --- the pages ------------------------------------------------------------------


def _snapshot(tmp_path, run_id, in_progress: bool, progress=None) -> dict:
    db = _db(tmp_path)
    return data.overview_snapshot(
        db, run_id, live=None if run_id else in_progress,
        run=_run(in_progress=in_progress, started_at=None, finished_at=None),
        progress=progress if progress is not None else _progress(),
    )


@pytest.mark.parametrize("in_progress", [True, False])
def test_the_run_page_always_keeps_its_bars(tmp_path, in_progress):
    html = overview.build(_snapshot(tmp_path, RUN, in_progress), "2026-09-19_100000")
    for key in ("scraper", "matcher", "applier"):
        assert f'id="bar:{key}"' in html
    assert 'role="progressbar"' in html


def _lifetime(**over) -> dict:
    base = {"sweeps": 3, "companies_total": 600, "companies_done": 540,
            "jobs_processed": 300, "jobs_in_scope": 400, "unique_jobs": 1234,
            "shortlisted": 20,
            "submitted": 8, "attention": 2}
    base.update(over)
    return base


@pytest.mark.parametrize("in_progress", [True, False])
def test_the_lifetime_page_always_keeps_its_bars(tmp_path, in_progress):
    """Lifetime totals describe the install, not a sweep, so they stay on the page
    between sweeps — the file on disk is whatever the last sweep's final write left."""
    html = overview.build(_snapshot(tmp_path, None, in_progress, progress=_lifetime()))
    for key in ("scraper", "matcher", "applier"):
        assert f'id="bar:{key}"' in html
    assert "across 3 sweeps" in html


def _scraper_count(html: str) -> str:
    at = html.index('id="bar:scraper"')
    start = html.index('<span class="bar-n">', at) + len('<span class="bar-n">')
    return html[start:html.index("</span>", start)]


def test_the_lifetime_scraper_prints_unique_jobs_but_fills_by_companies(tmp_path):
    """A company count summed across sweeps means nothing to a user; unique jobs
    does. It has no total, so the fill still tracks companies checked."""
    html = overview.build(_snapshot(tmp_path, None, False, progress=_lifetime()))
    assert _scraper_count(html) == "1,234 unique jobs"
    bar = data.lifetime_progress_bars(_lifetime(), False)[0]
    assert bar["pct"] == pytest.approx(90.0)


def test_the_run_page_scraper_still_counts_companies(tmp_path):
    html = overview.build(_snapshot(tmp_path, RUN, True), "2026-09-19_100000")
    assert _scraper_count(html) == "50 / 200 companies"


def test_lifetime_scraper_and_matcher_run_only_while_a_sweep_does():
    live = {b["key"]: b["state"] for b in data.lifetime_progress_bars(_lifetime(), True)}
    idle = {b["key"]: b["state"] for b in data.lifetime_progress_bars(_lifetime(), False)}
    assert (live["scraper"], live["matcher"]) == ("running", "running")
    assert (idle["scraper"], idle["matcher"]) == ("done", "done")
    assert live["applier"] == idle["applier"] == "done"


def test_the_lifetime_applier_is_the_backlog():
    bar = data.lifetime_progress_bars(_lifetime(), False)[2]
    assert (bar["done"], bar["total"]) == (10, 20)
    assert dict(bar["segments"]) == {"ok": 40.0, "warn": 10.0}
    assert bar["note"] == "8 applied · 2 need attention · 10 not yet applied"


def test_a_fresh_install_has_no_lifetime_bars():
    empty = _lifetime(sweeps=0, companies_total=0, companies_done=0, jobs_processed=0,
                      jobs_in_scope=0, shortlisted=0, submitted=0, attention=0)
    assert data.lifetime_progress_bars(empty, False) == []
    assert data.lifetime_progress_bars(None, False) == []


def test_a_shortlist_from_before_tracking_still_shows_the_applier():
    bars = data.lifetime_progress_bars(
        _lifetime(sweeps=0, companies_total=0, companies_done=0, jobs_processed=0,
                  jobs_in_scope=0), False)
    states = {b["key"]: b["state"] for b in bars}
    assert states == {"scraper": "waiting", "matcher": "waiting", "applier": "done"}
    assert bars[0]["note"] == "No sweeps tracked yet"


# --- the lifetime read ------------------------------------------------------------


def _job(job_id: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(source="greenhouse", board_token="acme", job_id=job_id,
               title="Engineer", location=Location(name="Remote"),
               absolute_url=f"https://example.com/{job_id}",  # type: ignore[arg-type]
               updated_at=now, scraped_at=now, content_text="text")


def test_lifetime_progress_sums_tracked_sweeps_only(tmp_path):
    db = _db(tmp_path)
    for run, total, jobs in ((RUN, 100, 3), ("2026-09-20T10-00-00Z", 50, 2)):
        db.start_progress(run, apply_enabled=False)
        db.set_progress(run, companies_total=total)
        db.bump_progress(run, companies_done=total, jobs_processed=jobs)
        db.insert_jobs(run, [_job(f"{run}-{n}") for n in range(jobs)])
    # A sweep from before tracking: its jobs must not hold the matcher bar short.
    db.insert_jobs("2026-09-01T00-00-00Z", [_job("old-1"), _job("old-2")])

    lp = db.lifetime_progress()
    assert lp["sweeps"] == 2
    assert (lp["companies_done"], lp["companies_total"]) == (150, 150)
    assert (lp["jobs_processed"], lp["jobs_in_scope"]) == (5, 5)
    # Unique jobs is not scoped to tracked sweeps: it is what the install has seen.
    assert lp["unique_jobs"] == 7


def test_unique_jobs_counts_a_resurfaced_posting_once(tmp_path):
    db = _db(tmp_path)
    db.insert_jobs(RUN, [_job("same"), _job("other")])
    db.insert_jobs("2026-09-20T10-00-00Z", [_job("same")])
    assert db.lifetime_progress()["unique_jobs"] == 2


def test_the_lifetime_backlog_counts_each_representative_once(tmp_path):
    """A job shortlisted in two sweeps is one job to apply to, a sibling is none,
    and a shortlist from before tracking still counts."""
    db = _db(tmp_path)
    _match(db, "rep")
    _match(db, "sib", rep="rep")
    _match(db, "bad")
    _match(db, "unlisted", shortlisted=False)
    db.upsert_match("2026-09-20T10-00-00Z", "rep", "acme", "Engineer", 80, True,
                    False, None, "x", "2026-09-20T10:05:00+00:00",
                    json.dumps({"job_id": "rep", "cluster_representative": None},
                               separators=(",", ":")))
    for job_id, status in (("rep", "submitted"), ("sib", "submitted"), ("bad", "error")):
        db.record_applied(job_id, "acme", "Engineer", "", "2026-09-19T11:00:00+00:00",
                          status, None, None)

    lp = db.lifetime_progress()
    assert lp["sweeps"] == 0
    assert lp["shortlisted"] == 2                 # rep and bad
    assert (lp["submitted"], lp["attention"]) == (1, 1)


def test_a_waiting_bar_prints_a_dash_not_zero_of_zero(tmp_path):
    bar = data.progress_bars(_progress(apply_queued=0, apply_handled=0),
                             _run())[2]
    html = overview._bar(bar)
    assert "0 / 0" not in html
    assert '<span class="bar-n">—</span>' in html


def test_the_applier_switched_off_draws_no_track(tmp_path):
    bar = data.progress_bars(_progress(apply_enabled=0), _run())[2]
    html = overview._bar(bar)
    assert "bar-track" not in html
    assert "Auto-apply is off" in html
