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

There is a third root, and it belongs to the user rather than the plugin:

* ``WORKSPACE`` — the folder the user made for their job search, holding their
  resume and every run's results. Read from ``settings.workspace_dir`` in their
  scraper.yaml, where ``/hireshire:setup`` recorded it **once, as an absolute
  path**. This does not weaken the cwd rule above: the setup *skill* knows the
  working directory and captures it, the engine only ever reads config. A session
  launched from somewhere else still writes to the folder the user chose.

Neither env var can be relied on: Claude Code sets them for **hooks**, but not for
the Bash calls a skill makes, and the skills are what write every mutable file. So
DATA is *derived* from ROOT's install path when the environment is silent — see
`hireshire/plugin_dirs.py`, which owns that reasoning and is shared with
`scripts/bootstrap.py`.

Outside a plugin install — a plain checkout, or pytest — nothing resolves and
everything falls back to the repo, so `pytest` and `python scraper.py` keep
working from a clone.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from hireshire.plugin_dirs import resolve_dirs

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parent.parent

ROOT, DATA = resolve_dirs()

SHIPPED_CONFIG = ROOT / "config"   # read-only: default YAMLs, company lists, slug seed
USER_CONFIG = DATA / "config"      # the user's live YAMLs, written by /hireshire:setup
RESULTS_DIR = DATA / "results"     # fallback results root; see results_root()
LOGS_DIR = DATA / "logs"
DB_PATH = DATA / "hireshire.db"
LAST_RUN_PATH = DATA / "last_run.json"  # pointer to the newest run, read by /apply

# Layout inside the user's workspace. Named here because setup creates these and
# the engine writes into them, and they must not drift apart.
RUN_RESULTS_DIRNAME = "hireshire_run_results"
RESUME_SUBDIR = Path("resume") / "original"


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


def workspace_dir() -> Path | None:
    """The user's own job-search folder, or None if setup has not set one.

    Reads `settings.workspace_dir` straight out of the YAML rather than through
    `hireshire.config`, which imports this module — going the other way would be a
    cycle. The file is a couple of kilobytes and a run reads it once, so there is
    nothing to cache.

    cwd is deliberately not consulted, here or anywhere: this value is whatever
    setup recorded, so results land in the user's folder no matter where the
    session was launched.
    """
    try:
        raw = yaml.safe_load(config_file("scraper.yaml").read_text(encoding="utf-8")) or {}
        value = str((raw.get("settings") or {}).get("workspace_dir") or "").strip()
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Could not read workspace_dir from scraper.yaml (%s); results will go "
            "to the plugin data directory", exc,
        )
        return None
    if not value:
        return None

    p = Path(value).expanduser()
    if not p.is_absolute():
        # ScraperSettings rejects this at write time, so it can only arrive via a
        # hand-edited file. Anchoring it against cwd is the one thing we must not do.
        logger.warning("workspace_dir %r is not an absolute path; ignoring it", value)
        return None
    return p


def results_root() -> Path:
    """Where per-run result directories are created."""
    ws = workspace_dir()
    return (ws / RUN_RESULTS_DIRNAME) if ws else RESULTS_DIR


def make_run_dir(stamp: str) -> Path:
    """Create and return this run's results directory.

    Never raises when a workspace is configured. That workspace is on a filesystem
    the plugin does not control — the folder gets deleted, the external drive is
    unplugged, the network share is not mounted — and throwing away a 20-minute
    sweep because its output folder went missing is the wrong trade. A merely
    absent directory is recreated (setup creates the same structure); only an
    unusable one falls back to the data directory, loudly.
    """
    root = results_root()
    try:
        d = root / stamp
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as exc:
        if root == RESULTS_DIR:
            raise  # the fallback itself is broken; there is nowhere left to go
        logger.error(
            "Cannot write results to %s (%s). Is the drive connected? Falling back "
            "to %s for this run — re-run /hireshire:setup to choose a new folder.",
            root, exc, RESULTS_DIR,
        )
        d = RESULTS_DIR / stamp
        d.mkdir(parents=True, exist_ok=True)
        return d
