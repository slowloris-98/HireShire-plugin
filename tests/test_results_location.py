"""Where results are written.

The plugin's own state lives in the data directory and always will. Results are
the exception: they go to the folder the *user* made for their job search, whose
absolute path `/hireshire:setup` records in `scraper.workspace_dir`.

The property these tests exist to protect is that the working directory plays no
part in that decision. A plugin's cwd is whatever project the user happens to be
in, so a session launched from somewhere else must still write to the folder they
chose — see the module docstring in hireshire/paths.py.
"""
from __future__ import annotations

import re

import pytest

from hireshire import config_writer as cw
from hireshire import paths


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the plugin data dir at tmp so nothing touches the real install."""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(paths, "USER_CONFIG", tmp_path / "config")
    monkeypatch.setattr(paths, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(paths, "LOGS_DIR", tmp_path / "logs")
    cw.install_user_config()
    return tmp_path


def test_unset_workspace_falls_back_to_the_data_directory(data_dir):
    """Installs made before this setting existed keep working untouched — they
    only move once the user re-runs setup."""
    assert paths.workspace_dir() is None
    assert paths.results_root() == paths.RESULTS_DIR


def test_configured_workspace_becomes_the_results_root(data_dir, tmp_path):
    ws = tmp_path / "hireshire_job_search"
    cw.write_config("scraper", {"workspace_dir": str(ws)})

    assert paths.workspace_dir() == ws
    assert paths.results_root() == ws / "hireshire_run_results"


def test_the_working_directory_is_never_consulted(data_dir, tmp_path, monkeypatch):
    """The one property worth a test of its own.

    A relative value can only arrive via a hand-edited file (ScraperSettings
    rejects it at write time), and the tempting wrong thing to do with it is
    anchor it against cwd. Run from a directory that would make that visible.
    """
    cfg = data_dir / "config" / "scraper.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            'workspace_dir: ""', "workspace_dir: some_relative_folder"
        ),
        encoding="utf-8",
    )

    elsewhere = tmp_path / "some_other_project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert paths.workspace_dir() is None
    assert paths.results_root() == paths.RESULTS_DIR
    assert elsewhere not in paths.results_root().parents


def test_write_config_rejects_a_relative_workspace(data_dir):
    with pytest.raises(cw.ConfigError, match="absolute"):
        cw.write_config("scraper", {"workspace_dir": "hireshire_job_search"})


def test_quoted_windows_path_round_trips(data_dir, tmp_path):
    """Windows Explorer's "Copy as path" wraps the path in quotes and users paste
    it whole. Backslashes also have to survive ruamel's quoting."""
    ws = tmp_path / "My Job Search"
    cw.write_config("scraper", {"workspace_dir": f'"{ws}"'})

    assert cw.read_config("scraper")["workspace_dir"] == str(ws)
    assert paths.workspace_dir() == ws


def test_unreachable_workspace_degrades_instead_of_losing_the_run(data_dir):
    """A 20-minute sweep must not be thrown away because an external drive is
    unplugged. Results land in the data directory instead, and nothing raises."""
    cw.write_config("scraper", {"workspace_dir": "Q:\\gone" if _on_windows() else "/proc/x/gone"})

    run_dir = paths.make_run_dir("2026-08-12_143005")

    assert paths.RESULTS_DIR in run_dir.parents
    assert run_dir.is_dir()


def test_a_deleted_workspace_is_recreated(data_dir, tmp_path):
    """Setup creates this structure, so recreating a folder the user deleted is
    the same policy — not a reason to fall back."""
    ws = tmp_path / "hireshire_job_search"
    cw.write_config("scraper", {"workspace_dir": str(ws)})
    assert not ws.exists()

    run_dir = paths.make_run_dir("2026-08-12_143005")

    assert run_dir == ws / "hireshire_run_results" / "2026-08-12_143005"
    assert run_dir.is_dir()


def test_run_dir_and_csv_share_one_stamp():
    """The folder name and the file name inside it are the same string — that is
    what makes a directory listing readable without opening anything."""
    import orchestrate

    stamp = orchestrate._run_stamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", stamp), stamp
    assert orchestrate._csv_name(stamp) == f"{stamp}_results.csv"
    assert orchestrate._json_name(stamp) == f"{stamp}_results.json"


def _on_windows() -> bool:
    import sys

    return sys.platform.startswith("win")
