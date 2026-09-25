"""The streaming applier: one `claude -p` session per shortlisted job, as it arrives.

What is pinned here, each for a failure that costs the user something real:

* the prompt never rides on argv (a leading `---` once failed every apply phase);
* the subscription is used, not a pay-as-you-go key;
* a verdict retires a job, a failed launch does not — or a broken MCP server would
  lose a whole sweep's shortlist for good;
* an ambiguous ending (timeout, unreadable result) *is* recorded, because the form may
  already be submitted and retrying would apply twice;
* nothing is launched for excluded employers or jobs already applied to — but an
  excluded employer is still recorded, so the user is told to apply by hand;
* the backlog's window closing is recorded too, and only ever on the window — a host
  that could start no session at all retires nothing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hireshire.applier import config as applier_config
from hireshire.applier import reasons
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


@pytest.fixture(autouse=True)
def workspace(monkeypatch):
    """No workspace unless a test sets one — never whatever the dev config says."""
    ws: dict = {"dir": None}
    monkeypatch.setattr(worker.paths, "workspace_dir", lambda: ws["dir"])
    return ws


def _settings(tmp_path, **over) -> ApplierSettings:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    base = dict(
        enable_applier=True, resume_path=str(resume), inter_job_delay_s=0,
        apply_timeout_s=5, exclude_companies=["Google"], first_name="Ada",
        last_name="Lovelace", email="ada@example.com", phone="555",
    )
    base.update(over)
    return ApplierSettings(**base)


def _job(job_id: str, company: str = "acme") -> dict:
    return {"job_id": job_id, "title": "Account Manager", "company": company,
            "job_url": f"https://example.com/jobs/{job_id}"}


def _run_dir(tmp_path, stamp: str = "2026-09-19_120000") -> Path:
    """This sweep's results folder — what `paths.make_run_dir` hands the worker."""
    d = tmp_path / "hireshire_run_results" / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _applied(tmp_path, stamp: str = "2026-09-19_120000") -> Path:
    return _run_dir(tmp_path, stamp) / "applied"


