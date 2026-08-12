"""The user's own job-search folder.

The plugin's state — venv, database, config, logs — lives in ``${CLAUDE_PLUGIN_DATA}``
and always will. This module owns the one directory that belongs to the *user*: the
folder they created, opened Claude Code in, and expect to find their search in.

    <workspace>/resume/original/<resume>.pdf
    <workspace>/hireshire_run_results/<stamp>/<stamp>_results.csv

Everything here runs at setup time only. The absolute path is written to
``scraper.workspace_dir`` once; the engine reads it from there and never looks at
the working directory (see ``hireshire/paths.py``).

The resume is *copied* in rather than referenced where it sits, so the folder is
self-contained: one directory to back up, move or delete.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from hireshire import paths
from hireshire.matcher.resume import extract_resume_text


class WorkspaceError(ValueError):
    """The chosen folder cannot serve as a workspace."""


def _clean(value: str | Path) -> Path:
    # Windows Explorer's "Copy as path" wraps the path in quotes; users paste it whole.
    return Path(str(value).strip().strip('"').strip("'")).expanduser()


def init_workspace(path: str | Path) -> Path:
    """Validate the chosen folder, create the skeleton, return its absolute path.

    Safe to call on a folder the user made a minute ago and left empty — that is
    the documented flow.
    """
    p = _clean(path).resolve()

    # ROOT is replaced wholesale on every plugin update. A workspace inside it
    # would take the user's entire search history with it, silently. This is
    # reachable: someone running Claude Code from a plugin checkout has cwd == ROOT,
    # and cwd is what setup offers as the default.
    if p == paths.ROOT or paths.ROOT in p.parents:
        raise WorkspaceError(
            f"{p} is inside the plugin's install directory, which is deleted and "
            "replaced on every update — anything kept there would be lost. "
            "Choose a folder of your own."
        )
    if p == paths.DATA or paths.DATA in p.parents:
        raise WorkspaceError(
            f"{p} is inside the plugin's private data directory. Choose a folder "
            "of your own — somewhere you would look for your own files."
        )
    if p == Path.home():
        raise WorkspaceError(
            "Choose a dedicated folder rather than your home directory — this "
            "creates subfolders inside whatever you pick."
        )

    try:
        (p / paths.RESUME_SUBDIR).mkdir(parents=True, exist_ok=True)
        (p / paths.RUN_RESULTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"Could not create the folder structure in {p}: {exc}") from exc
    return p


def find_resumes(workspace: str | Path) -> list[Path]:
    """PDFs already sitting in ``<workspace>/resume/original/``, newest first.

    The documented flow is: make a folder, drop the resume in, launch. When the
    user has done that, setup should recognise it rather than ask for a path.
    """
    d = _clean(workspace) / paths.RESUME_SUBDIR
    if not d.is_dir():
        return []
    return sorted(d.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)


def install_resume(src: str | Path, workspace: str | Path) -> Path:
    """Validate ``src``, copy it into ``<workspace>/resume/original/``, return the
    copy's absolute path.

    That returned path — not the one the user typed — is what belongs in
    ``matcher.resume_path`` and ``applier.resume_path``.
    """
    src = _clean(src).resolve()

    # Validate BEFORE copying. A scanned PDF must not litter the user's folder with
    # a file the pipeline will reject, and the error has to reach them now, while
    # they can still pick a different file — not three minutes into the first sweep.
    extract_resume_text(src)          # FileNotFoundError / ValueError propagate as-is

    dest_dir = (_clean(workspace) / paths.RESUME_SUBDIR).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    if src.parent == dest_dir:
        return src                    # already where it belongs; never copy onto itself

    dest = dest_dir / src.name
    if dest.exists():
        if dest.samefile(src) or filecmp.cmp(src, dest, shallow=False):
            return dest               # re-running setup with the same file is a no-op
        # Same name, different bytes. Never overwrite something the user put there
        # themselves — an older resume they meant to keep is exactly what this is.
        stem, suffix, n = dest.stem, dest.suffix, 2
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1

    shutil.copy2(src, dest)
    return dest
