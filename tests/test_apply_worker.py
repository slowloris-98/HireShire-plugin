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
from hireshire.applier import limits
from hireshire.applier import reasons
from hireshire.applier import worker
from hireshire.applier.config import ApplierSettings
from hireshire.models.job import Job, Location
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


def _posting(job_id: str, company: str = "acme") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=company,
        job_id=job_id,
        title="Account Manager",
        location=Location(name="Remote"),
        absolute_url=f"https://example.com/jobs/{job_id}",  # type: ignore[arg-type]
        updated_at=now,
        scraped_at=now,
        content_text="We need an account manager.",
    )


def _seed(db: Database, jobs, run_id: str = "2026-09-19T12-00-00Z") -> None:
    """Scrape the postings these queue items name, so the applier has rows to write.

    `record_applied` and `mark_not_shortlisted` are UPDATEs against the posting the
    scraper made — the applier never creates one — so a fixture that queues a job
    without scraping it first would record nothing at all. This is what the real
    pipeline does before the worker ever sees the job.
    """
    db.insert_jobs(run_id, [
        _posting(j["job_id"], j.get("company") or "acme")
        for j in jobs if j.get("job_id")
    ])


def _run_dir(tmp_path, stamp: str = "2026-09-19_120000") -> Path:
    """This sweep's results folder — what `paths.make_run_dir` hands the worker.

    Built at the real depth, one folder per day above the run folder, so the cwd rule
    below (`out_dir` has to be inside the workspace) is exercised where a real sweep
    exercises it rather than one level shallower.
    """
    d = tmp_path / "hireshire_run_results" / stamp[:10] / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _applied(tmp_path, stamp: str = "2026-09-19_120000") -> Path:
    return _run_dir(tmp_path, stamp) / "applied"


def _record(db: Database, job_id: str, company: str, *args, **kw) -> None:
    """`record_applied`, having scraped the posting it writes onto.

    The writer is an UPDATE — the applier never creates a posting — so a fixture that
    records an application for a job id nothing ever scraped would write nothing at
    all. Seeded only when the posting is missing, so a test that scrapes under a
    particular run id keeps control of `first_run_id`.
    """
    if not db.get_jobs([job_id]):
        db.insert_jobs("2026-09-19T12-00-00Z", [_posting(job_id, company)])
    db.record_applied(job_id, company, *args, **kw)


def _run(tmp_path, jobs, settings=None, db=None, backlog=False, run_dir=None):
    db = db or Database(tmp_path / "test.db")
    settings = settings or _settings(tmp_path)
    run_dir = run_dir if run_dir is not None else _run_dir(tmp_path)
    _seed(db, jobs)

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


def _applied_rows(db: Database) -> list[dict]:
    """Raw rows for the postings with an application. `load_applied` does not select
    every column, and these tests are about one it leaves out."""
    with db._lock:
        return [dict(r) for r in db._conn.execute(
            "SELECT * FROM postings WHERE apply_status IS NOT NULL")]


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
    # The per-job rules must name the tools as that server exposes them — checked on
    # the prompt the session was actually sent, not on the file, so this also proves
    # `TOOL_PREFIX_TOKEN` was substituted for the provider that ran.
    sent = calls[0]["proc"].sent.decode("utf-8")
    assert "mcp__playwright__browser_navigate" in sent
    assert worker.TOOL_PREFIX_TOKEN not in sent


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


_PORTALS = ["amazon", "apple", "google", "intuit", "meta", "microsoft"]


def _exclusions(tmp_path, monkeypatch, user_list: str, portals: str | None) -> list[str]:
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    if portals is not None:
        (shipped / "direct_companies.json").write_text(portals, encoding="utf-8")
    applier = tmp_path / "applier.yaml"
    applier.write_text(f"settings:\n  exclude_companies: {user_list}\n", encoding="utf-8")
    monkeypatch.setattr(applier_config.paths, "config_file", lambda name: tmp_path / name)
    monkeypatch.setattr(applier_config.paths, "SHIPPED_CONFIG", shipped)
    return applier_config.load_applier_config(applier).settings.exclude_companies


def test_every_direct_portal_is_excluded_whatever_the_users_copy_says(tmp_path, monkeypatch):
    """The user's `applier.yaml` survives updates and never receives a shipped default,
    so an install written before `amazon` was listed kept driving Amazon's login wall."""
    got = _exclusions(tmp_path, monkeypatch, "[google, apple]", json.dumps(_PORTALS))
    assert {c.lower() for c in got} == set(_PORTALS)