def _run(tmp_path, jobs, settings=None, db=None, backlog=False, run_dir=None):
    db = db or Database(tmp_path / "test.db")
    settings = settings or _settings(tmp_path)
    run_dir = run_dir if run_dir is not None else _run_dir(tmp_path)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        for j in jobs:
            await q.put(j)
        await q.put(None)
        return await worker.run_apply_worker(
            q, settings, "RESUME TEXT", run_dir=run_dir, db=db, include_backlog=backlog
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
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    shipped = json.loads((worker.paths.ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["playwright"]
    assert server["command"] == shipped["mcpServers"]["playwright"]["command"]
    args = server["args"]
    assert args[:len(shipped["mcpServers"]["playwright"]["args"])] == \
        shipped["mcpServers"]["playwright"]["args"]
    assert args[args.index("--output-dir") + 1] == str(_applied(tmp_path) / ".browser" / "j1")
    # The per-job rules must name the tools as that server exposes them.
    assert "mcp__playwright__browser_navigate" in worker.PROMPT_PATH.read_text(encoding="utf-8")


def _inputs(call) -> dict:
    """The job's JSON block out of the prompt a session was sent."""
    sent = call["proc"].sent.decode("utf-8")
    return json.loads(sent.split("## This job\n\n```json\n", 1)[1].split("\n```", 1)[0])


def test_the_session_runs_in_the_workspace_so_the_resume_can_be_uploaded(
        tmp_path, launcher, workspace):
    """Playwright MCP refuses uploads outside the session's cwd. Running under DATA
    while the resume sat in the workspace refused 5 of 8 forms on 2026-09-18, with
    nothing submitted."""
    ws = tmp_path / "ws"
    resume = ws / "resume" / "original" / "cv.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-1.4")
    workspace["dir"] = ws
    run_dir = _run_dir(ws)

    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")], run_dir=run_dir,
         settings=_settings(tmp_path, resume_path=str(resume)))

    (call,) = calls
    assert call["kwargs"]["cwd"] == str(ws)
    inputs = _inputs(call)
    assert inputs["resume_path"] == str(resume), "a resume already inside cwd is not copied"
    shot = Path(inputs["screenshot_path"])
    assert shot.parent == run_dir / "applied", "the screenshot belongs to this sweep"
    assert shot.parent.is_dir()
    argv = list(call["argv"])
    out_dir = json.loads(argv[argv.index("--mcp-config") + 1])[
        "mcpServers"]["playwright"]["args"][-1]
    assert out_dir == str(shot.parent / ".browser" / "j1")
    assert Path(out_dir).is_relative_to(ws), "the server refuses to write outside cwd"


def _write_configs(tmp_path, scraper_yaml: str) -> Path:
    applier = tmp_path / "applier.yaml"
    applier.write_text("settings:\n  enable_applier: true\n", encoding="utf-8")
    (tmp_path / "scraper.yaml").write_text(scraper_yaml, encoding="utf-8")
    return applier


@pytest.mark.parametrize("yaml_text, expected", [
    ("settings:\n  location_filter:\n    - united states\n    - india\n",
     ["united states", "india"]),
    ("settings:\n  location_filter: []\n", []),                    # no check at all
    ("settings:\n  location_filter: remote\n", ["remote"]),        # a bare string
    ("settings:\n  location_filter:\n    - ' usa '\n    - '  '\n", ["usa"]),
    ("settings: {}\n", []),                                        # key absent
    ("settings:\n  location_filter: 7\n", []),                     # not a list
    ("this: [is: not: yaml\n", []),                                # unreadable
])
def test_the_location_list_comes_from_the_scraper(tmp_path, monkeypatch, yaml_text,
                                                  expected):
    """One list, and the scraper owns it. A malformed or missing file must leave the
    applier checking nothing rather than failing to load — the same trade
    `reporting.data._matcher_settings` makes."""
    applier = _write_configs(tmp_path, yaml_text)
    monkeypatch.setattr(applier_config.paths, "config_file", lambda name: tmp_path / name)

    cfg = applier_config.load_applier_config(applier)
    assert cfg.settings.location_filter == expected


def test_a_location_list_in_applier_yaml_loses_to_the_scraper(tmp_path, monkeypatch):
    """The field is derived, not user-set. A stale copy in `applier.yaml` — hand-edited,
    or left by an install that wrote it — must not be what the session is told."""
    applier = tmp_path / "applier.yaml"
    applier.write_text(
        "settings:\n  enable_applier: true\n  location_filter: [mars]\n",
        encoding="utf-8")
    (tmp_path / "scraper.yaml").write_text(
        "settings:\n  location_filter: [india]\n", encoding="utf-8")
    monkeypatch.setattr(applier_config.paths, "config_file", lambda name: tmp_path / name)

    assert applier_config.load_applier_config(applier).settings.location_filter == ["india"]


def test_the_session_is_given_the_users_own_location_list(tmp_path, launcher):
    """`apply_one.md` used to hardcode six strings and never read config, so a job
    scraped as `Arlington, VA, United States` and rendered as `Arlington, VA` was
    skipped against a list the user never wrote. There is one list now, and this is
    the only path it reaches the session by."""
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")],
         settings=_settings(tmp_path, location_filter=["united states", "india"]))

    assert _inputs(calls[0])["accepted_locations"] == ["united states", "india"]


def test_an_empty_location_list_still_builds_a_prompt(tmp_path, launcher):
    """Empty means no check at all, matching the scraper. The branch itself is prose
    in the skill, so what is pinned here is that the key arrives empty rather than
    missing — an absent key reads to the session as "no list given"."""
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")], settings=_settings(tmp_path, location_filter=[]))

    assert _inputs(calls[0])["accepted_locations"] == []


# --- the browser server's own output -----------------------------------------

def _output_dir(call) -> Path:
    args = json.loads(call["argv"][list(call["argv"]).index("--mcp-config") + 1])[
        "mcpServers"]["playwright"]["args"]
    return Path(args[args.index("--output-dir") + 1])


def _writes_snapshots(proc: _Proc, seen: dict) -> _Proc:
    """Wrap a fake session so it writes what Playwright MCP writes unasked, and notes
    whether its scratch dir and the `applied` row existed while it ran."""
    inner = proc.communicate

    async def communicate(payload=None):
        out = seen["dir"]()
        (out / "page-2026-09-19T19-11-50-406Z.yml").write_text("- main")
        (out / "console-2026-09-19T19-11-49-722Z.log").write_text("[ERROR] 401")
        seen["existed"] = out.is_dir()
        return await inner(payload)

    proc.communicate = communicate
    return proc


