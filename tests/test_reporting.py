"""The reporting layer: the throttle, the placement, and the wiring into a run.

The pages themselves are pinned in `test_overview.py`. What is here is everything
around them — when a refresh happens, where the files land, and the rule that
losing a report must never take down the sweep it describes.

They exist because a run's reasoning had nowhere to go: four rationales per scored
job went into the `matches` table's `raw_json` and were never rendered anywhere a
user looks. A sweep that shortlisted nothing left them with a CSV of numbers.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

import orchestrate
from hireshire import reporting
from hireshire.reporting import data, overview


def snapshot(**over) -> dict:
    base = {
        "run_id": "2026-08-25T22-34-32Z",
        "in_progress": False,
        "matching_done": True,
        "started_at": "2026-08-25T22:34:32+00:00",
        "finished_at": "2026-08-25T23:41:36+00:00",
        "companies": 9641,
        "companies_with_jobs": 1008,
        "scrape_errors": 0,
        "jobs": 8504,
        "gated_out": 2016,
        "candidates": 6488,
        "over_budget": 6478,
        "duplicates": 0,
        "scored": 1,
        "shortlisted": 0,
        "top_score": 61,
        "by_reason": {"": 1, "rerank_below_top_k": 1},
        "threshold": 75,
        "top_k": 10,
        "model": "claude-sonnet-5",
        "usage": None,
    }
    base.update(over)
    return base


def scored_record(**over) -> dict:
    base = {
        "job_id": "direct:google:1",
        "board_token": "google",
        "title": "Software Engineer III, AI/ML",
        "location": "Mountain View, CA",
        "absolute_url": "https://example.com/j1",
        "relevance_score": 61,
        "encoder_score": 0.5133,
        "rerank_score_wide": 8.8849,
        "rerank_score": 7.2977,
        "rerank_stage": "refined",
        "cluster_representative": None,
        "cluster_size": 1,
        "years_experience_required": 2.0,
        "core_skills_score": 28,
        "core_skills_rationale": "Python is evidenced throughout the RAG projects.",
        "experience_score": 19,
        "experience_rationale": "Roughly 3.5 years, but none of it ML engineering.",
        "education_bonus_score": 14,
        "education_rationale": "BTech met; MS in progress.",
        "match_reasons": ["Hands-on LLM project experience"],
        "disqualifiers": ["No professional ML role"],
        "recommend": False,
        "skipped": False,
        "skip_reason": None,
        "shortlisted": False,
    }
    base.update(over)
    return base


def dropped_record(**over) -> dict:
    """A budget drop, exactly as `filtered_result` builds it — score 0, skipped."""
    base = {
        "job_id": "direct:google:2",
        "board_token": "google",
        "title": "Staff Software Engineer, Search",
        "location": "Seattle, WA",
        "absolute_url": "https://example.com/j2",
        "relevance_score": 0,
        "encoder_score": 0.41,
        "rerank_score_wide": 2.11,
        "rerank_score": None,
        "cluster_representative": None,
        "cluster_size": 1,
        "skipped": True,
        "skip_reason": "rerank_below_cutoff",
        "shortlisted": False,
    }
    base.update(over)
    return base


# --- what the sweep cost ------------------------------------------------------

USAGE = {"calls": 142, "input": 88210, "output": 31004,
         "cache_read": 412553, "cache_write": 4820, "cost_usd": 1.8734}


def _overview_snapshot(**over) -> dict:
    base = {
        "run_id": "2026-08-25T22-34-32Z",
        "counts": {"seen": 8504, "relevant": 2, "shortlisted": 0, "applied": 0},
        "applied": [], "applied_total": 0,
        "shortlisted": [], "shortlisted_total": 0,
        "filtered": [scored_record()], "filtered_total": 1,
        "seen": [], "seen_total": 0,
        "started_at": "2026-08-25T22:34:32+00:00",
        "finished_at": "2026-08-25T23:41:36+00:00",
        "usage": None,
        "live": False,
    }
    base.update(over)
    return base


def test_the_sweeps_cost_reaches_the_page():
    """Until this, a monitor sweep's cost survived only in
    logs/orchestration.log, because `quiet=True` suppresses the console summary."""
    html = overview.build(_overview_snapshot(usage=USAGE), "2026-08-25_153432")
    assert "$1.87" in html
    assert "Est. cost" in html


def test_a_run_that_was_never_measured_shows_no_cost_anywhere():
    """Runs made before the tally existed, and every backend but claude_code, have
    no figure. Printing $0.00 would claim the sweep was free."""
    html = overview.build(_overview_snapshot(), "2026-08-25_153432")
    assert "$0.00" not in html


def test_the_cost_display_is_one_switch(monkeypatch):
    """Every cost fragment hangs off render.SHOW_COST, so the feature comes out of
    the reports in one edit while the numbers stay in the database."""
    monkeypatch.setattr(overview, "SHOW_COST", False)
    html = overview.build(_overview_snapshot(usage=USAGE), "2026-08-25_153432")
    assert "Est. cost" not in html and "$1.87" not in html


# --- the one definition of "judged" -------------------------------------------


def test_a_cluster_sibling_counts_as_scored():
    """It inherited a verdict from its representative — judged once for the whole
    cluster, not never judged. Treating it as unscored would blank a real score,
    and `_never_scored` is the only place that distinction is made: the reports and
    the results CSV each have a copy, and they must agree."""
    sibling = dropped_record(
        job_id="direct:google:3", relevance_score=61, skipped=True,
        skip_reason="duplicate_of_cluster", cluster_representative="direct:google:1",
    )
    assert data._never_scored(sibling) is False
    assert data._never_scored(dropped_record()) is True
    assert data._never_scored(scored_record()) is False


def test_the_two_definitions_of_judged_agree():
    """`hireshire.results_export` carries its own copy, because the exporter must
    not import a reporting module. Two copies of one rule is the hazard; this is
    what holds them together."""
    from hireshire import results_export

    for record in (scored_record(), dropped_record(),
                   dropped_record(cluster_representative="direct:google:1")):
        assert data._never_scored(record) == results_export._never_scored(record)


# --- failure containment ------------------------------------------------------


def test_a_broken_report_never_takes_down_the_run(tmp_path, monkeypatch):
    """Losing a diagnostic must not fail a sweep whose CSV, JSON and database rows
    are already safe — the trade `write_results_csv` documents."""
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reporting.data, "run_snapshot", explode)
    reporting.refresh("run-1", tmp_path, "2026-08-25_153432", final=True)  # must not raise


def test_a_write_failure_is_reported_as_none_not_raised(tmp_path):
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    assert overview.write(_overview_snapshot(), blocked / "overview.html") is None


def test_the_throttle_lets_the_final_write_through(tmp_path, monkeypatch):
    """Throttled calls come off a callback that fires thousands of times; the final
    one comes from `_finalise_pipeline` and is the only call that sees a completed
    run, so it must never be skipped."""
    calls = []
    monkeypatch.setattr(reporting.data, "run_snapshot",
                        lambda db, run_id: calls.append(run_id) or snapshot())
    monkeypatch.setattr(reporting, "_last_refresh", 0.0)

    reporting.refresh("r", tmp_path, "s", final=True)
    reporting.refresh("r", tmp_path, "s")          # throttled out
    reporting.refresh("r", tmp_path, "s", final=True)
    assert len(calls) == 2


# --- placement ----------------------------------------------------------------


def test_reports_go_to_the_results_root_and_nowhere_else(tmp_path):
    """Reports are results: they belong beside the CSV, in the folder the user
    chose, and never in a directory resolved from the working directory — a plugin's
    cwd is whatever project the session was launched from.

    With no workspace configured, `results_root()` falls back inside DATA, which is
    also what a bare checkout and pytest see. That is the same fallback
    `test_no_mutable_state_is_written_into_the_install_dir` encodes, so the
    invariant asserted here is "under the results root", not "not under ROOT".
    """
    from hireshire import paths

    targets = reporting.report_paths(tmp_path, "2026-08-25_153432")
    root = paths.results_root()

    # Two pages, and only two. The dashboard and the matching report are gone.
    assert set(targets) == {"overview", "run_overview"}
    assert targets["overview"].parent == root
    # The stamped page goes with the run it describes, wherever that run dir is.
    assert targets["run_overview"].parent == tmp_path
    # And with no workspace set, the root itself stays inside DATA.
    assert root == paths.RESULTS_DIR and paths.DATA in root.parents


# --- wiring: a real finalise must produce the files ---------------------------


class _ReportDB:
    """Enough of `Database` for the reporting path, with no SQLite involved."""

    def __init__(self, all_rows):
        self._all_rows = all_rows
        self.finalised = []

    def load_pipeline_results(self, run_id):
        return []

    def load_all_matches(self, run_id):
        return self._all_rows

    def applied_ids(self):
        return set()

    def finalise_run(self, run_id, phase, started_at, ended_at, summary):
        self.finalised.append(phase)

    def scrape_counts(self, run_id):
        return {"companies": 9641, "companies_with_jobs": 1008, "errors": 0, "jobs": 8504}

    def match_counts(self, run_id):
        return {"rows_total": len(self._all_rows), "scored": 1, "shortlisted": 0,
                "top_score": 61, "by_reason": {"": 1, "rerank_below_cutoff": 1}}

    def run_phase_stats(self, run_id):
        # Both phases present — i.e. the sweep has finished, which is the state
        # `_finalise_pipeline` leaves behind.
        return {"match": {"threshold": 75, "model": "claude-sonnet-5"},
                "pipeline": {"started_at": "2026-08-25T22:34:32+00:00",
                             "finished_at": "2026-08-25T23:41:36+00:00"}}

    # --- the overview page's reads --------------------------------------------
    # These were missing once, and their absence was invisible: `overview_snapshot`
    # raised `AttributeError`, `refresh` swallowed it by design, and the overview was
    # simply never written while the assertions below said "both reports" and checked
    # the other two. A fake missing a method the real path calls is a hole in the
    # test, not a simplification.

    def overview_counts(self, run_id=None):
        return {"seen": 8504, "relevant": 2, "shortlisted": 0, "applied": 0}

    def load_applied_matches(self, run_id=None):
        return []

    def load_lifetime_matches(self, judged, limit):
        return [r for r in self._all_rows
                if bool(r.get("relevance_score") is not None
                        and not r.get("skipped")) is judged]

    def load_unmatched_jobs(self, run_id, limit):
        return [{"job_id": "direct:google:99", "board_token": "google",
                 "title": "Barista", "location": "Mountain View, CA",
                 "absolute_url": "https://example.com/j99"}]


def _finalise_with_reports(tmp_path, monkeypatch, stamp="2026-08-25_153432",
                           complete=True):
    """Drive the two halves the way `run_pipeline`'s `finally` does."""
    from hireshire import paths

    db = _ReportDB([scored_record(), dropped_record()])
    monkeypatch.setattr(paths, "LAST_RUN_PATH", tmp_path / "last_run.json")
    monkeypatch.setattr(paths, "results_root", lambda: tmp_path)
    monkeypatch.setattr(orchestrate, "get_db", lambda: db)
    monkeypatch.setattr(reporting, "get_db", lambda: db)

    results_dir = tmp_path / stamp
    results_dir.mkdir(exist_ok=True)

    async def drive():
        total = await orchestrate._write_run_outputs(
            "2026-08-25T22-34-32Z", results_dir, stamp, complete=complete
        )
        await orchestrate._finalise_pipeline(
            "2026-08-25T22-34-32Z", results_dir, "started", stamp, total,
            complete=complete,
        )

    asyncio.run(drive())
    return db, results_dir


