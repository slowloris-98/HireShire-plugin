"""Guards on what happens when the scoring backend is broken.

A misused `--json-schema` argument once failed all 100 scoring calls in a real sweep.
Three things went wrong at once and each is pinned here: the flag was wrong, the
failures permanently retired every job they touched, and the run still reported
"0 new matches" as though the jobs had simply not been good enough.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import matcher as matcher_mod
from hireshire import claude_cli
from hireshire.matcher import scorer as scorer_mod
from hireshire.matcher.config import MatcherSettings
from hireshire.matcher.scorer import (
    SCORING_ERROR_SKIP_REASONS,
    ClaudeCodeBackend,
    CLILaunchError,
    MatchResult,
    ScoringSchema,
)
from hireshire.matcher.seen import SeenStore
from hireshire.models.job import Job, Location
from hireshire.storage.db import Database


def _result(job_id: str, skip_reason: str | None, score: int = 0) -> MatchResult:
    return MatchResult(
        job_id=job_id,
        board_token="acme",
        title="Account Manager",
        location="Remote",
        absolute_url="https://example.com/jobs/" + job_id,
        relevance_score=score,
        match_reasons=[],
        disqualifiers=[],
        recommend=False,
        skipped=skip_reason is not None,
        skip_reason=skip_reason,
        scored_at=datetime.now(timezone.utc),
        source_run_id="run-1",
    )


# --- the flag itself -------------------------------------------------------

def test_the_cli_receives_the_schema_itself_not_a_path(monkeypatch):
    """`--json-schema` parses its argument as JSON. Passing a filename failed every
    call with `Unexpected identifier "C"` — the drive letter of C:\\Users\\..."""
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv

        class _Proc:
            returncode = 0

            async def communicate(self, input=None):
                payload = ScoringSchema(
                    requirements=[],
                    core_skills_rationale="ok", core_skills_band=5,
                    experience_rationale="ok", experience_band=4,
                    education_rationale="ok", education_band=1,
                    match_reasons=[], disqualifiers=[], recommend=True,
                ).model_dump_json()
                return json.dumps({"result": payload}).encode(), b""

        return _Proc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    settings = MatcherSettings(request_interval_s=0)
    backend = ClaudeCodeBackend(settings, asyncio.Semaphore(1))
    asyncio.run(backend.call("prompt", "system"))

    argv = list(captured["argv"])
    value = argv[argv.index("--json-schema") + 1]
    assert json.loads(value)["type"] == "object", "the CLI must get the schema, not a path"


_DLL_INIT_FAILED = 3221225794  # 0xC0000142, as Windows reports it


# A real failure envelope, as the CLI wrote it on 2026-09-22. The shape is the point:
# `usage` and the session bookkeeping come first and outrun any clip on the raw text,
# so `result` — the only part that says anything — was what got cut off.
_FAILURE_ENVELOPE = json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "duration_api_ms": 0,
    "stop_reason": "stop_sequence",
    "session_id": "8229e809-b3c5-4ab5-a69d-46f18f79f231",
    "total_cost_usd": 0,
    "usage": {
        "input_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "output_tokens": 0,
        "output_tokens_details": {"thinking_tokens": 0},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard", "iterations": [], "speed": "standard",
    },
    "is_error": True,
    "result": "Claude AI usage limit reached|1758598800",
}).encode()


def _scripted_backend(monkeypatch, exit_codes: list[int], fail_stdout: bytes = b""):
    """A backend whose CLI exits with each code in turn, then succeeds."""
    calls: list[int] = []

    async def fake_exec(*argv, **kwargs):
        rc = exit_codes[len(calls)] if len(calls) < len(exit_codes) else 0
        calls.append(rc)

        class _Proc:
            returncode = rc

            async def communicate(self, input=None):
                if rc:
                    return fail_stdout, b""
                payload = ScoringSchema(
                    requirements=[],
                    core_skills_rationale="ok", core_skills_band=5,
                    experience_rationale="ok", experience_band=4,
                    education_rationale="ok", education_band=1,
                    match_reasons=[], disqualifiers=[], recommend=True,
                ).model_dump_json()
                return json.dumps({"result": payload}).encode(), b""

        return _Proc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(scorer_mod, "_LAUNCH_RETRY_DELAYS_S", (0, 0, 0))
    backend = ClaudeCodeBackend(MatcherSettings(request_interval_s=0), asyncio.Semaphore(1))
    return backend, calls


def test_a_launch_failure_is_retried(monkeypatch):
    """The host refusing to start claude.exe is not an answer from the backend."""
    backend, calls = _scripted_backend(monkeypatch, [_DLL_INIT_FAILED] * 2)
    result = asyncio.run(backend.call("prompt", "system"))
    assert isinstance(result, ScoringSchema)
    assert len(calls) == 3


def test_a_persistent_launch_failure_gives_up_and_says_why(monkeypatch):
    backend, calls = _scripted_backend(monkeypatch, [_DLL_INIT_FAILED] * 10)
    with pytest.raises(CLILaunchError, match="0xC0000142"):
        asyncio.run(backend.call("prompt", "system"))
    assert len(calls) == len(scorer_mod._LAUNCH_RETRY_DELAYS_S) + 1


def test_an_ordinary_exit_is_never_retried(monkeypatch):
    """Exit 1 carries the API's own answer; asking again changes nothing."""
    backend, calls = _scripted_backend(monkeypatch, [1] * 10)
    with pytest.raises(RuntimeError, match="exited 1") as info:
        asyncio.run(backend.call("prompt", "system"))
    assert not isinstance(info.value, CLILaunchError)
    assert len(calls) == 1


