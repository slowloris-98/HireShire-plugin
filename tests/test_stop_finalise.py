"""A sweep killed before its `finally` must not leave its dashboards reading "running".

`--stop` is `taskkill /F` on Windows, and a killed shell task is no gentler: neither
runs `run_pipeline`'s `finally`, so the pipeline `runs` row — the pages' only signal
that a sweep is over — was never written, and both pages kept their `running` chip
and meta refresh for good. `orchestrate.finalise_abandoned_runs` writes what that
`finally` would have; `--stop` and the next sweep's start-up both call it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import orchestrate
from hireshire import paths, reporting
from hireshire.reporting import overview
from hireshire.storage.db import PHASE_PIPELINE, Database

OLD = "2026-09-20T08-00-00Z"
NEW = "2026-09-24T08-00-00Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(paths, "LAST_RUN_PATH", tmp_path / "last_run.json")
    monkeypatch.setattr(paths, "CURRENT_RUN_PATH", tmp_path / "current_run.json")
    monkeypatch.setattr(paths, "results_root", lambda: tmp_path / "results")
    monkeypatch.setattr(orchestrate, "get_db", lambda *a, **k: db)
    monkeypatch.setattr(reporting, "get_db", lambda *a, **k: db)
    return db, tmp_path


def _start(db, tmp_path, run_id, with_marker=True):
    """What `run_pipeline` has done by the time a kill can land."""
    stamp = orchestrate._run_stamp(orchestrate._run_started_at(run_id))
    results_dir = tmp_path / "results" / stamp
    results_dir.mkdir(parents=True)
    if with_marker:
        orchestrate._write_current_run(run_id, stamp, results_dir, "started")
    db.start_progress(run_id, False)
    # A mid-sweep refresh, as the ticker would have written before the kill.
    reporting.refresh(run_id, results_dir, stamp, final=True)
    return stamp, results_dir


def _page(results_dir, stamp):
    return (results_dir / overview.run_overview_name(stamp)).read_text(encoding="utf-8")


def test_a_killed_sweep_reads_as_running_until_finalised(env):
    db, tmp_path = env
    stamp, results_dir = _start(db, tmp_path, NEW)
    assert 'http-equiv="refresh"' in _page(results_dir, stamp)

    assert asyncio.run(orchestrate.finalise_abandoned_runs()) == [stamp]

    stats = db.run_phase_stats(NEW)[PHASE_PIPELINE]
    assert stats["completed"] is False and stats["stopped"] is True
    for html in (_page(results_dir, stamp),
                 (tmp_path / "results" / overview.LIFETIME_NAME).read_text(encoding="utf-8")):
        assert 'http-equiv="refresh"' not in html
        assert "chip live" not in html
    assert '<span class="chip">stopped</span>' in _page(results_dir, stamp)
    # The partial outputs its `finally` would have written, and the pointer to them.
    assert (results_dir / f"{stamp}_results.csv").exists()
    last = json.loads(paths.LAST_RUN_PATH.read_text(encoding="utf-8"))
    assert last["run_id"] == NEW and last["complete"] is False
    assert not paths.CURRENT_RUN_PATH.exists()
    # Idempotent: nothing is left to finalise.
    assert asyncio.run(orchestrate.finalise_abandoned_runs()) == []


def test_only_the_newest_orphan_repoints_last_run(env):
    db, tmp_path = env
    old_stamp, old_dir = _start(db, tmp_path, OLD, with_marker=False)
    new_stamp, new_dir = _start(db, tmp_path, NEW)

    assert asyncio.run(orchestrate.finalise_abandoned_runs()) == [old_stamp, new_stamp]

    assert json.loads(paths.LAST_RUN_PATH.read_text(encoding="utf-8"))["run_id"] == NEW
    assert not (old_dir / f"{old_stamp}_results.csv").exists()
    # The old one is still closed out, found without a marker.
    assert 'http-equiv="refresh"' not in _page(old_dir, old_stamp)


def test_a_sweep_that_finished_is_not_an_orphan(env):
    db, tmp_path = env
    _start(db, tmp_path, NEW)
    db.finalise_run(NEW, PHASE_PIPELINE, "started", None, {"completed": True})

    assert db.abandoned_runs() == []
    assert asyncio.run(orchestrate.finalise_abandoned_runs()) == []
    assert "stopped" not in db.run_phase_stats(NEW)[PHASE_PIPELINE]


def test_the_finaliser_leaves_a_live_sweep_alone(env, monkeypatch):
    """The in-flight run looks exactly like an orphan, so liveness is the only guard."""
    db, tmp_path = env
    _start(db, tmp_path, NEW)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import finalise_stopped
    from hireshire import process_liveness, sweep_pid

    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(paths, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(sweep_pid, "read", lambda *a, **k: 4242)
    monkeypatch.setattr(process_liveness, "is_alive", lambda pid: True)

    assert finalise_stopped.main() == 0
    assert PHASE_PIPELINE not in db.run_phase_stats(NEW)


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import bootstrap as bs

    monkeypatch.setattr(bs, "DATA", tmp_path)
    monkeypatch.setattr(bs, "venv_python", lambda *a: Path(sys.executable))
    monkeypatch.setattr(bs, "is_alive", lambda pid: False)
    return bs


def _fake_run(calls, finaliser_rc=0):
    def run(argv, **kwargs):
        calls.append(argv)
        rc = finaliser_rc if str(argv[-1]).endswith("finalise_stopped.py") else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
    return run


@pytest.mark.parametrize("finaliser_rc", [0, 1])
def test_stop_finalises_after_the_kill_and_never_fails_on_it(bootstrap, monkeypatch,
                                                              finaliser_rc, capsys):
    bootstrap.sweep_pid.write(4242, bootstrap.DATA)
    calls: list = []
    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run(calls, finaliser_rc))
    if sys.platform != "win32":
        monkeypatch.setattr(bootstrap.os, "kill", lambda pid, sig: None)

    assert bootstrap.stop() == 0
    assert str(calls[-1][-1]).endswith("finalise_stopped.py")
    assert ("could not update the dashboards" in capsys.readouterr().out) == bool(finaliser_rc)


def test_stop_with_nothing_running_still_repairs_the_pages(bootstrap, monkeypatch):
    calls: list = []
    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run(calls))

    assert bootstrap.stop() == 0
    assert [str(c[-1]) for c in calls][-1].endswith("finalise_stopped.py")