def test_finalising_a_run_writes_both_pages_and_the_csv(tmp_path, monkeypatch):
    """`refresh` swallows every exception, so a page that stops being written fails
    nothing unless something asserts it exists."""
    _, results_dir = _finalise_with_reports(tmp_path, monkeypatch)

    assert (results_dir / overview.run_overview_name("2026-08-25_153432")).exists()
    assert (tmp_path / overview.OVERVIEW_NAME).exists()
    assert (results_dir / "2026-08-25_153432_results.csv").exists()
    # ...and nothing writes the two pages that were removed.
    assert not (tmp_path / "dashboard.html").exists()
    assert not (tmp_path / "latest_matching.html").exists()
    assert list(results_dir.glob("*_matching.html")) == []
    assert list(results_dir.glob("*_all_jobs.csv")) == []


def test_the_overview_survives_the_finalise_path(tmp_path, monkeypatch):
    """Not just written — written with its sections populated. `refresh` catching
    everything means a broken snapshot is indistinguishable from a quiet one, so this
    reads the page back."""
    _, results_dir = _finalise_with_reports(tmp_path, monkeypatch)
    html = (results_dir / overview.run_overview_name("2026-08-25_153432")).read_text(
        encoding="utf-8"
    )

    assert "Total Jobs Seen" in html
    assert "Jobs Filtered (yet to be scored or not picked)" in html
    # The title-gate job reached the page, which it can only do via the jobs table.
    assert "Barista" in html
    # And the judge's reasoning is on it, which is the only prose the page carries.
    assert "Python is evidenced throughout the RAG projects." in html