def test_a_failed_call_reports_the_reason_the_cli_gave(monkeypatch):
    """The envelope says why. Raw text clipped for the log never reached that far."""
    backend, _ = _scripted_backend(monkeypatch, [1], fail_stdout=_FAILURE_ENVELOPE)
    with pytest.raises(RuntimeError, match="usage limit reached") as info:
        asyncio.run(backend.call("prompt", "system"))
    assert "api_ms=0" in str(info.value), "the tell that the call never reached the model"


def test_a_failure_envelope_is_read_field_by_field():
    detail = claude_cli.envelope_failure(_FAILURE_ENVELOPE.decode())
    assert "is_error=true" in detail
    assert "subtype=error_during_execution" in detail
    assert "api_ms=0" in detail
    assert "result: Claude AI usage limit reached|1758598800" in detail


def test_api_time_is_named_only_when_there_was_none():
    """Zero says the call never reached the model; any other value says nothing."""
    ran = json.dumps({"is_error": True, "duration_api_ms": 77652, "result": "boom"})
    assert "api_ms" not in claude_cli.envelope_failure(ran)


def test_an_older_envelope_keeps_its_error_key():
    nested = json.dumps({"is_error": True, "error": {"message": "credit balance too low"}})
    assert "result: credit balance too low" in claude_cli.envelope_failure(nested)


def test_the_reason_is_one_line_and_bounded():
    """It goes in a log line, beside a job title, whatever the CLI wrapped."""
    detail = claude_cli.envelope_failure(
        json.dumps({"is_error": True, "result": "line one\n\n  line two " + "x" * 500})
    )
    assert "\n" not in detail
    assert "line one line two" in detail
    assert len(detail) < 350


@pytest.mark.parametrize("raw", [
    "not json at all",
    "[]",
    json.dumps({"type": "result", "duration_api_ms": 12, "session_id": "abc"}),
])
def test_an_envelope_with_nothing_to_say_defers_to_the_raw_streams(raw):
    """None is what makes the caller fall back, so a silent envelope must not
    answer with an empty string."""
    assert claude_cli.envelope_failure(raw) is None


