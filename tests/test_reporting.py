"""The two HTML reports the engine writes on every sweep.

They exist because a run's reasoning had nowhere to go: four rationales per scored
job went into the `matches` table's `raw_json` and were never rendered anywhere a
user looks. A sweep that shortlisted nothing left them with a CSV of numbers.

Most of what is checked here is not layout — it is the handful of rules that make
the difference between a report and a misleading report.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

import orchestrate
from hireshire import reporting
from hireshire.reporting import dashboard, data, matching, overview


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
        "skip_reason": "rerank_below_top_k",
        "shortlisted": False,
    }
    base.update(over)
    return base


# --- the envelope rules -------------------------------------------------------


def test_the_matching_report_brings_no_document_skeleton():
    """The Artifact tool wraps what it publishes in its own doctype/head/body. A
    page that supplies those ends up nested inside a second copy of them."""
    html = matching.build(snapshot(), [scored_record()], "2026-08-25_153432").lower()
    for tag in ("<!doctype", "<html", "<head>", "<body"):
        assert tag not in html, f"matching report must not emit {tag}"


def test_the_matching_report_names_itself_for_the_rolling_url(tmp_path):
    """The skills find this artifact again by title so each sweep republishes to one
    URL. A title carrying the run date would create a new artifact every sweep."""
    html = matching.build(snapshot(), [scored_record()], "2026-08-25_153432")
    assert f"<title>{matching.TITLE}</title>" in html
    other = matching.build(snapshot(), [scored_record()], "2026-09-01_090000")
    assert f"<title>{matching.TITLE}</title>" in other


def test_the_dashboard_is_a_complete_local_document():
    html = dashboard.build({"totals": {"runs": 0, "jobs": 0, "candidates": 0,
                                       "scored": 0, "shortlisted": 0},
                            "applied": {"total": 0, "submitted": 0,
                                        "errors": 0, "skipped": 0, "recent": []},
                            "runs": [], "live": None}, tmp_root := __import__("pathlib").Path("/tmp/r"))
    assert html.lower().startswith("<!doctype html>")
    assert "<body>" in html
    assert str(tmp_root) in html


def test_the_dashboard_reloads_only_while_a_sweep_is_running():
    """A finished run that kept reloading would look like one that never ended."""
    base = {"totals": {"runs": 1, "jobs": 10, "candidates": 5, "scored": 1, "shortlisted": 0},
            "applied": {"total": 0, "submitted": 0, "errors": 0, "skipped": 0, "recent": []},
            "runs": [snapshot()], "live": None}
    idle = dashboard.build(base, __import__("pathlib").Path("/tmp/r"))
    assert "http-equiv=\"refresh\"" not in idle

    live_snap = snapshot(in_progress=True)
    running = dashboard.build({**base, "runs": [live_snap], "live": live_snap},
                              __import__("pathlib").Path("/tmp/r"))
    assert "http-equiv=\"refresh\"" in running


# --- what the sweep cost ------------------------------------------------------

USAGE = {"calls": 142, "input": 88210, "output": 31004,
         "cache_read": 412553, "cache_write": 4820, "cost_usd": 1.8734}


def test_the_sweeps_cost_reaches_the_matching_report():
    """Until this, a monitor sweep's cost survived only in logs/orchestration.log,
    because `quiet=True` suppresses the console summary."""
    html = matching.build(snapshot(usage=USAGE), [scored_record()], "2026-08-25_153432")

    assert "$1.87" in html
    assert "Est. cost" in html
    # The cache-read figure is the one that reveals a prompt cache that has stopped
    # working — a zero there means the run re-read the resume once per job.
    assert "412,553" in html
    assert "not a bill" in html


def test_a_run_that_was_never_measured_shows_no_cost_anywhere():
    """Runs made before the tally existed, and every backend but claude_code, have
    no figure. Printing $0.00 would claim the sweep was free."""
    html = matching.build(snapshot(), [scored_record()], "2026-08-25_153432")

    assert "Est. cost" not in html
    assert "$0.00" not in html


def test_the_cost_display_is_one_switch(monkeypatch):
    """Every cost fragment on both pages hangs off render.SHOW_COST, so the feature
    comes out of the reports in one edit while the numbers stay in the database."""
    monkeypatch.setattr(matching, "SHOW_COST", False)
    html = matching.build(snapshot(usage=USAGE), [scored_record()], "2026-08-25_153432")
    assert "Est. cost" not in html and "$1.87" not in html

    monkeypatch.setattr(dashboard, "SHOW_COST", False)
    board = dashboard.build(
        {"totals": {"runs": 1, "jobs": 10, "candidates": 5, "scored": 1,
                    "shortlisted": 0, "cost_usd": 1.8734},
         "applied": {"total": 0, "submitted": 0, "errors": 0, "skipped": 0, "recent": []},
         "runs": [snapshot(usage=USAGE)], "live": None},
        __import__("pathlib").Path("/tmp/r"),
    )
    assert "Est. cost" not in board and "$1.87" not in board


def test_the_dashboard_shows_cost_per_sweep_and_for_the_install():
    """Two different questions — what did that sweep cost, and what has this install
    spent — so the row keeps its own figure and the tile totals the measured ones."""
    board = dashboard.build(
        {"totals": {"runs": 2, "jobs": 10, "candidates": 5, "scored": 1,
                    "shortlisted": 0, "cost_usd": 2.5},
         "applied": {"total": 0, "submitted": 0, "errors": 0, "skipped": 0, "recent": []},
         # One measured sweep and one from before the tally existed.
         "runs": [snapshot(usage=USAGE), snapshot()], "live": None},
        __import__("pathlib").Path("/tmp/r"),
    )
    header = board.split("<thead><tr>")[1].split("</tr>")[0]

    assert header.count("<th>") == 9, "the cost column adds one header cell"
    assert "$2.50" in board, "lifetime tile"
    assert "$1.87" in board, "the measured sweep's own row"
    # ...and the unmeasured sweep gets an em dash in that column, not a zero.
    assert "$0.00" not in board


# --- the rules that keep the report honest ------------------------------------


def test_a_never_scored_job_renders_a_blank_score_not_a_zero():
    """`filtered_result` gives budget drops relevance_score=0. Printing that reads
    as "the model judged this worthless" — the misreading that hid a broken
    reranker for a whole run."""
    html = matching.build(snapshot(), [scored_record(), dropped_record()],
                          "2026-08-25_153432")
    payload = html.split('id="hs-unscored-data">')[1].split("</script>")[0]
    rows = json.loads(payload.replace("<\\/", "</"))
    assert len(rows) == 1
    assert "llm_score" not in rows[0] and "0" != rows[0].get("r")
    # The rendered cell is an em dash supplied by the template, never a number.
    assert "numeric blank" in html


def test_a_cluster_sibling_counts_as_scored():
    """It inherited a verdict from its representative — judged once for the whole
    cluster, not never judged. Treating it as unscored would blank a real score."""
    sibling = dropped_record(
        job_id="direct:google:3", relevance_score=61, skipped=False,
        skip_reason="duplicate_of_cluster", cluster_representative="direct:google:1",
    )
    scored, unscored = data.split_matches([scored_record(), sibling])
    assert len(scored) == 2 and unscored == []


def test_the_rationales_actually_reach_the_page():
    html = matching.build(snapshot(), [scored_record()], "2026-08-25_153432")
    for text in ("Python is evidenced throughout the RAG projects.",
                 "Roughly 3.5 years, but none of it ML engineering.",
                 "BTech met; MS in progress.",
                 "Hands-on LLM project experience",
                 "No professional ML role"):
        assert text in html, f"missing rationale text: {text!r}"


def test_the_two_rerank_scales_stay_in_separate_columns():
    """Historical rows carry logits from two different models. Merging or averaging
    them was a real bug; the funnel uses one model now, but rows written before that
    still render and must keep their two numbers apart."""
    html = matching.build(snapshot(), [scored_record(), dropped_record()],
                          "2026-08-25_153432")
    # Both values present, each under its own label, on the scored entry...
    assert "wide <b>8.88</b>" in html
    assert "cross <b>7.30</b>" in html
    # ...and as two separate columns over the unscored table.
    assert "<th>Refine</th>" in html and "<th>Wide</th>" in html


def test_a_single_model_row_shows_no_empty_wide_column():
    """New rows have no wide score. Rendering a dash where a number used to be reads
    as a bug rather than as an absence."""
    row = scored_record()
    row["rerank_score_wide"] = None
    html = matching.build(snapshot(), [row], "2026-08-25_153432")

    assert "wide <b>" not in html
    assert "cross <b>7.30</b>" in html


def test_the_unscored_list_scrolls_in_its_own_box():
    """Six thousand rows appended to the page would bury the reasoning above them,
    which is the part worth reading."""
    html = matching.build(snapshot(), [scored_record(), dropped_record()],
                          "2026-08-25_153432")
    assert 'id="hs-scroll"' in html and "class=\"scroll-y\"" in html
    assert "max-height: 70vh" in html and "overflow: auto" in html
    assert "position: sticky" in html, "the header must stay visible while scrolling"


def test_the_unscored_table_columns_line_up():
    """A rank column was added to the header and the row template separately; a
    mismatch between them shifts every value one cell to the left."""
    html = matching.build(snapshot(), [scored_record(), dropped_record()],
                          "2026-08-25_153432")
    header = html.split("<thead><tr>")[1].split("</tr>")[0]
    assert header.count("<th>") == 9
    row_template = html.split('html += "<tr>')[1].split("</tr>")[0]
    assert row_template.count("<td") == 9


def test_the_unscored_rows_arrive_already_ranked():
    """`load_all_matches` orders them refined-first then wide, each descending. The
    page presents that order rather than re-sorting, so nothing here can blend the
    two scales."""
    rows = [
        dropped_record(job_id="a", rerank_score=7.19, rerank_score_wide=7.19),
        dropped_record(job_id="b", rerank_score=6.79, rerank_score_wide=7.45),
        dropped_record(job_id="c", rerank_score=None, rerank_score_wide=-0.96),
    ]
    html = matching.build(snapshot(), [scored_record()] + rows, "2026-08-25_153432")
    payload = html.split('id="hs-unscored-data">')[1].split("</script>")[0]
    assert [r["f"] for r in json.loads(payload.replace("<\\/", "</"))] == ["7.19", "6.79", "—"]
    # The rank shown is the row's place in that server-side order, assigned before
    # any filtering, so it stays put when the reader searches.
    assert "rows[n].n = n + 1" in html


def test_a_running_sweep_does_not_report_zero_scored_as_a_verdict():
    html = matching.build(snapshot(in_progress=True, scored=0, shortlisted=0,
                                   top_score=None, candidates=0, gated_out=None), [],
                          "2026-08-25_153432")
    assert "still running" in html
    assert "sweep running" in html


def test_html_in_a_job_title_is_escaped():
    html = matching.build(snapshot(),
                          [scored_record(title="<script>alert(1)</script> Engineer")],
                          "2026-08-25_153432")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_embedded_json_cannot_close_its_own_script_block():
    html = matching.build(snapshot(),
                          [scored_record(), dropped_record(title="</script> Engineer")],
                          "2026-08-25_153432")
    payload = html.split('id="hs-unscored-data">')[1].split("</script>")[0]
    assert json.loads(payload.replace("<\\/", "</"))[0]["t"] == "</script> Engineer"


# --- failure containment ------------------------------------------------------


def test_a_broken_report_never_takes_down_the_run(tmp_path, monkeypatch):
    """Losing a diagnostic must not fail a sweep whose CSV, JSON and database rows
    are already safe — the trade `write_all_jobs_csv` documents."""
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reporting.data, "run_snapshot", explode)
    reporting.refresh("run-1", tmp_path, "2026-08-25_153432", final=True)  # must not raise


def test_a_write_failure_is_reported_as_none_not_raised(tmp_path):
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    assert dashboard.write(
        {"totals": {"runs": 0, "jobs": 0, "candidates": 0, "scored": 0, "shortlisted": 0},
         "applied": {"total": 0, "submitted": 0, "errors": 0, "skipped": 0, "recent": []},
         "runs": [], "live": None},
        blocked / "dashboard.html",
    ) is None


def test_the_throttle_lets_the_final_write_through(tmp_path, monkeypatch):
    """Throttled calls come off a callback that fires thousands of times; the final
    one comes from `_finalise_pipeline` and is the only call that sees a completed
    run, so it must never be skipped."""
    calls = []
    monkeypatch.setattr(reporting.data, "run_snapshot",
                        lambda db, run_id: calls.append(run_id) or snapshot())
    monkeypatch.setattr(reporting.data, "dashboard_snapshot", lambda db, limit=30: {
        "totals": {"runs": 0, "jobs": 0, "candidates": 0, "scored": 0, "shortlisted": 0},
        "applied": {"total": 0, "submitted": 0, "errors": 0, "skipped": 0, "recent": []},
        "runs": [], "live": None})
    monkeypatch.setattr(reporting, "_last_refresh", 0.0)

    reporting.refresh("r", tmp_path, "s", final=True)
    reporting.refresh("r", tmp_path, "s")          # throttled out
    reporting.refresh("r", tmp_path, "s", final=True)
    assert len(calls) == 2


# --- placement ----------------------------------------------------------------


def test_reports_go_to_the_results_root_and_nowhere_else(tmp_path):
    """Reports are results: they belong beside the CSVs, in the folder the user
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

    assert targets["latest_matching"].parent == root
    assert targets["dashboard"].parent == root
    # The stamped report goes with the run it describes, wherever that run dir is.
    assert targets["matching"].parent == tmp_path
    # And with no workspace set, the root itself stays inside DATA.
    assert root == paths.RESULTS_DIR and paths.DATA in root.parents