def test_snapshots_and_console_logs_are_deleted_once_the_outcome_is_recorded(
        tmp_path, launcher, monkeypatch):
    """Playwright MCP writes a `page-*.yml` per snapshot and a `console-*.log` into
    `--output-dir` whether or not anything asks — 204 and 20 beside 7 screenshots on
    one sweep. The session may read them while it fills the form, so they must exist
    until it exits, and be gone only after the `applied` row is written."""
    calls, script, _ = launcher
    seen: dict = {"dir": lambda: _output_dir(calls[-1])}
    script.append(_writes_snapshots(_outcome(status="submitted"), seen))

    order: list[str] = []
    real_record = Database.record_applied

    def record(self, *a, **k):
        order.append("recorded" if _output_dir(calls[0]).is_dir() else "already deleted")
        return real_record(self, *a, **k)

    monkeypatch.setattr(Database, "record_applied", record)
    _, db = _run(tmp_path, [_job("j1")])

    assert seen["existed"], "the session had nowhere to write its snapshots"
    assert order == ["recorded"], "output was deleted before the outcome was recorded"
    assert _statuses(db) == {"j1": "submitted"}
    applied = _applied(tmp_path)
    assert not _output_dir(calls[0]).exists()
    assert not (applied / ".browser").exists()
    assert not list(applied.glob("*.yml")) and not list(applied.glob("*.log"))


@pytest.mark.parametrize("ending", ["timeout", "launch_failure"])
def test_output_is_deleted_however_the_session_ends(tmp_path, launcher, ending):
    calls, script, _ = launcher
    seen: dict = {"dir": lambda: _output_dir(calls[-1])}
    proc = _Proc(hang=True) if ending == "timeout" else _Proc(rc=1)
    script.append(_writes_snapshots(proc, seen))
    _run(tmp_path, [_job("j1")], settings=_settings(tmp_path, apply_timeout_s=0.05))

    assert seen["existed"]
    assert not (_applied(tmp_path) / ".browser").exists()


def test_leftover_output_is_cleared_and_screenshots_are_kept(tmp_path, launcher):
    """What a killed session left in this run's folder: loose snapshots and logs, and
    a `.browser/` it never deleted. Screenshots and the copied resume beside them are
    the user's and must survive."""
    applied = _applied(tmp_path)
    (applied / ".browser" / "old").mkdir(parents=True)
    (applied / ".browser" / "old" / "page-x.yml").write_text("- main")
    (applied / "page-2026-09-19T19-11-50-406Z.yml").write_text("- main")
    (applied / "console-2026-09-19T19-11-49-722Z.log").write_text("[ERROR]")
    (applied / "roblox-8083944.png").write_bytes(b"\x89PNG")
    (applied / "notes.log").write_text("not ours")

    _run(tmp_path, [_job("j1")])

    left = sorted(p.name for p in applied.iterdir())
    assert left == ["notes.log", "resume.pdf", "roblox-8083944.png"]


def test_a_resume_outside_the_workspace_is_copied_into_it(tmp_path, launcher, workspace):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace["dir"] = ws

    calls, _, _ = launcher
    # resume at tmp_path/resume.pdf, outside the workspace the session runs in
    _run(tmp_path, [_job("j1"), _job("j2")], run_dir=_run_dir(ws))

    uploaded = Path(_inputs(calls[0])["resume_path"])
    assert uploaded.is_relative_to(ws) and uploaded.read_bytes() == b"%PDF-1.4"
    assert _inputs(calls[1])["resume_path"] == str(uploaded)


def test_without_a_workspace_the_session_runs_in_the_runs_applied_folder(
        tmp_path, launcher):
    """Installs predating `workspace_dir`, where the run folder is under DATA. The
    session must still stay out of ROOT, which every plugin update replaces, and the
    resume is copied in so the upload is inside the session's roots."""
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])

    (call,) = calls
    assert call["kwargs"]["cwd"] == str(_applied(tmp_path))
    uploaded = Path(_inputs(call)["resume_path"])
    assert uploaded == _applied(tmp_path) / "resume.pdf" and uploaded.is_file()


