"""The streaming applier: one `claude -p` session per shortlisted job, as it arrives.

What is pinned here, each for a failure that costs the user something real:

* the prompt never rides on argv (a leading `---` once failed every apply phase);
* the subscription is used, not a pay-as-you-go key;
* a verdict retires a job, a failed launch does not — or a broken MCP server would
  lose a whole sweep's shortlist for good;
* an ambiguous ending (timeout, unreadable result) *is* recorded, because the form may
  already be submitted and retrying would apply twice;
* nothing is launched for excluded employers or jobs already applied to.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hireshire.applier import worker
from hireshire.applier.config import ApplierSettings
from hireshire.storage.db import Database


class _Proc:
    pid = 4242

    def __init__(self, stdout: bytes = b"", rc: int = 0, hang: bool = False) -> None:
        self.returncode = None
        self._rc = rc
        self._stdout = stdout
        self._hang = hang
        self.sent: bytes | None = None

    async def communicate(self, payload: bytes | None = None):
        self.sent = payload
        if self._hang:
            await asyncio.sleep(3600)
        self.returncode = self._rc
        return self._stdout, (b"boom" if self._rc else b"")

    def kill(self) -> None:
        pass


def _outcome(**fields) -> _Proc:
    return _Proc(json.dumps({"type": "result", "structured_output": fields}).encode())


@pytest.fixture
def launcher(monkeypatch):
    """Fake `create_subprocess_exec`. Push `_Proc`s or exceptions onto `script`; an
    empty script answers `submitted`."""
    calls: list[dict] = []
    script: list = []
    killed: list[bool] = []

    async def fake_exec(*argv, **kwargs):
        nxt = script.pop(0) if script else _outcome(status="submitted")
        calls.append({"argv": argv, "kwargs": kwargs, "proc": nxt})
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(worker, "terminate_apply_subprocess", lambda: killed.append(True))
    return calls, script, killed


def _settings(tmp_path, **over) -> ApplierSettings:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    base = dict(
        enable_applier=True, resume_path=str(resume), inter_job_delay_s=0,
        applied_dir=str(tmp_path / "applied"),
        apply_timeout_s=5, exclude_companies=["Google"], first_name="Ada",
        last_name="Lovelace", email="ada@example.com", phone="555",
    )
    base.update(over)
    return ApplierSettings(**base)


def _job(job_id: str, company: str = "acme") -> dict:
    return {"job_id": job_id, "title": "Account Manager", "company": company,
            "job_url": f"https://example.com/jobs/{job_id}"}


def _run(tmp_path, jobs, settings=None, db=None, backlog=False):
    db = db or Database(tmp_path / "test.db")
    settings = settings or _settings(tmp_path)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        for j in jobs:
            await q.put(j)
        await q.put(None)
        return await worker.run_apply_worker(
            q, settings, "RESUME TEXT", db=db, include_backlog=backlog
        )

    return asyncio.run(go()), db


def _statuses(db: Database) -> dict[str, str]:
    return {r["job_id"]: r["status"] for r in db.load_applied()}


# --- how the session is launched --------------------------------------------

def test_the_prompt_goes_on_stdin_and_never_in_argv(tmp_path, launcher, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])

    (call,) = calls
    for arg in call["argv"]:
        assert "RESUME TEXT" not in arg and "Apply to one job" not in arg
        assert not arg.startswith("---")
    sent = call["proc"].sent.decode("utf-8")
    assert "Apply to one job" in sent, "the shared per-job rules did not reach stdin"
    assert "https://example.com/jobs/j1" in sent and "RESUME TEXT" in sent
    assert call["kwargs"]["stdin"] is asyncio.subprocess.PIPE
    assert "ANTHROPIC_API_KEY" not in call["kwargs"]["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in call["kwargs"]["env"]


def test_the_session_brings_its_own_browser_server(tmp_path, launcher):
    """Measured: a `claude -p` the engine starts did not load the plugin, so the
    namespaced Playwright tools were missing and every application would have failed.
    The session must load the plugin's `.mcp.json` itself, and only that."""
    from pathlib import Path

    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])
    argv = list(calls[0]["argv"])

    assert "--strict-mcp-config" in argv
    config = Path(argv[argv.index("--mcp-config") + 1])
    assert config.name == ".mcp.json" and config.is_file()
    assert "playwright" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    # The per-job rules must name the tools as that server exposes them.
    assert "mcp__playwright__browser_navigate" in worker.PROMPT_PATH.read_text(encoding="utf-8")


