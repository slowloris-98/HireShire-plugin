"""The results CSV.

This file is the plugin's only user-facing output, and since it moved out of the
plugin's data directory and into the user's own folder it is also a file they open
in Excel while a sweep is running. Both of those make its two properties worth
pinning: the column contract, and the fact that a locked file degrades to DB-only
writes instead of taking down a twenty-minute run.
"""
from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

import orchestrate


class _FakeDB:
    """Stands in for the SQLite facade — the DB is the source of truth, so these
    tests assert it keeps receiving rows even when the CSV cannot be written."""

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


def test_csv_is_named_for_the_run_and_carries_the_agreed_columns(tmp_path, db):
    stamp = "2026-08-12_143005"
    asyncio.run(_drain(tmp_path, stamp, [_record("Engineer", "Acme", 91)]))

    csv_path = tmp_path / f"{stamp}_results.csv"
    assert csv_path.exists(), list(tmp_path.iterdir())

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert list(rows[0]) == orchestrate._CSV_FIELDS
    assert rows[0]["title"] == "Engineer"
    assert rows[0]["company"] == "Acme"
    assert rows[0]["relevance_score"] == "91"


def test_every_row_reaches_both_the_csv_and_the_database(tmp_path, db):
    stamp = "2026-08-12_143005"
    records = [_record("Engineer", "Acme", 91), _record("Analyst", "Globex", 78)]
    asyncio.run(_drain(tmp_path, stamp, records))

    with (tmp_path / f"{stamp}_results.csv").open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2
    assert len(db.rows) == 2


def test_a_second_pass_appends_without_repeating_the_header(tmp_path, db):
    """The handle is opened in append mode, so a resumed or re-entered run adds to
    the file rather than truncating results the user already has."""
    stamp = "2026-08-12_143005"
    asyncio.run(_drain(tmp_path, stamp, [_record("Engineer", "Acme", 91)]))
    asyncio.run(_drain(tmp_path, stamp, [_record("Analyst", "Globex", 78)]))

    text = (tmp_path / f"{stamp}_results.csv").read_text(encoding="utf-8")
    assert text.count("relevance_score") == 1
    with (tmp_path / f"{stamp}_results.csv").open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2


def test_a_locked_csv_degrades_to_database_only_instead_of_failing_the_run(
    tmp_path, db, monkeypatch
):
    """`_open_csv_append` returns None once it gives up on a Windows file lock.

    The user having the CSV open in Excel must cost them the CSV for that run, not
    the run itself — which is why the DB is the source of truth and this path
    exists at all.
    """
    async def _locked(path, attempts=5, base_delay=0.5):
        return None

    monkeypatch.setattr(orchestrate, "_open_csv_append", _locked)

    stamp = "2026-08-12_143005"
    records = [_record("Engineer", "Acme", 91), _record("Analyst", "Globex", 78)]
    asyncio.run(_drain(tmp_path, stamp, records))     # must not raise

    assert len(db.rows) == 2
    assert not (tmp_path / f"{stamp}_results.csv").exists()


class _FinaliseDB(_FakeDB):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows
        self.finalised = None

    def load_pipeline_results(self, run_id):
        return self._rows

    def finalise_run(self, run_id, phase, started_at, ended_at, summary):
        self.finalised = (run_id, phase, summary)


def _finalise(tmp_path, stamp, rows, monkeypatch):
    from hireshire import paths

    monkeypatch.setattr(paths, "LAST_RUN_PATH", tmp_path / "last_run.json")
    db = _FinaliseDB(rows)
    monkeypatch.setattr(orchestrate, "get_db", lambda: db)
    results_dir = tmp_path / stamp
    results_dir.mkdir()
    asyncio.run(
        orchestrate._finalise_pipeline("2026-08-12T14-30-05Z", results_dir, "started", stamp)
    )
    return db, results_dir


def test_finalise_writes_the_pointer_apply_reads(tmp_path, monkeypatch):
    """/hireshire:apply opens last_run.json rather than guessing where the results
    root is — the root moved into a folder the user can relocate."""
    import json

    stamp = "2026-08-12_143005"
    db, results_dir = _finalise(tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch)

    pointer = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))

    assert pointer["run_id"] == "2026-08-12T14-30-05Z"
    assert pointer["stamp"] == stamp
    assert pointer["json"] == str(results_dir / f"{stamp}_results.json")
    assert pointer["csv"] == str(results_dir / f"{stamp}_results.csv")
    assert pointer["total_results"] == 1
    # ...and the file it points at is really there, with the rows in it.
    assert json.loads(Path(pointer["json"]).read_text(encoding="utf-8"))[0]["company"] == "Acme"


def test_an_unwritable_json_does_not_report_a_successful_run_as_failed(tmp_path, monkeypatch):
    """By this point the CSV and every database row are already written. Letting an
    OSError escape would log the whole sweep as a failure and return None."""
    stamp = "2026-08-12_143005"

    def _boom(*a, **kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "write_text", _boom)

    db, _ = _finalise(tmp_path, stamp, [_record("Engineer", "Acme", 91)], monkeypatch)

    # The run is still finalised in the DB, which is what makes it a success.
    assert db.finalised is not None
    assert db.finalised[2] == {"total_results": 1}


def test_stamp_is_local_time_and_shared_by_folder_and_file():
    """`run_id` stays UTC because it keys five tables; the stamp is local because a
    human reads it off a directory listing."""
    from datetime import datetime, timezone

    now = datetime(2026, 8, 12, 9, 0, 5, tzinfo=timezone.utc)
    stamp = orchestrate._run_stamp(now)

    assert stamp == now.astimezone().strftime("%Y-%m-%d_%H%M%S")
    assert orchestrate._csv_name(stamp).startswith(stamp)
    assert orchestrate._json_name(stamp).startswith(stamp)