def test_a_run_folder_outside_the_workspace_moves_the_session_with_it(
        tmp_path, launcher, workspace):
    """`make_run_dir` falls back to DATA when the workspace folder has gone missing or
    is unwritable, and the config still names one. The session must follow the run
    folder: the browser server refuses to write outside cwd, so a cwd that no longer
    contains `out_dir` would lose every screenshot."""
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace["dir"] = ws

    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])          # run dir under tmp_path, not under ws

    (call,) = calls
    assert call["kwargs"]["cwd"] == str(_applied(tmp_path))
    assert Path(_inputs(call)["screenshot_path"]).parent == _applied(tmp_path)


def test_each_run_keeps_its_own_applied_folder(tmp_path, launcher):
    """The whole point: one shared folder left the user's only record of each form in
    a pile with no way to tell which sweep it came from."""
    calls, _, _ = launcher
    first = _run_dir(tmp_path, "2026-09-19_120000")
    second = _run_dir(tmp_path, "2026-09-19_160000")
    db = Database(tmp_path / "test.db")
    _run(tmp_path, [_job("j1")], db=db, run_dir=first)
    # A screenshot the first sweep's session took, which the second must not disturb.
    (first / "applied" / "acme-j1.png").write_bytes(b"\x89PNG")
    _run(tmp_path, [_job("j2")], db=db, run_dir=second)

    shots = [Path(_inputs(c)["screenshot_path"]).parent for c in calls]
    assert shots == [first / "applied", second / "applied"]
    assert (first / "applied" / "acme-j1.png").is_file()


def test_the_session_keeps_the_tools_the_judge_strips(tmp_path, launcher):
    """The scorer runs `claude -p --safe-mode --tools ""`, and `hireshire/claude_cli.py`
    is the obvious place to "share" those flags. It is the wrong one: `--safe-mode`
    disables MCP servers and `--tools ""` removes the built-ins, so either would leave
    this session with no browser and every application would fail."""
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])
    argv = list(calls[0]["argv"])

    assert "--safe-mode" not in argv
    assert "--tools" not in argv


# --- verdicts ---------------------------------------------------------------

def test_submitted_and_error_outcomes_are_recorded(tmp_path, launcher):
    _, script, _ = launcher
    script += [_outcome(status="submitted", screenshot="/tmp/j1.png"),
               _outcome(status="error", error="Sign in to apply")]
    stats, db = _run(tmp_path, [_job("j1"), _job("j2")])

    assert _statuses(db) == {"j1": "submitted", "j2": "error"}
    assert stats["submitted"] == 1 and stats["error"] == 1


def test_a_question_aimed_at_bots_is_handed_to_the_user():
    """A form that asks "are you a bot?" or tells an AI to type a word is asking who is
    filling it in. Answering either way misrepresents the application or gets it
    flagged, so the session stops and the job lands under Needs Attention with one
    fixed line. The rule is prose, so what is pinned is that the prompt carries it."""
    prompt = " ".join(worker.PROMPT_PATH.read_text(encoding="utf-8").split())

    assert "Manual application required." in prompt
    assert "whether you are a bot, an AI" in prompt
    assert "do not submit" in prompt


def test_the_prompt_spells_every_needs_attention_label_exactly():
    """Needs Attention prints one fixed label per cause, and the session writes most of
    them. `reasons` is where they are spelled, so the prompt must carry each one
    verbatim — a label reworded in one place and not the other would put two wordings
    of one cause back on the page."""
    prompt = " ".join(worker.PROMPT_PATH.read_text(encoding="utf-8").split())

    for label in (reasons.HUMAN_VERIFICATION, reasons.MANUAL_REQUIRED,
                  reasons.SUBMIT_UNCONFIRMED, reasons.POSTING_CLOSED, reasons.REJECTED,
                  reasons.NOT_A_JOB, reasons.COVER_LETTER_OFF,
                  f"{reasons.REQUIRED_QUESTION}: <topic>"):
        assert f"`{label}`" in prompt, label
    for topic, _ in reasons.TOPICS:
        assert f"`{topic}`" in prompt, topic
    assert "verification code" in prompt and "CAPTCHA" in prompt


