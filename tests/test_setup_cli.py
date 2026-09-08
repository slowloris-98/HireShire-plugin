"""Tests for the setup CLI.

This file replaced the heredoc'd Python snippets `/hireshire:setup` used to write
to a temp file. The snippets were the reason a first-time install asked for
permission a dozen times: Claude Code matches permission rules against the exact
command string, and a heredoc body differs on every call, so nothing could ever be
approved once.

What matters here is therefore that the argv stays *fixed* and the error messages
stay *readable* — the skill relays them to the user verbatim.
"""
from __future__ import annotations

import json
import sys

import pytest

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import approve  # noqa: E402
import setup_cli  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the plugin data dir at tmp so nothing touches the real install.

    A subdirectory, not `tmp_path` itself: the workspace tests create folders
    beside it, and `init_workspace` rightly refuses anything inside DATA.
    """
    data = tmp_path / "plugin_data"
    monkeypatch.setattr(paths, "DATA", data)
    monkeypatch.setattr(paths, "USER_CONFIG", data / "config")
    monkeypatch.setattr(paths, "RESULTS_DIR", data / "results")
    monkeypatch.setattr(paths, "LOGS_DIR", data / "logs")
    return data


def test_install_config_then_set_then_get_round_trips(data_dir, capsys):
    assert setup_cli.main(["install-config"]) == 0
    assert setup_cli.main(["set", "matcher", "--json", '{"threshold": 81}']) == 0
    capsys.readouterr()

    assert setup_cli.main(["get", "matcher"]) == 0
    assert json.loads(capsys.readouterr().out)["threshold"] == 81


def test_one_set_carries_several_keys(data_dir, capsys):
    """The skill batches a group of answers into one call, so this is the shape it
    actually uses — not one command per question."""
    setup_cli.main(["install-config"])
    payload = '{"location_filter": ["remote", "berlin"], "max_age_hours": 48}'
    assert setup_cli.main(["set", "scraper", "--json", payload]) == 0
    capsys.readouterr()

    setup_cli.main(["get", "scraper"])
    got = json.loads(capsys.readouterr().out)
    assert got["location_filter"] == ["remote", "berlin"]
    assert got["max_age_hours"] == 48


def test_a_rejected_key_reports_what_is_allowed(data_dir, capsys):
    setup_cli.main(["install-config"])
    assert setup_cli.main(["set", "matcher", "--json", '{"db_path": "/etc/passwd"}']) == 1
    err = capsys.readouterr().err
    assert "Not editable" in err and "db_path" in err


def test_the_yaml_nesting_mistake_is_named_rather_than_just_refused(data_dir, capsys):
    """`{"title_filter": {"exclude_keywords": [...]}}` is the shape the config file
    suggests and the writer rejects, so the error has to name the right call."""
    setup_cli.main(["install-config"])
    nested = '{"title_filter": {"exclude_keywords": ["intern"]}}'
    assert setup_cli.main(["set", "matcher", "--json", nested]) == 1
    assert "exclude_keywords" in capsys.readouterr().err


def test_malformed_json_is_a_message_not_a_traceback(data_dir, capsys):
    setup_cli.main(["install-config"])
    assert setup_cli.main(["set", "matcher", "--json", '{"threshold": }']) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_a_json_scalar_is_refused_with_the_shape_it_wanted(data_dir, capsys):
    setup_cli.main(["install-config"])
    assert setup_cli.main(["set", "matcher", "--json", "75"]) == 1
    assert "flat keys" in capsys.readouterr().err


def test_an_unknown_phase_lists_the_real_ones(data_dir, capsys):
    assert setup_cli.main(["get", "applyer"]) == 1
    assert "Choose from" in capsys.readouterr().err


def test_write_profile_resolves_the_data_dir_itself(data_dir, capsys):
    """The skill must never name this directory. A setup run in the desktop app
    once wrote the profile somewhere the engine never reads, which silently
    disabled the reranker for every later sweep."""
    assert setup_cli.main(["write-profile", "--text", "Component-based UI development."]) == 0
    printed = capsys.readouterr().out.splitlines()[0]

    dest = data_dir / setup_cli.PROFILE_FILENAME
    assert dest.exists()
    assert printed == dest.as_posix()
    assert "Component-based UI development." in dest.read_text(encoding="utf-8")


def test_an_empty_profile_is_refused(data_dir, capsys):
    assert setup_cli.main(["write-profile", "--text", "   "]) == 1
    assert "reranker" in capsys.readouterr().err


def test_init_workspace_refuses_the_install_dir(data_dir, capsys):
    """ROOT is replaced wholesale on update, so a workspace inside it would take the
    user's whole search history with it."""
    assert setup_cli.main(["init-workspace", str(paths.ROOT / "job-search")]) == 1
    assert "update" in capsys.readouterr().err


def test_init_workspace_creates_the_documented_skeleton(data_dir, tmp_path, capsys):
    ws = tmp_path / "search"
    assert setup_cli.main(["init-workspace", str(ws)]) == 0
    assert capsys.readouterr().out.strip() == ws.resolve().as_posix()
    assert (ws / paths.RESUME_SUBDIR).is_dir()
    assert (ws / paths.RUN_RESULTS_DIRNAME).is_dir()


def test_printed_paths_survive_being_pasted_back_into_a_json_payload(data_dir, tmp_path, capsys):
    """The round trip that broke a real first run on Windows.

    Every path this CLI prints exists to be fed into the next `set --json` call. A
    native Windows path is full of backslashes, which are JSON escapes: they lose a
    level in the shell and the next command dies on `Invalid \\escape`. The caller
    then retries the same way, because the payload looks correctly escaped.

    Printing POSIX separators makes the failure impossible rather than documented.
    On macOS and Linux this assertion is trivially true and the output is unchanged;
    on Windows it is the whole point.
    """
    ws = tmp_path / "search"
    setup_cli.main(["init-workspace", str(ws)])
    printed = capsys.readouterr().out.strip()

    assert "\\" not in printed
    # The real test: it round-trips through JSON without escaping, and still names
    # the same directory.
    from pathlib import Path
    payload = json.dumps({"workspace_dir": printed})
    assert Path(json.loads(payload)["workspace_dir"]) == ws.resolve()


def test_a_windows_path_in_json_is_refused_with_the_fix_named(data_dir, capsys):
    """The backstop for a caller who builds the payload by hand anyway. `Invalid
    \\escape` on its own does not tell anyone what to do differently."""
    assert setup_cli.main(["set", "scraper", "--json", r'{"workspace_dir": "C:\Users\me"}']) == 1
    err = capsys.readouterr().err
    assert "forward slashes" in err


def test_a_missing_resume_fails_before_anything_is_copied(data_dir, tmp_path, capsys):
    ws = tmp_path / "search"
    setup_cli.main(["init-workspace", str(ws)])
    capsys.readouterr()

    assert setup_cli.main(["install-resume", str(tmp_path / "nope.pdf"), str(ws)]) == 1
    assert list((ws / paths.RESUME_SUBDIR).iterdir()) == []


def test_every_subcommand_is_one_the_guard_will_approve():
    """The two lists are maintained by hand in different files. A subcommand missing
    from the guard still works — it just prompts, which is the failure this whole
    change exists to remove, and nothing else would report it."""
    guarded = approve._SUBCOMMANDS["scripts/setup_cli.py"]
    assert set(setup_cli.SUBCOMMANDS) == set(guarded)


def test_the_declared_subcommands_are_the_ones_that_are_implemented():
    assert set(setup_cli.SUBCOMMANDS) == set(setup_cli.HANDLERS)