@pytest.mark.parametrize("rc", [3221225794, -1073741502])
def test_both_spellings_of_the_launch_failure_are_recognised(rc):
    assert claude_cli.is_launch_failure(rc)
    assert "0xC0000142" in claude_cli.describe_exit(rc)


def test_ordinary_exit_codes_are_described_as_themselves():
    assert not claude_cli.is_launch_failure(1)
    assert not claude_cli.is_launch_failure(None)
    assert claude_cli.describe_exit(1) == "1"


def test_the_schema_stays_well_inside_the_argv_limit():
    """The schema rides on argv now. Windows caps a command line at 32,767 chars;
    stay an order of magnitude clear of it."""
    assert len(json.dumps(ScoringSchema.model_json_schema())) < 8000


# --- errors must not retire jobs -------------------------------------------

@pytest.mark.parametrize("reason", sorted(SCORING_ERROR_SKIP_REASONS))
def test_a_scoring_failure_does_not_retire_a_job(reason):
    """A job may be retired on a verdict, never on an error — otherwise fixing the
    backend cannot bring back what it failed on."""
    assert reason in matcher_mod._RETRYABLE_SKIP_REASONS


@pytest.mark.parametrize("reason", ["no_content_text", "llm_skipped", None])
def test_verdicts_still_retire_a_job(reason):
    assert reason not in matcher_mod._RETRYABLE_SKIP_REASONS


def _posting(db: Database, job_id: str, run_id: str = "run-1") -> Job:
    """Scrape one posting, and hand it back so it can be asked about.

    `SeenStore` is keyed `(board_token, job_id)` and tests membership with the object
    rather than the tuple, so a test needs the `Job` itself — and `upsert_match` and
    `mark_seen` are both UPDATEs onto the row the scraper makes, so there has to be
    one.
    """
    now = datetime.now(timezone.utc)
    job = Job(
        source="greenhouse", board_token="acme", job_id=job_id,
        title="Account Manager", location=Location(name="Remote"),
        absolute_url=f"https://example.com/{job_id}",  # type: ignore[arg-type]
        updated_at=now, scraped_at=now, content_text="text",
    )
    db.insert_jobs(run_id, [job])
    return job


def test_seen_store_releases_jobs_retired_by_a_scoring_error(tmp_path):
    db = Database(tmp_path / "test.db")
    now = datetime.now(timezone.utc).isoformat()
    jobs = {}
    for job_id, skipped, reason in [
        ("failed", 1, "api_error"),
        ("judged", 0, None),
        ("empty", 1, "no_content_text"),
    ]:
        jobs[job_id] = _posting(db, job_id)
        db.upsert_match(
            run_id="run-1", job_id=job_id, board_token="acme", title="Account Manager",
            relevance_score=0, shortlisted=False, skipped=bool(skipped),
            skip_reason=reason, source_run_id="run-1", scored_at=now, raw_json="{}",
        )
    db.mark_seen([("acme", j) for j in jobs])

    seen = SeenStore(db=db)

    assert jobs["failed"] not in seen, \
        "a job the backend failed on must become eligible again"
    assert jobs["judged"] in seen and jobs["empty"] in seen, \
        "real outcomes must still retire a job"


def test_a_job_scored_successfully_later_is_not_released(tmp_path):
    """One bad run followed by a good one must not un-retire the job.

    The posting holds one verdict, so the good run's score *replaces* the failure
    rather than sitting beside it — which is what `forget_seen_scoring_errors` now
    reads. It used to need an `EXCEPT` over a second `matches` row to reach the same
    answer.
    """
    db = Database(tmp_path / "test.db")
    now = datetime.now(timezone.utc).isoformat()
    job = _posting(db, "j1")
    for run_id, skipped, reason in [("run-1", 1, "api_error"), ("run-2", 0, None)]:
        db.upsert_match(
            run_id=run_id, job_id="j1", board_token="acme", title="Account Manager",
            relevance_score=80, shortlisted=True, skipped=bool(skipped),
            skip_reason=reason, source_run_id=run_id, scored_at=now, raw_json="{}",
        )
    db.mark_seen([("acme", "j1")])

    assert job in SeenStore(db=db)