def test_a_location_skip_retires_the_job(tmp_path, launcher):
    """It used to be counted and dropped on the floor: no `applied` row and still
    shortlisted, so `load_pending_applications` re-queued it every sweep for the whole
    `backlog_hours` window — one browser session each time to re-read a location that
    cannot change. A location is a verdict, so the job is un-shortlisted instead.

    Still no `applied` row, unlike an excluded employer: there is nothing for the user
    to do about a job in the wrong country, so it belongs under Jobs Filtered rather
    than Needs Attention. And `skipped` must stay falsy — the judge really did read
    this posting, and setting it would blank a real score in the results CSV."""
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location", location="London, UK"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    stats, _ = _run(tmp_path, [_job("j1")], db=db)

    assert _statuses(db) == {} and stats["skipped_location"] == 1
    (row,) = db.load_all_matches("r0")
    assert row["shortlisted"] is False
    assert row["skip_reason"] == worker.LOCATION_SKIP_REASON
    assert not row.get("skipped"), "a judged job must keep its score"
    assert row["relevance_score"] == 80
    assert row["applier_location"] == "London, UK"


def test_a_retired_location_skip_leaves_the_backlog(tmp_path, launcher):
    """The whole point of the change: the only thing that stopped it being re-driven
    every sweep for `backlog_hours`."""
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location", location="Berlin"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    _run(tmp_path, [_job("j1")], db=db)

    since = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    assert db.load_pending_applications(since) == []


def test_a_location_skip_retires_the_job_in_every_run_that_shortlisted_it(
        tmp_path, launcher):
    """`matches` is keyed `(run_id, job_id)` and a backlog job's row belongs to an
    earlier sweep, so `mark_not_shortlisted` takes no `run_id`. Updating only one row
    would leave the other shortlisted — back in the backlog, and rendered a second
    time on the lifetime page under a contradicting label."""
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location", location="Paris"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    _match(db, "r1", "j1", score=75)
    _run(tmp_path, [_job("j1")], db=db)

    for run_id in ("r0", "r1"):
        (row,) = db.load_all_matches(run_id)
        assert row["shortlisted"] is False, run_id
        assert row["skip_reason"] == worker.LOCATION_SKIP_REASON, run_id


def test_retiring_a_job_twice_is_a_no_op(tmp_path):
    """`AND shortlisted = 1` makes it idempotent, so a job reached again through some
    other path does not churn rows or overwrite a later verdict."""
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")

    assert db.mark_not_shortlisted("j1", worker.LOCATION_SKIP_REASON, "London") == 1
    assert db.mark_not_shortlisted("j1", worker.LOCATION_SKIP_REASON, "Berlin") == 0
    (row,) = db.load_all_matches("r0")
    assert row["applier_location"] == "London"


def test_a_location_skip_with_no_location_text_still_retires_the_job(tmp_path, launcher):
    """`location` is optional on the outcome, and a session that omits it must not
    leave the job looping. The page just renders the bare label."""
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    _run(tmp_path, [_job("j1")], db=db)

    (row,) = db.load_all_matches("r0")
    assert row["shortlisted"] is False
    assert "applier_location" not in row