def test_the_stamped_report_and_the_rolling_copy_are_both_written(tmp_path):
    run_dir = tmp_path / "2026-08-25_153432"
    run_dir.mkdir()
    latest = tmp_path / matching.LATEST_NAME

    matching.write(snapshot(), [scored_record()], "2026-08-25_153432", run_dir, latest)

    stamped = run_dir / matching.matching_name("2026-08-25_153432")
    assert stamped.exists() and latest.exists()
    assert stamped.read_text(encoding="utf-8") == latest.read_text(encoding="utf-8")


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

    def finalise_run(self, run_id, phase, started_at, ended_at, summary):
        self.finalised.append(phase)

    def scrape_counts(self, run_id):
        return {"companies": 9641, "companies_with_jobs": 1008, "errors": 0, "jobs": 8504}

    def match_counts(self, run_id):
        return {"rows_total": len(self._all_rows), "scored": 1, "shortlisted": 0,
                "top_score": 61, "by_reason": {"": 1, "rerank_below_top_k": 1}}

    def run_phase_stats(self, run_id):
        # Both phases present — i.e. the sweep has finished, which is the state
        # `_finalise_pipeline` leaves behind.
        return {"match": {"threshold": 75, "model": "claude-sonnet-5"},
                "pipeline": {"started_at": "2026-08-25T22:34:32+00:00",
                             "finished_at": "2026-08-25T23:41:36+00:00"}}

    def recent_runs(self, limit=30):
        return [{"run_id": "2026-08-25T22-34-32Z",
                 "started_at": "2026-08-25T22:34:32+00:00",
                 "finished_at": "2026-08-25T23:41:36+00:00"}]

    def load_applied(self):
        return []

    # --- the overview page's reads --------------------------------------------
    # These were missing, and their absence was invisible: `overview_snapshot` raised
    # `AttributeError`, `refresh` swallowed it by design, and the overview was simply
    # never written while the assertions below said "both reports" and checked the
    # other two. A fake that is missing a method the real path calls is a hole in the
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


