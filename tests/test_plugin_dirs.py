"""Where mutable state lives, and how it gets rescued when it lived in the wrong place.

`CLAUDE_PLUGIN_DATA` is set for hooks but not for the Bash calls a skill makes, so
the engine resolved DATA to `ROOT/data` for everything the skills ran: the user's
config, the SQLite DB, the generated profile and the logs all landed in the install
directory, which Claude Code replaces wholesale on the next update.
"""
from __future__ import annotations

import sys
from pathlib import Path

from hireshire.plugin_dirs import derive_data_dir, legacy_data_dirs, resolve_dirs


def _install(tmp_path: Path, version: str = "0.2.1") -> Path:
    """A ROOT laid out the way Claude Code lays out an installed plugin."""
    root = tmp_path / "plugins" / "cache" / "hireshire" / "hireshire" / version
    root.mkdir(parents=True)
    return root


def test_data_is_derived_from_an_install_path(tmp_path):
    root = _install(tmp_path)
    assert derive_data_dir(root) == tmp_path / "plugins" / "data" / "hireshire-hireshire"


def test_a_checkout_derives_nothing(tmp_path):
    """A bare clone or a --plugin-dir load must fall back to the repo, not guess."""
    plain = tmp_path / "some" / "checkout"
    plain.mkdir(parents=True)
    assert derive_data_dir(plain) is None


def test_the_environment_still_wins(tmp_path, monkeypatch):
    root = _install(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "explicit"))
    assert resolve_dirs() == (root, tmp_path / "explicit")


def test_data_is_derived_when_only_root_is_set(tmp_path, monkeypatch):
    """The case that caused the bug: a skill's Bash call gets neither var, and the
    engine must not fall back to writing inside the install directory."""
    root = _install(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    _, data = resolve_dirs()
    assert data == tmp_path / "plugins" / "data" / "hireshire-hireshire"
    assert root not in data.parents, "DATA must never sit inside the install dir"


def test_stranded_data_is_found_under_the_previous_version(tmp_path):
    """The update that ships the fix installs to a *new* version folder, so the
    files to rescue are next door, not underneath."""
    old = _install(tmp_path, "0.2.0")
    (old / "data").mkdir()
    (old / "data" / "hireshire.db").write_text("db")
    new = _install(tmp_path, "0.2.1")
    data = derive_data_dir(new)

    assert legacy_data_dirs(new, data) == [old / "data"]


def test_an_empty_install_dir_has_nothing_to_rescue(tmp_path):
    new = _install(tmp_path, "0.2.1")
    (new / "data").mkdir()
    (new / "data" / "venv").mkdir()  # not migratable — absolute paths inside

    assert legacy_data_dirs(new, derive_data_dir(new)) == []


def test_the_live_data_dir_is_never_reported_as_stranded(tmp_path):
    """A checkout writes to ROOT/data legitimately; migrating it onto itself would
    be a no-op at best and data loss at worst."""
    root = tmp_path / "checkout"
    (root / "data").mkdir(parents=True)
    (root / "data" / "hireshire.db").write_text("db")

    assert legacy_data_dirs(root, root / "data") == []


def test_bootstrap_moves_stranded_files_and_leaves_the_venv(tmp_path, monkeypatch):
    old = _install(tmp_path, "0.2.0")
    legacy = old / "data"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "matcher.yaml").write_text("settings: {threshold: 85}")
    (legacy / "hireshire.db").write_text("db")
    (legacy / "venv").mkdir()
    new = _install(tmp_path, "0.2.1")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(new))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import importlib

    import bootstrap
    importlib.reload(bootstrap)  # picks up the patched environment
    bootstrap.rescue_stranded_data()

    data = tmp_path / "plugins" / "data" / "hireshire-hireshire"
    assert (data / "hireshire.db").read_text() == "db"
    assert "threshold: 85" in (data / "config" / "matcher.yaml").read_text()
    assert (legacy / "venv").exists(), "the venv hard-codes paths; it must be rebuilt"
    assert not (legacy / "hireshire.db").exists(), "a move, not a copy"
