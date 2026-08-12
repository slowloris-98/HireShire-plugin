"""Filesystem anchors for the plugin.

Claude Code runs a plugin with an arbitrary working directory, so nothing in the
engine may resolve a path relative to cwd. Two roots matter, and they have
opposite lifetimes:

* ``ROOT`` — the install directory. **Replaced wholesale on every plugin update**,
  so it holds only shipped, read-only content: engine code, default YAMLs, the
  company slug lists, the curated bad-slug seed.
* ``DATA`` — ``~/.claude/plugins/data/<plugin>-<marketplace>/``. **Survives
  updates.** The venv, SQLite DB, the user's live config, the generated search
  profile and the results CSVs live here.

Writing mutable state into ROOT loses it on the next release, which is the single
easiest way to break this plugin.

Outside a plugin install — a plain checkout, or pytest — both env vars are absent
and everything falls back to the repo, so `pytest` and `python scraper.py` keep
working from a clone.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or _REPO)
DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or (_REPO / "data"))

SHIPPED_CONFIG = ROOT / "config"   # read-only: default YAMLs, company lists, slug seed
USER_CONFIG = DATA / "config"      # the user's live YAMLs, written by /hireshire:setup
RESULTS_DIR = DATA / "results"
LOGS_DIR = DATA / "logs"
DB_PATH = DATA / "hireshire.db"


def resolve_data(value: str | Path) -> Path:
    """Anchor a config path value.

    Absolute paths pass through untouched — that is how a user's resume, which
    lives outside the plugin entirely, is addressed. Relative paths are taken as
    DATA-relative, never cwd-relative. A leading ``data/`` is tolerated so the
    upstream YAML defaults (``data/hireshire.db``) keep working.
    """
    p = Path(value)
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0] == "data":
        p = Path(*parts[1:]) if len(parts) > 1 else Path()
    return DATA / p


def config_file(name: str) -> Path:
    """Locate a phase YAML: the user's copy in DATA if setup has written one,
    otherwise the shipped default. Setup copies ROOT → DATA on first run, so this
    fallback only covers the pre-setup window (and a bare checkout)."""
    user = USER_CONFIG / name
    return user if user.exists() else SHIPPED_CONFIG / name


def ensure_data_dirs() -> None:
    for d in (DATA, USER_CONFIG, RESULTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