def test_excluded_and_already_applied_jobs_launch_nothing(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    db.record_applied("j2", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
                      "submitted", None, None)
    stats, _ = _run(tmp_path, [_job("j1", company="google"), _job("j2")], db=db)
    assert calls == []
    assert stats["excluded"] == 1


def test_an_excluded_employer_is_recorded_so_it_needs_attention(tmp_path, launcher):
    """It used to be dropped with only a log line: the job sat under Jobs Shortlisted
    as though the applier would get to it, and came back through the backlog every
    sweep for `backlog_hours`. An account-login portal is a verdict — the answer is
    the same on every future sweep — so it is recorded like any other verdict, which
    is what puts it under Needs Attention and takes it out of the backlog."""
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    stats, _ = _run(tmp_path, [_job("j1", company="Google")], db=db)

    assert calls == [] and stats["excluded"] == 1
    (row,) = db.load_applied()
    assert row["job_id"] == "j1" and row["status"] == "excluded"
    assert row["error"] == worker.EXCLUDED_REASON
    assert row["error"].startswith("Requires human verification")
    assert "\n" not in row["error"], "Needs Attention prints this as one line"
    # Out of the backlog: the only thing that stopped it being re-dropped every sweep.
    since = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    assert db.load_pending_applications(since) == []


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


def test_a_windows_launch_failure_is_named_and_deferred(tmp_path, launcher, caplog):
    """0xC0000142 used to log as a bare 3221225794, which reads like an API error."""
    _, script, _ = launcher
    script.append(_Proc(rc=3221225794))
    with caplog.at_level("WARNING", logger=worker.logger.name):
        stats, db = _run(tmp_path, [_job("j1")])

    assert _statuses(db) == {} and stats["deferred"] == 1
    assert "0xC0000142 STATUS_DLL_INIT_FAILED" in caplog.text


def test_a_failed_session_logs_the_reason_the_cli_gave(tmp_path, launcher, caplog):
    """Nine `exited 1` deferrals in one night logged their token counts and no reason:
    the envelope's bookkeeping outran the clip before `result`, which is the only part
    that says anything. Those fields are read by name now."""
    _, script, _ = launcher
    envelope = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "duration_api_ms": 0,
        "session_id": "8229e809-b3c5-4ab5-a69d-46f18f79f231",
        "total_cost_usd": 0,
        "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard", "iterations": [], "speed": "standard",
        },
        "is_error": True,
        "result": "Claude AI usage limit reached|1758598800",
    }).encode()
    script.append(_Proc(envelope, rc=1))
    with caplog.at_level("WARNING", logger=worker.logger.name):
        stats, db = _run(tmp_path, [_job("j1")])

    assert "Claude AI usage limit reached" in caplog.text
    assert "api_ms=0" in caplog.text, "the tell that the session never reached the model"
    assert _statuses(db) == {} and stats["deferred"] == 1, "a failed launch retired a job"


def test_a_timeout_is_recorded_as_an_unconfirmed_error_and_kills_the_session(
        tmp_path, launcher):
    _, script, killed = launcher
    script.append(_Proc(hang=True))
    _, db = _run(tmp_path, [_job("j1")], settings=_settings(tmp_path, apply_timeout_s=0.05))

    (row,) = db.load_applied()
    assert row["status"] == "error"
    assert row["error"] == reasons.SUBMIT_UNCONFIRMED
    assert "\n" not in row["error"], "Needs Attention prints this as one line"
    assert killed, "the timed-out browser session was left running"


def test_an_unreadable_result_is_recorded_rather_than_retried(tmp_path, launcher):
    _, script, _ = launcher
    script.append(_Proc(b"not json at all"))
    _, db = _run(tmp_path, [_job("j1")])
    assert _statuses(db) == {"j1": "error"}
    (row,) = db.load_applied()
    assert row["error"] == reasons.SUBMIT_UNCONFIRMED


def test_the_prompt_carries_the_screening_answers_and_links(tmp_path):
    """Setup asks these once so a form's authorization, sponsorship and relocation
    questions stop ending in `error` (known issue A4). An unset answer goes through
    as null, which `apply_one.md` reads as "never asked"."""
    settings = _settings(
        tmp_path, linkedin_url="https://linkedin.com/in/ada",
        portfolio_url="https://ada.dev", work_authorized=True,
        requires_sponsorship=False,
    )
    dirs = worker.SessionDirs(cwd=tmp_path, out_dir=tmp_path,
                              resume_path=tmp_path / "resume.pdf")
    prompt = worker.build_prompt(_job("j1"), settings, dirs, "RESUME")
    inputs = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])

    applicant = inputs["applicant"]
    assert applicant["linkedin_url"] == "https://linkedin.com/in/ada"
    assert applicant["portfolio_url"] == "https://ada.dev"
    assert applicant["work_authorized"] is True
    assert applicant["requires_sponsorship"] is False
    assert applicant["willing_to_relocate"] is None


def test_the_prompt_carries_github_and_self_identification(tmp_path):
    """GitHub has its own box on many forms, and the EEO section is answered from the
    user's own setup answers. Unset answers go through as "" — decline."""
    settings = _settings(
        tmp_path, github_url="https://github.com/ada", gender="female",
        veteran_status="not_protected_veteran",
    )
    dirs = worker.SessionDirs(cwd=tmp_path, out_dir=tmp_path,
                              resume_path=tmp_path / "resume.pdf")
    prompt = worker.build_prompt(_job("j1"), settings, dirs, "RESUME")
    inputs = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])

    applicant = inputs["applicant"]
    assert applicant["github_url"] == "https://github.com/ada"
    assert applicant["self_identification"] == {
        "gender": "female", "race_ethnicity": "", "disability": "",
        "veteran_status": "not_protected_veteran",
    }


