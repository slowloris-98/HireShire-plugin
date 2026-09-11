"""The run's output files, and what survives a sweep that does not finish.

The CSV is the plugin's only tabular user-facing output. It used to be appended row
by row as jobs were judged, which made it a file in processing order that also had
to survive being open in Excel; it is now written once from the database, sorted
across the whole sweep.

That trade is the thing most of this file pins. Writing at the end means writing in
a `finally`, because otherwise a sweep that died fifteen minutes in would leave real
scored rows reachable only by opening the database by hand — no CSV, no JSON, a
`last_run.json` still naming the previous run, and two pages left meta-refreshing
forever on a process that is dead.
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

import orchestrate


class _FakeDB:
    """Stands in for the SQLite facade. The DB is the source of truth, so the
    streaming phase asserts only that rows keep reaching it."""

    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def record_pipeline_result(self, run_id, record):
        self.rows.append((run_id, record))


def _record(title: str, company: str, score: int) -> dict:
    return {
        "title": title,
        "company": company,
        "location": "Remote",
        "posted_at": "2026-08-10T00:00:00Z",
        "job_url": f"https://example.com/{title}",
        "relevance_score": score,
        "rerank_score": 0.9,
        "job_id": f"id-{title}",
        "found_at": "2026-08-12T14:30:05Z",
    }


def _match_row(job_id: str, score: int, **over) -> dict:
    row = {
        "job_id": job_id,
        "board_token": "acme",
        "title": f"Engineer {job_id}",
        "location": "Remote",
        "absolute_url": f"https://example.com/{job_id}",
        "posted_at": "2026-08-10T00:00:00Z",
        "relevance_score": score,
        "rerank_score": 4.4,
        "shortlisted": True,
        "skipped": False,
        "skip_reason": None,
    }
    row.update(over)
    return row


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(orchestrate, "get_db", lambda: fake)
    return fake


async def _drain(results_dir, stamp, records, run_id="2026-08-12T14-30-05Z"):
    q: asyncio.Queue = asyncio.Queue()
    for r in records:
        await q.put(r)
    await q.put(None)          # the sentinel every queue gets exactly one of
    await orchestrate._track_results(q, results_dir, run_id, stamp, quiet=True)


def test_the_streaming_phase_writes_rows_to_the_database_only(tmp_path, db):
    """No file here any more. The CSV is sorted across the whole sweep, so no row's
    position is known until every row exists — and a half-written file in processing
    order was the thing a user opened mid-sweep and misread."""
    stamp = "2026-08-12_143005"
    records = [_record("Engineer", "Acme", 91), _record("Analyst", "Globex", 78)]
    asyncio.run(_drain(tmp_path, stamp, records))

    assert len(db.rows) == 2
    assert list(tmp_path.iterdir()) == []


# --- the run's output files ---------------------------------------------------


class _OutputsDB(_FakeDB):
    def __init__(self, rows, all_rows=None, applied=None):
        super().__init__()
        self._rows = rows
        self._all_rows = all_rows if all_rows is not None else []
        self._applied = applied or set()
        self.finalised = None

    def load_pipeline_results(self, run_id):
        return self._rows

    def load_all_matches(self, run_id):
        return self._all_rows

    def applied_ids(self):
        return self._applied

    def finalise_run(self, run_id, phase, started_at, ended_at, summary):
        self.finalised = (run_id, phase, summary)


def _write_outputs(tmp_path, stamp, rows, monkeypatch, all_rows=None, applied=None,
                   complete=True):
    from hireshire import paths

    monkeypatch.setattr(paths, "LAST_RUN_PATH", tmp_path / "last_run.json")
    db = _OutputsDB(rows, all_rows, applied)
    monkeypatch.setattr(orchestrate, "get_db", lambda: db)
    results_dir = tmp_path / stamp
    results_dir.mkdir(exist_ok=True)
    total = asyncio.run(
        orchestrate._write_run_outputs(
            "2026-08-12T14-30-05Z", results_dir, stamp, complete=complete
        )
    )
    return db, results_dir, total


def test_the_outputs_are_the_csv_the_json_and_the_pointer(tmp_path, monkeypatch):
    """/hireshire:apply opens last_run.json rather than guessing where the results
    root is — the root moved into a folder the user can relocate."""
    stamp = "2026-08-12_143005"
    db, results_dir, total = _write_outputs(
        tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch,
        all_rows=[_match_row("j1", 91)],
    )

    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))

    assert total == 1
    assert pointer["run_id"] == "2026-08-12T14-30-05Z"
    assert pointer["stamp"] == stamp
    assert pointer["json"] == str(results_dir / f"{stamp}_results.json")
    assert pointer["csv"] == str(results_dir / f"{stamp}_results.csv")
    assert pointer["total_results"] == 1
    assert pointer["complete"] is True
    # ...and the files it points at are really there, with the rows in them.
    assert json.loads(Path(pointer["json"]).read_text(encoding="utf-8"))[0]["company"] == "Acme"
    with Path(pointer["csv"]).open(encoding="utf-8-sig") as f:
        assert next(csv.DictReader(f))["llm_score"] == "91"


def test_the_pointer_names_only_the_two_pages_that_exist(tmp_path, monkeypatch):
    """The dashboard and the matching report are gone, and a pointer still naming
    them would send a skill at a path nothing writes."""
    stamp = "2026-08-12_143005"
    _write_outputs(tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch)

    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))
    assert pointer["overview_html"].endswith("overview.html")
    assert pointer["run_overview_html"].endswith(f"{stamp}_overview.html")
    for gone in ("matching_html", "latest_matching_html", "dashboard_html",
                 "all_jobs_csv"):
        assert gone not in pointer


def test_the_csv_says_which_jobs_have_been_applied_to(tmp_path, monkeypatch):
    """`applied` is read once for the whole file and passed in, because the table is
    keyed on the job alone — an application is a fact about a job, not a sweep."""
    stamp = "2026-08-12_143005"
    _, results_dir, _ = _write_outputs(
        tmp_path, stamp, [], monkeypatch,
        all_rows=[_match_row("j1", 91), _match_row("j2", 80)],
        applied={"j1"},
    )

    with (results_dir / f"{stamp}_results.csv").open(encoding="utf-8-sig") as f:
        assert [r["applied"] for r in csv.DictReader(f)] == ["yes", "no"]


def test_an_unwritable_json_does_not_report_a_successful_run_as_failed(tmp_path, monkeypatch):
    """By this point every database row is already written. Letting an OSError
    escape would log the whole sweep as a failure and return None."""
    stamp = "2026-08-12_143005"

    def _boom(*a, **kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "write_text", _boom)

    # Must not raise; the row count still comes back.
    _, _, total = _write_outputs(
        tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch
    )
    assert total == 1


# --- a sweep that does not finish ---------------------------------------------


def test_a_partial_run_still_writes_its_files_and_says_so(tmp_path, monkeypatch):
    """The whole reason the outputs moved into a `finally`. Before it, a sweep that
    died half-scored left no CSV, no JSON, and a pointer at the previous run."""
    stamp = "2026-08-12_143005"
    _, results_dir, _ = _write_outputs(
        tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch,
        all_rows=[_match_row("j1", 91)], complete=False,
    )

    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))
    assert pointer["complete"] is False
    # The files are real, just partial — and /apply reads `json` either way.
    assert (results_dir / f"{stamp}_results.csv").exists()
    assert (results_dir / f"{stamp}_results.json").exists()


def test_the_runs_row_is_written_whether_or_not_the_sweep_finished(tmp_path, monkeypatch):
    """That row is the pages' only signal for "is this sweep still going". A crashed
    run without one leaves both of them reloading themselves forever with their
    elapsed figure climbing on a process that is dead."""
    from hireshire.storage.db import PHASE_PIPELINE

    db = _OutputsDB([])
    monkeypatch.setattr(orchestrate, "get_db", lambda: db)
    monkeypatch.setattr(orchestrate.reporting, "refresh", lambda *a, **kw: None)

    asyncio.run(
        orchestrate._finalise_pipeline(
            "2026-08-12T14-30-05Z", tmp_path, "started", "2026-08-12_143005",
            total_results=3, complete=False,
        )
    )

    run_id, phase, summary = db.finalised
    assert phase == PHASE_PIPELINE
    assert summary == {"total_results": 3, "completed": False}


def test_the_final_refresh_runs_after_the_runs_row_lands(tmp_path, monkeypatch):
    """Ordering, not sequencing for its own sake: the pages read that row to decide
    whether to keep reloading, so refreshing first leaves a finished run
    meta-refreshing forever."""
    order: list[str] = []

    class _OrderDB(_OutputsDB):
        def finalise_run(self, *a, **kw):
            order.append("runs-row")
            super().finalise_run(*a, **kw)

    monkeypatch.setattr(orchestrate, "get_db", lambda: _OrderDB([]))
    monkeypatch.setattr(
        orchestrate.reporting, "refresh",
        lambda *a, **kw: order.append("refresh"),
    )

    asyncio.run(
        orchestrate._finalise_pipeline(
            "2026-08-12T14-30-05Z", tmp_path, "started", "2026-08-12_143005"
        )
    )
    assert order == ["runs-row", "refresh"]


def test_stamp_is_local_time_and_shared_by_folder_and_file():
    """`run_id` stays UTC because it keys five tables; the stamp is local because a
    human reads it off a directory listing."""
    from datetime import datetime, timezone

    from hireshire.results_export import results_name

    now = datetime(2026, 8, 12, 9, 0, 5, tzinfo=timezone.utc)
    stamp = orchestrate._run_stamp(now)

    assert stamp == now.astimezone().strftime("%Y-%m-%d_%H%M%S")
    assert results_name(stamp).startswith(stamp)
    assert orchestrate._json_name(stamp).startswith(stamp)