def test_the_session_runs_in_applied_dir_so_screenshots_survive_updates(tmp_path, launcher):
    """Playwright MCP writes screenshots under the session's working directory. A
    sweep's is ROOT, which every plugin update replaces."""
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path / "applied")
    assert (tmp_path / "applied").is_dir()


# --- verdicts ---------------------------------------------------------------

def test_submitted_and_error_outcomes_are_recorded(tmp_path, launcher):
    _, script, _ = launcher
    script += [_outcome(status="submitted", screenshot="/tmp/j1.png"),
               _outcome(status="error", error="Sign in to apply")]
    stats, db = _run(tmp_path, [_job("j1"), _job("j2")])

    assert _statuses(db) == {"j1": "submitted", "j2": "error"}
    assert stats["submitted"] == 1 and stats["error"] == 1


def test_a_location_skip_is_not_recorded(tmp_path, launcher):
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location"))
    stats, db = _run(tmp_path, [_job("j1")])
    assert _statuses(db) == {}
    assert stats["skipped_location"] == 1


def test_excluded_and_already_applied_jobs_launch_nothing(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    db.record_applied("j2", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
                      "submitted", None, None)
    stats, _ = _run(tmp_path, [_job("j1", company="google"), _job("j2")], db=db)
    assert calls == []
    assert stats["excluded"] == 1


def test_a_missing_resume_launches_nothing(tmp_path, launcher):
    calls, _, _ = launcher
    settings = _settings(tmp_path, resume_path=str(tmp_path / "nope.pdf"))
    stats, db = _run(tmp_path, [_job("j1"), _job("j2")], settings=settings)
    assert calls == [] and _statuses(db) == {}
    assert stats["deferred"] == 2


# --- failures ---------------------------------------------------------------

def test_launch_failures_are_not_recorded_and_trip_the_breaker(tmp_path, launcher):
    calls, script, _ = launcher
    script += [FileNotFoundError("claude"), _Proc(rc=1), _Proc(rc=1)]
    stats, db = _run(tmp_path, [_job(f"j{i}") for i in range(5)])

    assert len(calls) == worker.BREAKER_LIMIT, "kept launching after the breaker tripped"
    assert _statuses(db) == {}, "a failed launch retired a job"
    assert stats["deferred"] == 5


def test_a_success_resets_the_breaker(tmp_path, launcher):
    calls, script, _ = launcher
    script += [_Proc(rc=1), _Proc(rc=1), _outcome(status="submitted"),
               _Proc(rc=1), _Proc(rc=1)]
    _run(tmp_path, [_job(f"j{i}") for i in range(6)])
    assert len(calls) == 6


def test_a_timeout_is_recorded_as_an_unconfirmed_error_and_kills_the_session(
        tmp_path, launcher):
    _, script, killed = launcher
    script.append(_Proc(hang=True))
    _, db = _run(tmp_path, [_job("j1")], settings=_settings(tmp_path, apply_timeout_s=0.05))

    (row,) = db.load_applied()
    assert row["status"] == "error"
    assert "check before applying again" in row["error"]
    assert killed, "the timed-out browser session was left running"


def test_an_unreadable_result_is_recorded_rather_than_retried(tmp_path, launcher):
    _, script, _ = launcher
    script.append(_Proc(b"not json at all"))
    _, db = _run(tmp_path, [_job("j1")])
    assert _statuses(db) == {"j1": "error"}


def test_cancelling_the_worker_kills_the_session_in_flight(tmp_path, launcher):
    calls, script, killed = launcher
    script.append(_Proc(hang=True))
    db = Database(tmp_path / "test.db")
    settings = _settings(tmp_path, apply_timeout_s=3600)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        await q.put(_job("j1"))
        task = asyncio.create_task(
            worker.run_apply_worker(q, settings, "r", db=db, include_backlog=False)
        )
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert killed
    assert _statuses(db) == {}


# --- the backlog ------------------------------------------------------------

def _match(db: Database, run_id: str, job_id: str, *, shortlisted=True, rep=None,
           scored_at: datetime | None = None, score=80) -> None:
    scored_at = scored_at or datetime.now(timezone.utc)
    raw = {"job_id": job_id, "board_token": "acme", "title": "Account Manager",
           "absolute_url": f"https://example.com/jobs/{job_id}",
           "relevance_score": score, "cluster_representative": rep}
    # Compact separators, as `model_dump_json` writes every real row — the no-JSON1
    # fallback in `Database._sibling_sql` depends on it.
    db.upsert_match(run_id, job_id, "acme", "Account Manager", score, shortlisted,
                    False, None, run_id, scored_at.isoformat(),
                    json.dumps(raw, separators=(",", ":")))


@pytest.mark.parametrize("json1", [True, False])
def test_pending_applications_are_shortlisted_unapplied_recent_representatives(
        tmp_path, json1):
    db = Database(tmp_path / "test.db")
    db._has_json1 = json1
    old = datetime.now(timezone.utc) - timedelta(days=10)

    _match(db, "r1", "fresh")
    _match(db, "r1", "sibling", rep="fresh")
    _match(db, "r1", "applied")
    _match(db, "r1", "rejected", shortlisted=False)
    _match(db, "r0", "stale", scored_at=old)
    _match(db, "r0", "twice", score=70)
    _match(db, "r1", "twice", score=75)
    db.record_applied("applied", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
                      "error", None, None)

    since = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    rows = db.load_pending_applications(since)

    assert sorted(r["job_id"] for r in rows) == ["fresh", "twice"]
    for r in rows:
        assert r["company"] == "acme"
        assert r["job_url"] == f"https://example.com/jobs/{r['job_id']}"


def test_a_job_in_both_the_backlog_and_the_stream_is_applied_to_once(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    stats, _ = _run(tmp_path, [_job("j1")], db=db, backlog=True)
    assert len(calls) == 1
    assert stats["submitted"] == 1


# --- the queue it is fed from -----------------------------------------------

class _RecordingDB:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def record_pipeline_result(self, run_id, record):
        if self.fail:
            raise RuntimeError("disk full")


def _track(tmp_path, monkeypatch, db, records):
    import orchestrate

    monkeypatch.setattr(orchestrate, "get_db", lambda *a, **k: db)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        apply_q: asyncio.Queue = asyncio.Queue()
        for r in records:
            await q.put(r)
        await q.put(None)
        try:
            await orchestrate._track_results(q, tmp_path, "run", "stamp", apply_q=apply_q)
        finally:
            return [apply_q.get_nowait() for _ in range(apply_q.qsize())]

    return asyncio.run(go())


def test_every_tracked_result_is_handed_to_the_applier(tmp_path, monkeypatch):
    got = _track(tmp_path, monkeypatch, _RecordingDB(), [_job("a"), _job("b")])
    assert [g and g["job_id"] for g in got] == ["a", "b", None]


def test_the_applier_gets_its_sentinel_even_when_tracking_fails(tmp_path, monkeypatch):
    got = _track(tmp_path, monkeypatch, _RecordingDB(fail=True), [_job("a")])
    assert got == [None], "the apply worker would wait forever"