def test_cancelling_the_worker_kills_the_session_in_flight(tmp_path, launcher):
    calls, script, killed = launcher
    script.append(_Proc(hang=True))
    db = Database(tmp_path / "test.db")
    settings = _settings(tmp_path, apply_timeout_s=3600)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        await q.put(_job("j1"))
        task = asyncio.create_task(
            worker.run_apply_worker(q, settings, "r", run_dir=_run_dir(tmp_path),
                                    db=db, include_backlog=False)
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


def test_the_applier_bar_counts_every_streamed_job_but_not_the_backlog(
        tmp_path, launcher):
    """A deferral or a location skip writes no `applied` row, so a bar counting rows
    would stop short of the shortlist. The backlog belongs to earlier sweeps and must
    not push this sweep's bar past its own total."""
    db = Database(tmp_path / "test.db")
    db.start_progress("now", apply_enabled=True)
    _match(db, "r0", "old")                       # a backlog job from an earlier sweep

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        for j in (_job("j1"), _job("j2", company="Google")):
            await q.put(j)
        await q.put(None)
        return await worker.run_apply_worker(
            q, _settings(tmp_path), "RESUME TEXT", run_dir=_run_dir(tmp_path),
            db=db, include_backlog=True, run_id="now",
        )

    asyncio.run(go())
    assert len(launcher[0]) == 2                  # the backlog job and j1 launched
    assert db.run_progress("now")["apply_handled"] == 2


# --- the window the backlog closes ------------------------------------------

def _stale(db: Database, job_id: str = "stale", **over) -> None:
    """A shortlisted job the backlog can no longer see: scored before the window."""
    over.setdefault("scored_at", datetime.now(timezone.utc) - timedelta(days=10))
    _match(db, "r0", job_id, **over)


def _window_start() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()


@pytest.mark.parametrize("json1", [True, False])
def test_the_backlogs_two_halves_partition_the_unapplied_shortlist(tmp_path, json1):
    """Pending and expired are one set split on age, and nothing may fall between
    them: a job neither half returns is a job that is never applied to and never
    recorded, which is the silence this pair exists to end.

    `rescored` is why the age test is a `HAVING` on `MAX(scored_at)` rather than a
    `WHERE` on the row. It has a stale row beside a fresh one — `matches` is keyed
    `(run_id, job_id)`, so a retried scoring leaves both (known issue R1) — and a
    row-level test would report it as expired while the backlog was still retrying it.
    """
    db = Database(tmp_path / "test.db")
    db._has_json1 = json1

    _match(db, "r0", "fresh")
    _stale(db, "stale")
    _stale(db, "sibling", rep="fresh")            # judged by proxy, never applied to
    _stale(db, "done")
    db.record_applied("done", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
                      "error", None, None)
    _stale(db, "rejected", shortlisted=False)
    _stale(db, "rescored", score=70)              # the stale row that survives…
    _match(db, "r1", "rescored", score=75)        # …beside the fresh one

    pending = {r["job_id"] for r in db.load_pending_applications(_window_start())}
    expired = {r["job_id"] for r in db.load_expired_applications(_window_start())}

    assert pending == {"fresh", "rescored"}
    assert expired == {"stale"}
    assert not pending & expired


def test_the_window_closing_records_the_job_instead_of_dropping_it(tmp_path, launcher):
    """`backlog_hours` runs against `scored_at`, which never advances, so a job whose
    sessions keep failing to launch stops being retried. It used to stop silently —
    still shortlisted, no `applied` row, sitting under Jobs Shortlisted for good as
    though the applier would still get to it."""
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _stale(db)

    stats, _ = _run(tmp_path, [], db=db, backlog=True)

    assert calls == [], "a job past the window was launched"
    assert stats["expired"] == 1
    row = db.load_applied()[0]
    assert row["job_id"] == "stale" and row["status"] == "expired"
    assert row["error"] == worker.expired_reason(72)
    assert row["absolute_url"] == "https://example.com/jobs/stale"
    # Out of both halves for good: the record is what retires it.
    assert db.load_pending_applications(_window_start()) == []
    assert db.load_expired_applications(_window_start()) == []


def test_a_job_given_up_on_is_never_queued_or_recorded_twice(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _stale(db)

    _run(tmp_path, [], db=db, backlog=True)
    stats, _ = _run(tmp_path, [], db=db, backlog=True)

    assert calls == []
    assert stats["expired"] == 0
    assert len(db.load_applied()) == 1


def test_a_job_still_inside_the_window_is_retried_not_given_up_on(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")

    stats, _ = _run(tmp_path, [], db=db, backlog=True)

    assert len(calls) == 1 and stats["expired"] == 0
    assert _statuses(db) == {"j1": "submitted"}


def test_a_tripped_breaker_gives_up_on_nothing(tmp_path, launcher):
    """Known issue S2: a host where every `claude` launch failed at once. A machine
    that could not start a session has learnt nothing about the jobs it never
    reached, and retiring them would be the deferral-as-verdict bug in a new place."""
    _, script, _ = launcher
    db = Database(tmp_path / "test.db")
    _stale(db)
    script += [_Proc(rc=1), _Proc(rc=1), _Proc(rc=1)]

    stats, _ = _run(tmp_path, [_job("j1"), _job("j2"), _job("j3")], db=db, backlog=True)

    assert stats["deferred"] == 3 and stats["expired"] == 0
    assert _statuses(db) == {}


def test_a_blocked_applier_gives_up_on_nothing(tmp_path, launcher):
    """A missing resume is the install's problem, not the job's."""
    db = Database(tmp_path / "test.db")
    _stale(db)

    stats, _ = _run(tmp_path, [], settings=_settings(tmp_path, resume_path=""),
                    db=db, backlog=True)

    assert stats["expired"] == 0 and _statuses(db) == {}


def test_the_expiry_pass_opts_out_with_the_backlog(tmp_path, launcher):
    """It is that flag's other half: a caller that does not want earlier sweeps'
    jobs retried does not want them retired either."""
    db = Database(tmp_path / "test.db")
    _stale(db)

    stats, _ = _run(tmp_path, [], db=db, backlog=False)

    assert stats["expired"] == 0 and _statuses(db) == {}


def test_giving_up_on_a_job_does_not_move_this_sweeps_applier_bar(tmp_path, launcher):
    """The bar's total is `apply_queued`, this sweep's own stream. An expired job
    belongs to an earlier sweep — the same reason the backlog is left out of it."""
    db = Database(tmp_path / "test.db")
    db.start_progress("now", apply_enabled=True)
    _stale(db)

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        await q.put(_job("j1"))
        await q.put(None)
        return await worker.run_apply_worker(
            q, _settings(tmp_path), "RESUME TEXT", run_dir=_run_dir(tmp_path),
            db=db, include_backlog=True, run_id="now",
        )

    stats = asyncio.run(go())
    assert stats["expired"] == 1
    assert db.run_progress("now")["apply_handled"] == 1      # j1 only


# --- the queue it is fed from -----------------------------------------------

class _RecordingDB:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.queued = 0

    def record_pipeline_result(self, run_id, record):
        if self.fail:
            raise RuntimeError("disk full")

    def bump_progress(self, run_id, **deltas):
        self.queued += deltas.get("apply_queued", 0)


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
    db = _RecordingDB()
    got = _track(tmp_path, monkeypatch, db, [_job("a"), _job("b")])
    assert [g and g["job_id"] for g in got] == ["a", "b", None]
    # The overview's applier bar counts what reached the worker.
    assert db.queued == 2


def test_the_applier_gets_its_sentinel_even_when_tracking_fails(tmp_path, monkeypatch):
    got = _track(tmp_path, monkeypatch, _RecordingDB(fail=True), [_job("a")])
    assert got == [None], "the apply worker would wait forever"


def test_a_bare_disability_no_written_by_0_15_0_still_loads(tmp_path, monkeypatch):
    """Installs that saved through 0.15.0 carry `disability: no` unquoted, which
    PyYAML reads as False. It must load as the word, not switch auto-apply off."""
    applier = _write_configs(tmp_path, "settings:\n  location_filter: []\n")
    applier.write_text(
        "settings:\n  enable_applier: true\n  disability: no\n", encoding="utf-8")
    monkeypatch.setattr(applier_config.paths, "config_file", lambda name: tmp_path / name)

    settings = applier_config.load_applier_config(applier).settings
    assert settings.disability == "no"
    assert settings.enable_applier is True