def _finalise_with_reports(tmp_path, monkeypatch, stamp="2026-08-25_153432"):
    import asyncio

    import orchestrate
    from hireshire import paths

    db = _ReportDB([scored_record(), dropped_record()])
    monkeypatch.setattr(paths, "LAST_RUN_PATH", tmp_path / "last_run.json")
    monkeypatch.setattr(paths, "results_root", lambda: tmp_path)
    monkeypatch.setattr(orchestrate, "get_db", lambda: db)
    monkeypatch.setattr(reporting, "get_db", lambda: db)

    results_dir = tmp_path / stamp
    results_dir.mkdir()
    asyncio.run(
        orchestrate._finalise_pipeline("2026-08-25T22-34-32Z", results_dir, "started", stamp)
    )
    return db, results_dir


def test_finalising_a_run_writes_every_report(tmp_path, monkeypatch):
    db, results_dir = _finalise_with_reports(tmp_path, monkeypatch)

    assert (results_dir / matching.matching_name("2026-08-25_153432")).exists()
    assert (tmp_path / matching.LATEST_NAME).exists()
    assert (tmp_path / dashboard.DASHBOARD_NAME).exists()
    # Both overview scopes. `refresh` swallows every exception, so a page that stops
    # being written fails nothing unless something asserts it exists.
    assert (results_dir / overview.run_overview_name("2026-08-25_153432")).exists()
    assert (tmp_path / overview.OVERVIEW_NAME).exists()


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


