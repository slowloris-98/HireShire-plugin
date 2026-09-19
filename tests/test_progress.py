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

import pytest

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


def _snapshot(tmp_path, run_id, in_progress: bool, **kw) -> dict:
    db = _db(tmp_path)
    return data.overview_snapshot(
        db, run_id, live=None if run_id else in_progress,
        run=_run(in_progress=in_progress, started_at=None, finished_at=None),
        progress=_progress(), **kw,
    )


@pytest.mark.parametrize("in_progress", [True, False])
def test_the_run_page_always_keeps_its_bars(tmp_path, in_progress):
    html = overview.build(_snapshot(tmp_path, RUN, in_progress), "2026-09-19_100000")
    for key in ("scraper", "matcher", "applier"):
        assert f'id="bar:{key}"' in html
    assert 'role="progressbar"' in html
    assert '<p class="prog-cap">' not in html, "the run page's eyebrow already names the sweep"


def test_the_lifetime_page_shows_a_live_sweep_and_names_it(tmp_path):
    html = overview.build(_snapshot(tmp_path, None, True, progress_label="2026-09-19_100000"))
    assert 'id="bar:scraper"' in html
    assert "Sweep 2026-09-19_100000 in progress" in html


def test_the_lifetime_page_drops_the_bars_once_the_sweep_ends(tmp_path):
    html = overview.build(_snapshot(tmp_path, None, False, progress_label="x"))
    assert 'class="prog"' not in html


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