# --- the circuit breaker ---------------------------------------------------

def test_the_breaker_trips_after_five_consecutive_failures():
    breaker = matcher_mod._ScoringBreaker()
    for i in range(4):
        breaker.record(_result(f"j{i}", "api_error"))
        assert not breaker.tripped

    breaker.record(_result("j4", "api_error"))
    assert breaker.tripped


def test_a_success_resets_the_breaker():
    """A backend that fails occasionally must not abort a run that is working."""
    breaker = matcher_mod._ScoringBreaker()
    for i in range(4):
        breaker.record(_result(f"j{i}", "api_error"))
    breaker.record(_result("good", None, score=80))
    for i in range(4):
        breaker.record(_result(f"k{i}", "api_error"))

    assert not breaker.tripped


def test_budget_drops_do_not_trip_the_breaker():
    """Over-budget jobs are skipped in their thousands and say nothing about the
    backend's health."""
    breaker = matcher_mod._ScoringBreaker()
    for i in range(20):
        breaker.record(_result(f"j{i}", matcher_mod.BUDGET_SKIP_REASON))

    assert not breaker.tripped


def test_the_breaker_summary_names_the_error_and_promises_a_retry():
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "claude CLI exited 1: --json-schema is not valid JSON"
    for i in range(5):
        breaker.record(_result(f"j{i}", "api_error"))

    summary = breaker.summary()
    assert "--json-schema" in summary
    assert "rescored" in summary
    assert "machine" not in summary


def test_a_breaker_tripped_by_launch_failures_blames_the_machine():
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "claude CLI exited 3221225794 (0xC0000142 STATUS_DLL_INIT_FAILED)"
    breaker.launch_failed = True
    for i in range(5):
        breaker.record(_result(f"j{i}", "api_error"))

    summary = breaker.summary()
    assert breaker.tripped
    assert "0xC0000142" in summary and "machine" in summary
    assert "No jobs were retired" in summary


def test_the_scorer_flags_a_launch_failure_for_the_breaker():
    """The flag the breaker reads must follow the most recent failure, either way."""
    class _Backend:
        def __init__(self, exc):
            self.exc = exc

        async def call(self, prompt, system_prompt):
            raise self.exc

    from hireshire.matcher.scorer import JobScorer
    from hireshire.models.job import Job

    now = datetime.now(timezone.utc)
    job = Job(
        source="greenhouse", board_token="acme", title="Account Manager", job_id="j1",
        location={"name": "Remote"}, absolute_url="https://example.com/j1",
        updated_at=now, scraped_at=now, content_text="Sell things.",
    )
    scorer = JobScorer(MatcherSettings(), _Backend(CLILaunchError("exited 0xC0000142")))
    result = asyncio.run(scorer.score(job, "resume", "run-1"))
    assert result.skip_reason == "api_error" and scorer.last_error_was_launch

    scorer._backend = _Backend(RuntimeError("claude CLI exited 1"))
    asyncio.run(scorer.score(job, "resume", "run-1"))
    assert not scorer.last_error_was_launch


# --- the console the judge's child runs in ---------------------------------