def test_the_pointer_file_names_the_reports_for_the_skills(tmp_path, monkeypatch):
    """The skills must never reconstruct these paths — the results root is a folder
    the user chose and can move."""
    _, results_dir = _finalise_with_reports(tmp_path, monkeypatch)
    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))

    assert pointer["matching_html"] == str(results_dir / matching.matching_name("2026-08-25_153432"))
    assert pointer["latest_matching_html"] == str(tmp_path / matching.LATEST_NAME)
    assert pointer["dashboard_html"] == str(tmp_path / dashboard.DASHBOARD_NAME)
    # The pointer /apply actually reads is untouched by any of this.
    assert pointer["json"] == str(results_dir / "2026-08-25_153432_results.json")


def test_a_finished_run_leaves_a_dashboard_that_stops_reloading(tmp_path, monkeypatch):
    """The final refresh runs after `finalise_run`, so the pipeline's run row exists
    and the page knows the sweep is over. Refreshing before it would leave a
    finished run reloading itself forever."""
    _finalise_with_reports(tmp_path, monkeypatch)
    html = (tmp_path / dashboard.DASHBOARD_NAME).read_text(encoding="utf-8")
    assert 'http-equiv="refresh"' not in html


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
    """Same trade `reporting.refresh` and `write_all_jobs_csv` document: a report is
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
    the stop can still be mid-write. If it landed after `_finalise_pipeline`'s
    `final=True` write it would re-arm the dashboard's meta refresh and leave a
    finished run reloading itself forever — the failure
    `test_a_finished_run_leaves_a_dashboard_that_stops_reloading` pins from the
    other side.
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
    `reporting.refresh` after `_finalise_pipeline` has begun, because that call is
    the one that renders the run as finished.
    """
    monkeypatch.setattr(orchestrate, "_REPORT_TICK_S", 0.01)
    monkeypatch.setattr(orchestrate.paths, "make_run_dir", lambda stamp: tmp_path)

    calls = []
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

    finalised = {}

    async def fake_finalise(run_id, results_dir, started_at, stamp):
        finalised["refreshes_before"] = len(calls)

    monkeypatch.setattr(orchestrate.scraper, "main", silent_scraper)
    monkeypatch.setattr(orchestrate.matcher, "main", silent_matcher)
    monkeypatch.setattr(orchestrate, "_collect_results", drain)
    monkeypatch.setattr(orchestrate, "_track_results", sink)
    monkeypatch.setattr(orchestrate, "_finalise_pipeline", fake_finalise)

    assert asyncio.run(orchestrate.run_pipeline(quiet=True)) is not None

    assert finalised["refreshes_before"] >= 2, (
        "the reports must keep rebuilding through a phase that emits no scores"
    )
    assert len(calls) == finalised["refreshes_before"], (
        "a rebuild landed after _finalise_pipeline began; it would re-arm the "
        "dashboard's meta refresh on a finished run"
    )
