"""Tests for the hand-recorded outcomes CLI.

Two things matter here. The argv has to stay *fixed*, because `scripts/approve.py`
recognises these commands by shape and a drifting argv means a permission prompt
between the user's click and the thing it asks for. And the two outcomes have to stay
*asymmetric*: `applied` writes an application, `declined` writes none, and getting
that backwards either inflates the Jobs applied tile or leaves the job sitting in the
section it was meant to leave.
"""
from __future__ import annotations

import json
import sys

import pytest

from hireshire import paths
from hireshire.storage.db import DECLINED_BY_USER, Database

sys.path.insert(0, str(paths.ROOT / "scripts"))

import approve  # noqa: E402
import jobs_cli  # noqa: E402

RUN = "2026-09-09T06-51-12Z"


def _db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def _shortlisted(db: Database, job_id: str, *, score: int = 85) -> None:
    raw = json.dumps({
        "job_id": job_id, "board_token": "acme", "title": "Backend Engineer",
        "absolute_url": f"https://example.com/jobs/{job_id}",
        "relevance_score": score, "skipped": False, "skip_reason": None,
    })
    db.upsert_match(RUN, job_id, "acme", "Backend Engineer", score, True, False, None,
                    RUN, "2026-09-09T07:00:00+00:00", raw)


def _attempt(db: Database, job_id: str, status: str = "error",
             error: str = "Stuck on a required question — check whether it was sent.") -> None:
    db.record_applied(job_id, "acme", "Backend Engineer",
                      f"https://example.com/jobs/{job_id}",
                      "2026-09-09T08:00:00+00:00", status, None, error)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The CLI wired to a throwaway database, with the report rebuild stubbed out.

    The rebuild is covered by the reporting tests; here it would only need a results
    directory and a `last_run.json` to write into.
    """
    db = _db(tmp_path)
    monkeypatch.setattr(jobs_cli, "get_db", lambda: db)
    monkeypatch.setattr(jobs_cli, "_rebuild_reports", lambda: {"overview": "X.html"})
    return db


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_every_subcommand_is_one_the_guard_vouches_for():
    """The list in the CLI and the list in the guard have to stay in step."""
    assert set(jobs_cli.SUBCOMMANDS) == approve._SUBCOMMANDS["scripts/jobs_cli.py"]


def test_list_shows_both_kinds_of_stuck_job_at_lifetime_scope(cli, capsys):
    """Needs-attention rows and shortlisted-never-applied rows, in one payload.

    Neither half is scoped to a run: run scope needs a `matches` row in that sweep,
    and a job the backlog carried across sweeps has none — which is the job that gets
    stuck in the first place.
    """
    _shortlisted(cli, "j1")
    _shortlisted(cli, "j2")
    _attempt(cli, "j1", status="excluded", error="Portal needs an account login.")

    assert jobs_cli.main(["list"]) == 0
    payload = _out(capsys)

    assert [r["job_id"] for r in payload["attention"]] == ["j1"]
    assert payload["attention"][0]["status"] == "excluded"
    assert payload["attention"][0]["reason"] == "Portal needs an account login."
    # j1 has an application on record, so only j2 is still waiting on the applier.
    assert [r["job_id"] for r in payload["shortlisted"]] == ["j2"]
    assert payload["shortlisted"][0]["url"] == "https://example.com/jobs/j2"


def test_a_submitted_application_is_not_something_the_user_has_to_deal_with(cli, capsys):
    _shortlisted(cli, "j1")
    _attempt(cli, "j1", status="submitted", error=None)

    assert jobs_cli.main(["list"]) == 0
    payload = _out(capsys)
    assert payload == {"attention": [], "shortlisted": []}


def test_applied_promotes_the_row_and_reports_what_it_did(cli, capsys):
    _shortlisted(cli, "j1")
    _attempt(cli, "j1")

    assert jobs_cli.main(["applied", "--job-id", "j1"]) == 0
    payload = _out(capsys)

    assert payload["results"] == [
        {"job_id": "j1", "action": "applied", "result": "updated"}
    ]
    # The pages are rebuilt, because a static file nothing rewrites is a page the user
    # would watch not change.
    assert payload["pages"] == {"overview": "X.html"}
    assert [r["status"] for r in cli.load_applied()] == ["submitted"]


def test_declined_writes_no_application_and_retires_the_job(cli, capsys):
    _shortlisted(cli, "j1")
    _attempt(cli, "j1")

    assert jobs_cli.main(["declined", "--job-id", "j1"]) == 0
    payload = _out(capsys)

    assert payload["results"][0]["result"] == "declined"
    # Not an application: no row at all, because any status other than 'submitted'
    # renders under Needs Attention by design.
    assert cli.load_applied() == []
    row = next(r for r in cli.load_all_matches(RUN) if r["job_id"] == "j1")
    assert row["skip_reason"] == DECLINED_BY_USER
    assert row["shortlisted"] in (0, False)
    # It keeps the score it earned, so Jobs Filtered shows a number and not a dash.
    assert row["relevance_score"] == 85


def test_several_jobs_are_one_command_and_one_approval(cli, capsys):
    for job_id in ("j1", "j2", "j3"):
        _shortlisted(cli, job_id)

    assert jobs_cli.main(
        ["applied", "--job-id", "j1", "--job-id", "j2", "--job-id", "j3"]
    ) == 0
    payload = _out(capsys)
    assert [r["result"] for r in payload["results"]] == ["inserted"] * 3


def test_a_job_the_database_never_saw_changes_nothing_and_rebuilds_nothing(cli, capsys):
    """An unknown id is reported, not invented, and costs no page rebuild."""
    assert jobs_cli.main(["applied", "--job-id", "ghost"]) == 0
    payload = _out(capsys)

    assert payload["results"] == [
        {"job_id": "ghost", "action": "applied", "result": "unknown"}
    ]
    assert payload["pages"] == {}
    assert cli.load_applied() == []


def test_declining_a_job_twice_is_a_no_op_the_second_time(cli, capsys):
    _shortlisted(cli, "j1")
    assert jobs_cli.main(["declined", "--job-id", "j1"]) == 0
    capsys.readouterr()

    assert jobs_cli.main(["declined", "--job-id", "j1"]) == 0
    payload = _out(capsys)
    assert payload["results"][0]["result"] == "nothing_to_change"
    assert payload["pages"] == {}


def test_a_write_needs_a_job_id(capsys):
    """argparse exits 2 rather than writing to every job in the database."""
    with pytest.raises(SystemExit) as exc:
        jobs_cli.main(["applied"])
    assert exc.value.code == 2


def test_an_unreadable_database_is_a_message_not_a_traceback(tmp_path, monkeypatch,
                                                             capsys):
    """The skill relays stderr verbatim, so it has to arrive readable."""
    def boom():
        raise OSError("database is locked")

    monkeypatch.setattr(jobs_cli, "get_db", boom)
    assert jobs_cli.main(["list"]) == 1
    assert "OSError: database is locked" in capsys.readouterr().err
