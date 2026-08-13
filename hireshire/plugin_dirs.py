"""Where the plugin's two directories are, without trusting the environment.

**`CLAUDE_PLUGIN_DATA` does not name the same directory on every Claude Code
surface, so it cannot be the authority.** Claude Code resolves it from the plugin
*identifier*, and the identifier depends on which interface the user is in: the
terminal and the VS Code extension report `hireshire@hireshire` and get
`data/hireshire-hireshire`, while the Claude desktop app reports the plugin as an
inline source and gets `data/hireshire-inline`. A user who ran `/hireshire:setup`
in the desktop app therefore wrote their generated search profile into one
directory while every engine run read from the other — the profile silently went
missing, the reranker had no query, and the funnel's only precision stage stopped
running. Nothing announces this; the run just gets worse.

ROOT does not have that problem: it is the install directory on all three, and an
installed plugin lives at a known path:

    ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/   <- ROOT
    ~/.claude/plugins/data/<plugin>-<marketplace>/              <- DATA

So DATA is *derived* from ROOT and that derivation wins, even against an explicit
`CLAUDE_PLUGIN_DATA`. The version segment is deliberately not part of the answer,
which is what lets an update keep the user's database. That is a layout owned by
Claude Code rather than by us, so every step is guarded; when it cannot be proven
the environment is consulted after all, and failing that `<repo>/data` — correct
for a bare checkout or a `--plugin-dir` load.

The other half of this rule lives in the skills: they may write
``${CLAUDE_PLUGIN_ROOT}``, which Claude Code expands consistently, but never
``${CLAUDE_PLUGIN_DATA}``. They ask `hireshire.sh --paths` instead, so exactly one
implementation of this reasoning exists. `tests/test_plugin_shell.py` enforces it.

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
    """(ROOT, DATA), deriving DATA from ROOT wherever ROOT is a real install.

    The derivation outranks `CLAUDE_PLUGIN_DATA` on purpose: see the module
    docstring. The env var is only consulted for a ROOT we cannot recognise — a
    checkout, a `--plugin-dir` load, a scratch directory under test — where there
    is nothing to derive from and the caller's explicit answer is the best one
    available.
    """
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    derived = derive_data_dir(root)
    if derived is not None:
        return root, derived
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return root, (Path(env_data) if env_data else root / "data")


def legacy_data_dirs(root: Path, data: Path) -> list[Path]:
    """`data/` directories left inside an install dir by the bug described above.

    Checks this ROOT and its sibling **version** directories, because the update that
    delivers the fix installs to a new version folder — the stranded files are next
    door under the old version, not underneath us. Newest last, so a caller migrating
    in order ends on the most recent.

    The sibling scan only happens when ROOT is a real install
    (`.../cache/<marketplace>/<plugin>/<version>`), where every sibling is by
    definition another version of this same plugin. From a checkout — a `--plugin-dir`
    load, or a developer running from source — ROOT's siblings are whatever else the
    user keeps in that folder, and treating their `data/` directories as ours to move
    is how this function once emptied a neighbouring project. If we cannot prove the
    layout, we look only at ROOT itself.
    """
    candidates = [root]
    if derive_data_dir(root) is not None:
        try:
            candidates += [d for d in root.parent.iterdir() if d.is_dir() and d != root]
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