def test_the_pointer_file_names_the_pages_for_the_skills(tmp_path, monkeypatch):
    """The skills must never reconstruct these paths — the results root is a folder
    the user chose and can move."""
    _, results_dir = _finalise_with_reports(tmp_path, monkeypatch)
    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))

    assert pointer["overview_html"] == str(tmp_path / overview.OVERVIEW_NAME)
    assert pointer["run_overview_html"] == str(
        results_dir / overview.run_overview_name("2026-08-25_153432")
    )
    # The pointer /apply actually reads is untouched by any of this.
    assert pointer["json"] == str(results_dir / "2026-08-25_153432_results.json")


def test_a_finished_run_leaves_pages_that_stop_reloading(tmp_path, monkeypatch):
    """The final refresh runs after `finalise_run`, so the pipeline's run row exists
    and the pages know the sweep is over. Refreshing before it would leave a
    finished run reloading itself forever."""
    _, results_dir = _finalise_with_reports(tmp_path, monkeypatch)
    for page in (tmp_path / overview.OVERVIEW_NAME,
                 results_dir / overview.run_overview_name("2026-08-25_153432")):
        assert 'http-equiv="refresh"' not in page.read_text(encoding="utf-8")


def test_a_crashed_run_also_leaves_pages_that_stop_reloading(tmp_path, monkeypatch):
    """The same guarantee from the other direction, and the one that was missing.
    A sweep that died wrote no `runs` row at all, so both pages went on reloading
    themselves forever with their elapsed figure climbing on a dead process."""
    db, results_dir = _finalise_with_reports(tmp_path, monkeypatch, complete=False)

    assert db.finalised == ["pipeline"]
    for page in (tmp_path / overview.OVERVIEW_NAME,
                 results_dir / overview.run_overview_name("2026-08-25_153432")):
        assert 'http-equiv="refresh"' not in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("reason,expected", [
    ("rerank_below_top_k", "Over budget — lost the top-K race"),
    ("duplicate_of_cluster", "Duplicate requisition — verdict copied from its cluster"),
    ("", "Scored by the LLM"),
])
def test_known_skip_reasons_get_a_readable_label(reason, expected):
    assert data.reason_label(reason) == expected


