"""Where the plugin's two directories are, without trusting the environment.

`CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` are set for **hooks**, but not for
the Bash calls a skill makes. A first real install proved the difference the
expensive way: the SessionStart hook built the venv in the data directory at
16:56, and three minutes later a skill-driven run — same machine, same session,
no env vars — resolved DATA to `ROOT/data` and built a *second* venv there. Every
mutable file the skills then wrote (the user's config, the SQLite DB, the
generated profile, the logs) landed in the install directory, which is replaced
wholesale on the next plugin update.

So DATA is derived from ROOT's own location when the environment is silent. An
installed plugin lives at a known path:

    ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/   <- ROOT
    ~/.claude/plugins/data/<plugin>-<marketplace>/              <- DATA

That is a layout owned by Claude Code rather than by us, so every step is guarded
and the answer falls back to `<repo>/data` — correct for a bare checkout, and no
worse than the old behaviour if the layout ever changes.

Deliberately imports **stdlib only**: `scripts/bootstrap.py` runs on the system
interpreter before the venv exists and needs this same answer, and it cannot
import `hireshire.paths` (which needs PyYAML).
"""

from __future__ import annotations

import os
from pathlib import Path

# Files that belong to DATA and must survive a plugin update. `venv` is absent on
# purpose: it hard-codes absolute paths, so it is rebuilt rather than moved.
MIGRATABLE = (
    "config",
    "logs",
    "hireshire.db",
    "hireshire.db-wal",
    "hireshire.db-shm",
    "last_run.json",
    "profile.md",
    "user_bad_slugs.json",
    "user_recovered_slugs.json",
)


def derive_data_dir(root: Path) -> Path | None:
    """DATA for an installed plugin, inferred from ROOT. None if ROOT is not an
    install (a checkout, a `--plugin-dir` load, an unfamiliar layout)."""
    try:
        version_dir = root.resolve()
        plugin, marketplace = version_dir.parent, version_dir.parent.parent
        cache = marketplace.parent
    except (OSError, IndexError):
        return None

    if cache.name != "cache" or not plugin.name or not marketplace.name:
        return None
    return cache.parent / "data" / f"{plugin.name}-{marketplace.name}"


def resolve_dirs() -> tuple[Path, Path]:
    """(ROOT, DATA), preferring the environment and deriving what it omits."""
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env_data:
        return root, Path(env_data)
    return root, (derive_data_dir(root) or (root / "data"))


def legacy_data_dirs(root: Path, data: Path) -> list[Path]:
    """`data/` directories left inside an install dir by the bug described above.

    Checks this ROOT and its sibling version directories, because the update that
    delivers the fix installs to a *new* version folder — the stranded files are
    next door under the old version, not underneath us. Newest last, so a caller
    migrating in order ends on the most recent.
    """
    candidates = [root]
    parent = root.parent
    try:
        if parent.is_dir():
            candidates += [d for d in parent.iterdir() if d.is_dir() and d != root]
    except OSError:
        pass

    found = []
    for version_dir in candidates:
        legacy = version_dir / "data"
        try:
            if legacy.resolve() == data.resolve():
                continue  # not stranded: it is where we are already writing
        except OSError:
            continue
        if any((legacy / name).exists() for name in MIGRATABLE):
            found.append(legacy)
    return sorted(found, key=lambda p: p.stat().st_mtime)