def test_every_judge_call_gets_its_own_console(monkeypatch):
    """A judge child must not inherit the sweeper's console.

    Once that console goes stale -- the Claude Code session that launched the
    background shell task ended or restarted -- every inherited child dies at DLL init
    with 0xC0000142 and the sweep scores nothing for hours while scraping happily.
    `tests/test_child_console.py` carries the mechanism and the evidence.
    """
    monkeypatch.setattr(claude_cli.sys, "platform", "win32")
    backend, _ = _scripted_backend(monkeypatch, [])
    captured: dict = {}

    inner = asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        captured.update(kwargs)
        return await inner(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    asyncio.run(backend.call("prompt", "system"))
    assert captured.get("creationflags") == 0x08000000


# --- teardown: a judge child must not outlive the call that started it ------


class _HangingProc:
    """A CLI that never answers and records being killed."""

    returncode = None

    def __init__(self, drain_hangs: bool = False) -> None:
        self.killed = False
        self._drain_hangs = drain_hangs

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self, input=None):
        if self.killed and not self._drain_hangs:
            return b"", b""
        await asyncio.Event().wait()   # never returns


def _hanging_backend(monkeypatch, proc):
    async def fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return ClaudeCodeBackend(MatcherSettings(request_interval_s=0), asyncio.Semaphore(1))


def test_a_cancelled_scoring_call_kills_the_cli_it_started(monkeypatch):
    """`run_pipeline` cancels every sibling stage as soon as one raises, so a sweep that
    dies mid-scoring used to leave up to `matcher.concurrency` CLI processes running with
    their pipes open and nobody to reap them. The cancellation must still propagate --
    the teardown is not allowed to swallow it."""
    proc = _HangingProc()
    backend = _hanging_backend(monkeypatch, proc)

    async def main():
        task = asyncio.ensure_future(backend.call("prompt", "system"))
        await asyncio.sleep(0)          # let it reach communicate()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert proc.killed, "a cancelled scoring call must kill the CLI it started"


def test_a_timed_out_scoring_call_cannot_hang_on_its_own_teardown(monkeypatch):
    """The timeout path used to `await proc.communicate()` with no bound, on a process
    it had just killed and which might not die. The drain is bounded now, and the
    timeout error it raises is unchanged."""
    proc = _HangingProc(drain_hangs=True)
    backend = _hanging_backend(monkeypatch, proc)
    monkeypatch.setattr(scorer_mod, "_REAP_DRAIN_S", 0.01)
    settings = MatcherSettings(request_interval_s=0, claude_cli_timeout_s=1)
    backend._timeout = 0.01

    with pytest.raises(RuntimeError, match="timed out after"):
        asyncio.run(backend.call("prompt", "system"))
    assert proc.killed
    assert settings.claude_cli_timeout_s == 1   # the setting itself is untouched


# --- what the breaker says when the host is the problem ---------------------


def test_the_launch_failure_summary_names_the_stale_console_and_the_cure():
    """It used to offer three possibilities, two of which are now ruled out: the 21:05
    sweep ran almost entirely locked and made ~30 apply sessions and 166 codex calls
    before failing, and there were no power events. Naming a cause the user cannot act
    on sends them to their login instead of restarting the sweep."""
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "codex CLI exited 3221225794 (0xC0000142 STATUS_DLL_INIT_FAILED)"
    breaker.launch_failed = True
    for i in range(5):
        breaker.record(_result(f"j{i}", "api_error"))

    summary = breaker.summary()
    for phrase in ("0xC0000142", "machine", "console", "restart", "No jobs were retired"):
        assert phrase in summary, phrase
    assert "asleep" not in summary and "locked" not in summary


def test_a_breaker_tripped_by_a_launch_failure_logs_one_findable_line(caplog):
    """A log search for the exit code has to land on the explanation, not only on the
    per-call warnings that repeat it."""
    breaker = matcher_mod._ScoringBreaker()
    breaker.last_error = "codex CLI exited 3221225794 (0xC0000142 STATUS_DLL_INIT_FAILED)"
    breaker.launch_failed = True
    with caplog.at_level("ERROR"):
        for i in range(5):
            breaker.record(_result(f"j{i}", "api_error"))

    explained = [r for r in caplog.records
                 if "0xC0000142" in r.getMessage() and "restart" in r.getMessage()]
    assert len(explained) == 1, "exactly one line should explain the stale console"