def test_the_users_own_exclusions_survive_and_are_not_repeated(tmp_path, monkeypatch):
    got = _exclusions(tmp_path, monkeypatch, "[workday-co, Google]", json.dumps(_PORTALS))
    assert got[:2] == ["workday-co", "Google"]
    assert [c.lower() for c in got].count("google") == 1
    assert {c.lower() for c in got} == set(_PORTALS) | {"workday-co"}


@pytest.mark.parametrize("portals", [None, "not json", '{"a": 1}'])
def test_an_unreadable_portal_list_leaves_the_users_exclusions_alone(tmp_path, monkeypatch,
                                                                     portals):
    assert _exclusions(tmp_path, monkeypatch, "[acme]", portals) == ["acme"]


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
    """`mark_not_shortlisted` takes no `run_id`, and a backlog job is why.

    A job reached from the backlog was judged by an *earlier* sweep, so a writer
    scoped to the current one would retire nothing and the job would go straight back
    into the backlog — one browser session per sweep, to re-read a location that
    cannot change. It used to have to update a row per sweep for the same reason, or
    the lifetime page rendered the job twice under contradicting labels.
    """
    _, script, _ = launcher
    script.append(_outcome(status="skipped_location", location="Paris"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")                      # judged by an earlier sweep
    _run(tmp_path, [_job("j1")], db=db)

    (row,) = db.load_all_matches("r0")
    assert row["shortlisted"] is False
    assert row["skip_reason"] == worker.LOCATION_SKIP_REASON
    # No application record: a job in the wrong country is not something the user can
    # go and do by hand, so it is un-shortlisted into Jobs Filtered instead.
    assert _statuses(db) == {}


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
    _record(db, "j2", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
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
    _match(db, "r0", "j1", company="Google")
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


def test_a_session_with_no_browser_tools_is_deferred_not_recorded(
        tmp_path, launcher, caplog):
    """A session that never got its browser tools reached no verdict about the job.

    It exits cleanly, which every other clean ending treats as a verdict — but with no
    browser it cannot have opened the posting, let alone clicked submit, so there is no
    second application to protect against. Recording it retired the job permanently on
    a fact about the *session*: on one install 11 jobs were discarded this way while
    the cause was still intermittent.

    So: no `applied` row, the job stays shortlisted for the backlog, and it counts as a
    deferral.
    """
    _, script, _ = launcher
    script.append(_outcome(
        status="error", error="Browser automation tools are unavailable in this session."))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    with caplog.at_level("WARNING", logger=worker.logger.name):
        stats, _ = _run(tmp_path, [_job("j1")], db=db)

    assert _statuses(db) == {}, "a job with no browser session was recorded as applied"
    assert stats["deferred"] == 1 and stats["error"] == 0
    (row,) = db.load_all_matches("r0")
    assert row["shortlisted"] is True, "the backlog can no longer retry this job"
    assert "no browser tools" in caplog.text


def test_a_browser_deferral_is_noted_but_still_retried(tmp_path, launcher):
    """The note records WHEN, and changes nothing about the retrying.

    Both halves matter. The timestamp is what lets the overview say a shortlisted job
    is being retried rather than merely queued — a deferral writes no status, so there
    is nothing else to tell them apart. And the job must still be pending afterwards:
    if noting it ever set a status, the backlog would stop seeing it and the deferral
    would become the verdict this whole path exists to avoid.
    """
    _, script, _ = launcher
    script.append(_outcome(status="error", error="Browser tools unavailable"))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    stats, _ = _run(tmp_path, [_job("j1")], db=db)

    assert stats["deferred"] == 1 and _statuses(db) == {}
    (row,) = db.load_all_matches("r0")
    assert row["apply_deferred_at"], "nothing noted, so the page cannot say why"
    assert row["shortlisted"] is True
    # The backlog still has it, which is what "still retried" means.
    assert [r["job_id"] for r in
            db.load_pending_applications("2026-01-01T00:00:00+00:00")] == ["j1"]


def test_an_ordinary_launch_failure_notes_nothing(tmp_path, launcher):
    """Only the browser case is noted. A host that could not start the CLI at all says
    nothing about the browser, and a line claiming otherwise would send the user
    looking in the wrong place."""
    _, script, _ = launcher
    script.append(_Proc(rc=1))
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "j1")
    stats, _ = _run(tmp_path, [_job("j1")], db=db)

    assert stats["deferred"] == 1
    (row,) = db.load_all_matches("r0")
    assert not row["apply_deferred_at"]


@pytest.mark.parametrize("stored", [
    # Every wording one real install stored for this one cause. Free text is why they
    # did not group on the page, which is what hid how often it was happening.
    "Browser automation tools are unavailable",
    "Browser automation tools are unavailable in this session.",
    "Browser automation tools unavailable",
    "Browser tools unavailable",
    "Retry with Playwright browser tools enabled",
    reasons.BROWSER_UNAVAILABLE,
])
def test_every_wording_of_a_missing_browser_reads_as_one_label(stored):
    """`short_label` collapses the free text already in the database onto one label, so
    rows written before this existed read as one cause rather than five."""
    assert reasons.short_label("error", stored) == reasons.BROWSER_UNAVAILABLE


@pytest.mark.parametrize("stored, label", [
    ("This job is no longer available", reasons.POSTING_CLOSED),
    ("404 not found", reasons.POSTING_CLOSED),
    ("Manual application required.", reasons.MANUAL_REQUIRED),
    ("Submit not confirmed — check before reapplying", reasons.SUBMIT_UNCONFIRMED),
    ("Not a job posting", reasons.NOT_A_JOB),
])
def test_the_browser_rule_does_not_swallow_the_causes_around_it(stored, label):
    """The rule matches on "browser"/"automation", and sits ahead of `POSTING_CLOSED`
    because "no longer available" would otherwise claim a browser message. Neither may
    take the other's rows: one is a deferral and the rest are verdicts."""
    assert reasons.short_label("error", stored) == label


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


def test_the_prompt_carries_postal_code_and_education(tmp_path):
    """Both were `Required question` endings before setup asked for them. Unset they
    go through empty, which `apply_one.md` reads as never asked."""
    dirs = worker.SessionDirs(cwd=tmp_path, out_dir=tmp_path,
                              resume_path=tmp_path / "resume.pdf")
    degree = {"school": "Georgia Tech", "degree": "M.S.", "field": "CS",
              "graduation": "2026-12"}
    settings = _settings(tmp_path, postal_code="02139", education=[degree])
    prompt = worker.build_prompt(_job("j1"), settings, dirs, "RESUME")
    applicant = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])["applicant"]
    assert applicant["postal_code"] == "02139"
    assert applicant["education"] == [degree]

    prompt = worker.build_prompt(_job("j1"), _settings(tmp_path), dirs, "RESUME")
    applicant = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])["applicant"]
    assert applicant["postal_code"] == "" and applicant["education"] == []

    rules = worker.PROMPT_PATH.read_text(encoding="utf-8")
    assert "`postal_code`" in rules and "`education`" in rules


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
           scored_at: datetime | None = None, score=80, company="acme") -> None:
    """One shortlisted posting, scraped and judged by `run_id`.

    `company` has to be settable because it is half the posting's key: a fixture that
    scrapes under `acme` and then queues the job as `google` names two different
    postings, and the worker would write its verdict onto the one the test is not
    looking at.
    """
    scored_at = scored_at or datetime.now(timezone.utc)
    raw = {"job_id": job_id, "board_token": company, "title": "Account Manager",
           "absolute_url": f"https://example.com/jobs/{job_id}",
           "relevance_score": score, "cluster_representative": rep}
    # The posting first: `upsert_match` is an UPDATE onto the row the scraper made.
    db.insert_jobs(run_id, [_posting(job_id, company)])
    # Compact separators, as `model_dump_json` writes every real row — the no-JSON1
    # fallback in `Database._sibling_sql` depends on it.
    db.upsert_match(run_id, job_id, company, "Account Manager", score, shortlisted,
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
    _record(db, "applied", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
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


def test_an_application_off_the_backlog_is_recorded_as_one(tmp_path, launcher):
    """The fact has to be written when it is known. `applied` has no `run_id`, so
    nothing downstream can tell a job the backlog handed over from one this sweep
    found, and the Jobs Applied section would render the two identically."""
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "old")                       # shortlisted by an earlier sweep
    _run(tmp_path, [_job("j1")], db=db, backlog=True)

    flags = {r["job_id"]: r["applied_from_backlog"]
             for r in db.load_applied_matches()}
    assert flags == {"old": True, "j1": False}


def test_the_verdicts_no_session_produces_do_not_claim_a_backlog_origin(
        tmp_path, launcher):
    """`excluded` never reaches Jobs Applied, and the expiry pass is backlog-only by
    definition, so the flag would say nothing on either. Both keep the default."""
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "excl", company="Google")
    _stale(db, "gone")

    _run(tmp_path, [], db=db, backlog=True)

    rows = {r["job_id"]: (r["apply_status"], r["from_backlog"])
            for r in _applied_rows(db)}
    assert rows["excl"] == ("excluded", 0)
    assert rows["gone"] == (worker.EXPIRED_STATUS, 0)


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
    _record(db, "done", "acme", "t", "u", "2026-09-01T00:00:00+00:00",
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
    # 96, not the configured 72: the per-company cap is on by default and widens the
    # window so a held job is still in the backlog when its slot frees.
    assert row["error"] == worker.expired_reason(96)
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


# --- the per-company cap ----------------------------------------------------

def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_a_third_job_at_one_company_is_held_and_writes_nothing(tmp_path, launcher):
    """Two submissions, then a hold. A hold is a deferral — the company is only busy
    this week — so no `applied` row is written and the job stays in the backlog."""
    calls, _, _ = launcher
    stats, db = _run(tmp_path, [_job("a1"), _job("a2", company=" ACME "), _job("a3"),
                                _job("b1", company="beta")])

    assert len(calls) == 3                        # a1, a2, b1
    assert stats["submitted"] == 3 and stats["held"] == 1
    assert "a3" not in _statuses(db)


def test_the_hold_counts_only_recent_submissions(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _record(db, "old1", "acme", "t", "u", _ago(80), "submitted", None, None)
    _record(db, "old2", "acme", "t", "u", _ago(75), "submitted", None, None)
    _record(db, "err", "acme", "t", "u", _ago(1), "error", None, "x")
    _record(db, "exc", "acme", "t", "u", _ago(1), "excluded", None, "x")
    _record(db, "new", "acme", "t", "u", _ago(1), "submitted", None, None)

    stats, _ = _run(tmp_path, [_job("j1"), _job("j2")], db=db)

    # One recent submission counts, so one slot is left: j1 goes, j2 waits.
    assert len(calls) == 1 and stats["held"] == 1


def test_a_hand_marked_application_counts(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    for j in ("h1", "h2"):
        _record(db, j, "acme", "t", "u", _ago(2), "error", None, "x")
        db.mark_applied_by_hand(j, _ago(1))

    stats, _ = _run(tmp_path, [_job("j1")], db=db)
    assert calls == [] and stats["held"] == 1


def test_a_cap_of_zero_turns_it_off(tmp_path, launcher):
    calls, _, _ = launcher
    stats, _ = _run(tmp_path, [_job(f"j{n}") for n in range(4)],
                    settings=_settings(tmp_path, max_per_company=0))
    assert len(calls) == 4 and stats["held"] == 0


def test_a_held_job_is_applied_to_from_the_backlog_once_a_slot_frees(tmp_path, launcher):
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "held")
    _record(db, "x1", "acme", "t", "u", _ago(10), "submitted", None, None)
    _record(db, "x2", "acme", "t", "u", _ago(5), "submitted", None, None)

    stats, _ = _run(tmp_path, [], db=db, backlog=True)
    assert calls == [] and stats["held"] == 1
    assert [r["job_id"] for r in db.load_pending_applications(_window_start())] == ["held"]

    # A later sweep, after the older submission has aged out of the window.
    _record(db, "x1", "acme", "t", "u", _ago(73), "submitted", None, None)
    stats, _ = _run(tmp_path, [], db=db, backlog=True)
    assert len(calls) == 1 and stats["submitted"] == 1


def test_a_job_held_past_backlog_hours_is_not_expired_before_its_slot_frees(
        tmp_path, launcher):
    """Scored 80h ago: past the configured 72h backlog, inside the widened window. The
    slot a hold waits for frees up to `company_window_hours` after a submission that
    may have come minutes after the job was scored, so a 72h backlog would expire it
    a moment before it could have been applied to."""
    calls, _, _ = launcher
    db = Database(tmp_path / "test.db")
    _match(db, "r0", "held", scored_at=datetime.now(timezone.utc) - timedelta(hours=80))

    stats, _ = _run(tmp_path, [], db=db, backlog=True)

    assert stats["expired"] == 0
    assert len(calls) == 1                        # retried, and the slot is free


def test_the_backlog_window_widens_only_while_the_cap_is_on(tmp_path):
    assert limits.backlog_window_hours(_settings(tmp_path)) == 96
    assert limits.backlog_window_hours(_settings(tmp_path, backlog_hours=200)) == 200
    assert limits.backlog_window_hours(_settings(tmp_path, max_per_company=0)) == 72


def test_hold_until_is_when_the_count_drops_below_the_cap(tmp_path):
    s = _settings(tmp_path)
    now = datetime.now(timezone.utc)
    stamps = [(now - timedelta(hours=h)).isoformat() for h in (50, 10, 70)]
    until = limits.hold_until(stamps + ["garbage", _ago(100)], s, now)
    # Three inside a cap of 2: the second-oldest (50h ago) has to age out.
    assert abs((until - (now + timedelta(hours=22))).total_seconds()) < 1
    assert limits.hold_until(stamps[:1], s, now) is None



# --- which CLI applies, and what happens when none can ------------------------------

def test_an_unset_provider_still_runs_the_claude_session(tmp_path, launcher):
    """Empty `applier.provider` means claude_code, so an install predating the setting
    keeps the session it already had."""
    calls, _, _ = launcher

    stats, db = _run(tmp_path, [_job("j1")])

    assert stats["submitted"] == 1
    assert calls[0]["argv"][0] == "claude"


def test_an_unbuildable_provider_records_nothing_and_leaves_every_job_pending(
        tmp_path, launcher, monkeypatch, caplog):
    """The whole contract of having a provider choice at all.

    There is no failover to the other CLI, mirroring `matcher.make_backend` — so a
    missing `codex`, an empty `applier.model` or a typo has to be a DEFERRAL: nothing
    launched, no `applied` rows, no expiry rows, and every job still shortlisted for the
    next sweep's backlog. Retrying the job on Claude instead would be a second browser
    session against a form the first may already have submitted; recording anything
    would retire a job over an install's problem.
    """
    calls, _, _ = launcher
    monkeypatch.setattr("shutil.which", lambda name: None)      # no codex on PATH
    db = Database(tmp_path / "test.db")
    _stale(db)

    stats, _ = _run(tmp_path, [_job("j1")],
                    settings=_settings(tmp_path, provider="codex", model="gpt-5.6-terra"),
                    db=db, backlog=True)

    assert calls == [], "a session was launched with no usable provider"
    assert _statuses(db) == {}, "a job was retired over an unavailable CLI"
    assert stats["expired"] == 0 and stats["submitted"] == 0
    for row in db._conn.execute(
            "SELECT shortlisted FROM postings WHERE scored_at IS NOT NULL"):
        assert row["shortlisted"] == 1, "the backlog can no longer retry this job"
    assert "apply provider unavailable" in caplog.text, \
        "the user was not told why nothing applied"


def test_the_apply_prompt_names_the_tools_the_session_actually_has(tmp_path, launcher):
    """The prompt's tool naming follows the provider, and nothing reaches a model with
    the placeholder still in it — a session told to call tools it does not have cannot
    apply to anything."""
    calls, _, _ = launcher

    _run(tmp_path, [_job("j1")])
    sent = calls[0]["proc"].sent.decode("utf-8")

    assert worker.TOOL_PREFIX_TOKEN not in sent
    assert "mcp__playwright__browser_navigate" in sent
    # And the file itself still carries the placeholder, so neither name is hardcoded.
    assert worker.TOOL_PREFIX_TOKEN in worker.PROMPT_PATH.read_text(encoding="utf-8")


def test_the_sweep_says_which_cli_applied(tmp_path, launcher, caplog):
    """The matcher has always printed its provider and model; the applier printed
    nothing, which is what let a stale `applier.provider` run a whole sweep unnoticed —
    a user who switched to Codex and believed they had switched back.

    Once per sweep, and never on the blocked path, where the error line has already said
    why nothing will run.
    """
    import logging
    caplog.set_level(logging.INFO)

    _run(tmp_path, [_job("j1")])

    assert "driving the browser with claude_code" in caplog.text


def test_a_blocked_sweep_does_not_announce_a_session_it_never_built(
        tmp_path, launcher, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setattr("shutil.which", lambda name: None)

    _run(tmp_path, [_job("j1")],
         settings=_settings(tmp_path, provider="codex", model="gpt-5.6-terra"))

    assert "driving the browser" not in caplog.text
    assert "apply provider unavailable" in caplog.text


def test_a_codex_model_left_behind_does_not_reach_the_claude_session(tmp_path, launcher):
    """Switching back: setup rewrites `provider` but may leave `model` and `effort` as
    the Codex ones. They are inert here — this session passes neither flag — and this
    test exists so a future "pass the model through for symmetry" cannot silently send
    `claude -p` a model it will reject.
    """
    calls, _, _ = launcher

    stats, _ = _run(tmp_path, [_job("j1")], settings=_settings(
        tmp_path, provider="claude_code", model="gpt-5.6-terra", effort="xhigh"))

    argv = calls[0]["argv"]
    assert stats["submitted"] == 1
    assert "--model" not in argv and "--effort" not in argv
    assert not any("gpt-5.6" in a for a in argv)


# --- the console the apply session runs in ----------------------------------


def test_the_session_shares_the_judges_spawn_flags_but_not_its_cli_flags(
        tmp_path, launcher, monkeypatch):
    """Two rules that look like one, and only one of them is about sharing.

    `--safe-mode` and `--tools ""` must NOT be shared through `claude_cli`: they are
    argv, which decides what the model can do, and this session needs a browser (see
    `test_the_session_keeps_the_tools_the_judge_strips`). `creationflags` MUST be
    shared: it is spawn kwargs, which decide whether Windows will start the process at
    all, and the answer is the same for every child the engine starts. So the judge and
    the applier want identical spawn kwargs and different argv, and a reader who finds
    only the argv rule will wrongly conclude the helper violates it.
    """
    monkeypatch.setattr(worker.claude_cli.sys, "platform", "win32")
    calls, _, _ = launcher
    _run(tmp_path, [_job("j1")])

    argv = list(calls[0]["argv"])
    assert "--safe-mode" not in argv and "--tools" not in argv
    assert calls[0]["kwargs"].get("creationflags") == 0x08000000


def test_the_taskkill_that_ends_a_session_runs_in_its_own_console(monkeypatch):
    """The second-order bug: the kill is itself a console child.

    On a stale console `taskkill` could not launch either, so a timed-out session's
    browser went on sitting over a half-filled form. This cannot use the `launcher`
    fixture, which replaces `terminate_apply_subprocess` outright.
    """
    monkeypatch.setattr(worker.claude_cli.sys, "platform", "win32")
    seen: dict = {}

    class _Live:
        pid = 4242
        returncode = None

        def kill(self):
            pass

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

        class _Done:
            returncode = 0
        return _Done()

    monkeypatch.setattr(worker, "_apply_proc", _Live())
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    worker.terminate_apply_subprocess()

    assert seen["argv"][0] == "taskkill" and "/T" in seen["argv"]
    assert seen["kwargs"].get("creationflags") == 0x08000000


def test_a_windows_launch_failure_names_the_console_and_the_cure(
        tmp_path, launcher, caplog):
    """When the breaker trips because the host would not start the CLI, the message has
    to name that and say restarting the sweep fixes it. The generic line sends the user
    to their login, which is the one thing that is not wrong."""
    _, script, _ = launcher
    script.extend(_Proc(rc=3221225794) for _ in range(3))
    with caplog.at_level("ERROR", logger=worker.logger.name):
        _run(tmp_path, [_job(f"j{i}") for i in range(3)])

    tripped = [r.getMessage() for r in caplog.records if "failed in a row" in r.getMessage()]
    assert len(tripped) == 1
    assert "console" in tripped[0] and "restart" in tripped[0]


def test_a_scratch_dir_that_cannot_be_deleted_is_reported(
        tmp_path, launcher, caplog, monkeypatch):
    """The measurement behind known issue A8.

    `shutil.rmtree(..., ignore_errors=True)` already swallows a live process holding a
    file in the session's scratch folder -- on Windows, almost always a browser the
    session did not close (playwright-mcp#1568: Chrome ignores SIGINT and SIGTERM and
    outlives the MCP server). We cannot reach it, because `apply_one` has already exited
    and its descendants are reparented. So the signal is recorded rather than acted on,
    and tidying up still never costs the job.
    """
    # The folder survives, which is what a live browser holding a file looks like.
    monkeypatch.setattr(worker.shutil, "rmtree", lambda *a, **k: None)
    with caplog.at_level("WARNING", logger=worker.logger.name):
        stats, db = _run(tmp_path, [_job("j1")])

    assert _statuses(db) == {"j1": "submitted"}, "the outcome is still recorded"
    assert stats["submitted"] == 1
    assert "scratch folder" in caplog.text and "browser" in caplog.text