def test_an_unknown_skip_reason_is_shown_verbatim_not_bucketed():
    """A reason nobody has labelled yet is exactly the one worth reading."""
    assert data.reason_label("some_new_failure") == "Some new failure"


# --- staying fresh while a sweep runs -----------------------------------------


def test_the_ticker_refreshes_with_no_scoring_events_at_all(monkeypatch):
    """The condition that froze a live run for forty-seven minutes.

    Report refreshes used to come off two pipeline callbacks, and both stop:
    `on_company_start` ends with the scrape, and `on_job_score` fires only on an LLM
    score, which `top_k` caps. On the sweep that prompted this the tenth and last
    score landed four minutes in, and nothing refreshed the reports again while the
    matcher recorded ~1,470 further rows. So the guarantee under test is precisely
    "fires when nothing is being scored".
    """
    monkeypatch.setattr(orchestrate, "_REPORT_TICK_S", 0.01)
    calls = []

    async def drive():
        ticker = asyncio.create_task(orchestrate._tick_reports(lambda: calls.append(1)))
        await asyncio.sleep(0.08)
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(drive())
    assert len(calls) >= 2, "a clock-driven refresh must not depend on funnel events"


def test_a_raising_scheduler_never_takes_down_the_sweep(monkeypatch):
    """Same trade `reporting.refresh` and `write_results_csv` document: a report is
    a diagnostic, and losing one must not cancel the run it describes."""
    monkeypatch.setattr(orchestrate, "_REPORT_TICK_S", 0.01)
    calls = []

    def explode():
        calls.append(1)
        raise RuntimeError("boom")

    async def drive():
        ticker = asyncio.create_task(orchestrate._tick_reports(explode))
        await asyncio.sleep(0.05)
        alive = not ticker.done()
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        return alive

    assert asyncio.run(drive()) is True
    assert len(calls) >= 2, "it should keep ticking after a failure, not stop at one"


def test_stopping_the_ticker_waits_for_a_rebuild_already_running(monkeypatch):
    """Why cancelling the task is not enough on its own.

    The ticker dispatches to an executor thread, so a rebuild fired a moment before
    the stop can still be mid-write. If it landed after the `final=True` write it
    would re-arm the pages' meta refresh and leave a finished run reloading itself
    forever — the failure `test_a_finished_run_leaves_pages_that_stop_reloading`
    pins from the other side.
    """
    monkeypatch.setattr(orchestrate, "_REPORT_TICK_S", 0.01)
    busy = threading.Lock()
    released = []

    async def drive():
        ticker = asyncio.create_task(orchestrate._tick_reports(lambda: None))
        await asyncio.sleep(0.03)
        busy.acquire()                      # stand in for a rebuild in flight

        def finish_rebuild():
            time.sleep(0.05)
            released.append(True)
            busy.release()

        threading.Thread(target=finish_rebuild, daemon=True).start()
        await orchestrate._stop_report_ticker(ticker, busy)
        return ticker.done()

    assert asyncio.run(drive()) is True, "the ticker must be stopped, not just asked"
    assert released == [True], "stop returned while a rebuild was still writing"
    assert not busy.locked(), "the drain must leave the lock free"


def _silent_pipeline(monkeypatch, tmp_path, calls):
    """A `run_pipeline` whose scraper and matcher emit no events at all."""
    monkeypatch.setattr(orchestrate, "_REPORT_TICK_S", 0.01)
    monkeypatch.setattr(orchestrate.paths, "make_run_dir", lambda stamp: tmp_path)
    monkeypatch.setattr(
        reporting, "refresh",
        lambda run_id, results_dir, stamp, final=False: calls.append(final),
    )

    async def silent_scraper(*a, out_queue=None, **k):
        await asyncio.sleep(0.06)          # a phase long enough to need refreshing
        if out_queue is not None:
            await out_queue.put(None)

    async def silent_matcher(*a, in_queue=None, out_queue=None, **k):
        while in_queue is not None and await in_queue.get() is not None:
            pass
        await asyncio.sleep(0.06)          # ...and one that never scores a job
        if out_queue is not None:
            await out_queue.put(None)

    async def drain(in_q, out_q):
        while await in_q.get() is not None:
            pass
        await out_q.put(None)

    async def sink(q, *a, **k):
        while await q.get() is not None:
            pass

    monkeypatch.setattr(orchestrate.scraper, "main", silent_scraper)
    monkeypatch.setattr(orchestrate.matcher, "main", silent_matcher)
    monkeypatch.setattr(orchestrate, "_collect_results", drain)
    monkeypatch.setattr(orchestrate, "_track_results", sink)


def test_run_pipeline_keeps_refreshing_through_a_phase_that_scores_nothing(
    tmp_path, monkeypatch
):
    """The wiring, not the helper — the two are separately breakable.

    `_tick_reports` can be perfect and the sweep still freeze if nothing starts it,
    which is exactly the shape of the original bug: the machinery to refresh existed
    and was simply never driven once the funnel stopped producing scores. So this
    drives the real `run_pipeline` with a scraper and matcher that emit no events at
    all, and asserts the reports moved anyway.

    It also pins the ordering `_stop_report_ticker` exists for: nothing may reach
    `reporting.refresh` after the finalise has begun, because that call is the one
    that renders the run as finished.
    """
    calls = []
    _silent_pipeline(monkeypatch, tmp_path, calls)

    finalised = {}

    async def fake_outputs(run_id, results_dir, stamp, complete=True):
        finalised["refreshes_before"] = len(calls)
        finalised["complete"] = complete
        return 0

    async def fake_finalise(*a, **kw):
        pass

    monkeypatch.setattr(orchestrate, "_write_run_outputs", fake_outputs)
    monkeypatch.setattr(orchestrate, "_finalise_pipeline", fake_finalise)

    assert asyncio.run(orchestrate.run_pipeline(quiet=True)) is not None

    assert finalised["complete"] is True
    assert finalised["refreshes_before"] >= 2, (
        "the reports must keep rebuilding through a phase that emits no scores"
    )
    assert len(calls) == finalised["refreshes_before"], (
        "a rebuild landed after the finalise began; it would re-arm the pages' "
        "meta refresh on a finished run"
    )


def test_a_failing_sweep_still_writes_its_outputs_and_marks_them_partial(
    tmp_path, monkeypatch
):
    """The reason the outputs live in a `finally`.

    Before this, a sweep that raised part-way left no CSV, no JSON, a `last_run.json`
    still naming the previous run, and no `runs` row — so fifteen minutes of real
    scoring was reachable only by opening the database by hand, and both pages went
    on reloading themselves forever.
    """
    calls = []
    _silent_pipeline(monkeypatch, tmp_path, calls)

    async def exploding_matcher(*a, in_queue=None, out_queue=None, **k):
        raise RuntimeError("the boards went away")

    monkeypatch.setattr(orchestrate.matcher, "main", exploding_matcher)

    seen = {}

    async def fake_outputs(run_id, results_dir, stamp, complete=True):
        seen["outputs"] = complete
        return 0

    async def fake_finalise(run_id, results_dir, started_at, stamp,
                            total_results=0, complete=True):
        seen["finalise"] = complete

    monkeypatch.setattr(orchestrate, "_write_run_outputs", fake_outputs)
    monkeypatch.setattr(orchestrate, "_finalise_pipeline", fake_finalise)

    # The run is reported as failed...
    assert asyncio.run(orchestrate.run_pipeline(quiet=True)) is None
    # ...and both halves ran anyway, told that it did not finish.
    assert seen == {"outputs": False, "finalise": False}


def test_a_failure_writing_the_outputs_does_not_mask_the_real_one(
    tmp_path, monkeypatch, caplog
):
    """A `finally` that raises replaces the traceback of the failure it was meant to
    survive with its own."""
    calls = []
    _silent_pipeline(monkeypatch, tmp_path, calls)

    async def exploding_matcher(*a, in_queue=None, out_queue=None, **k):
        raise RuntimeError("the boards went away")

    async def exploding_outputs(*a, **kw):
        raise OSError("and the disk is full")

    monkeypatch.setattr(orchestrate.matcher, "main", exploding_matcher)
    monkeypatch.setattr(orchestrate, "_write_run_outputs", exploding_outputs)

    with caplog.at_level("ERROR"):
        assert asyncio.run(orchestrate.run_pipeline(quiet=True)) is None

    assert "the boards went away" in caplog.text, "the original failure must survive"
    assert "Could not write the run's outputs" in caplog.text
