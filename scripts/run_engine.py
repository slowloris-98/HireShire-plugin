"""Launcher that re-executes an engine entrypoint inside the plugin's venv.

Hooks and monitors start on the *system* interpreter, which has none of the
engine's dependencies. This is the one file both may invoke directly: it finds
the venv interpreter by absolute path and hands off.

    python scripts/run_engine.py orchestrate.py --once

Absolute paths matter here. Claude Code's hook exec form cannot spawn the
`.cmd`/`.bat` shims Windows installs, and a bare `python` can hit the Microsoft
Store alias stub, so the venv interpreter is always addressed by full path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import DATA, ROOT, is_current, main as bootstrap_main, venv_python  # noqa: E402


def run(argv: list[str]) -> int:
    if not argv:
        print("usage: run_engine.py <script.py> [args...]", file=sys.stderr)
        return 2

    if not is_current():
        rc = bootstrap_main()
        if rc != 0:
            return rc

    script = ROOT / argv[0]
    if not script.exists():
        print(f"run_engine: no such entrypoint: {script}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    # ROOT is what matters: the child derives DATA from it, so pinning ROOT is what
    # guarantees parent and child agree. DATA is passed for legibility — anything
    # inspecting the child's environment sees where it is writing — but it no longer
    # decides anything, because `resolve_dirs` prefers the derivation.
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(ROOT))
    env["CLAUDE_PLUGIN_DATA"] = str(DATA)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Windows defaults stdout to the console's ANSI codepage (cp1252), which cannot
    # encode most of what comes out of a PDF. `setup_cli.py resume-text` died on
    # U+25CF — an ordinary bullet — during a real first run, taking setup with it.
    # Every resume has bullets, so on Windows this was a blocker for essentially
    # every user, and it only appears once a real PDF is involved.
    #
    # Set here rather than in each entrypoint because this is the one place every
    # engine script is launched from, the same argument as interpreter and directory
    # discovery. `:replace` covers the residue: PDF extraction can emit lone
    # surrogates, which are not encodable even in UTF-8, and a mangled character
    # beats a dead run. setdefault so an explicitly-set value still wins.
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")

    return subprocess.run(
        [str(venv_python()), str(script), *argv[1:]],
        cwd=str(ROOT),
        env=env,
    ).returncode


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
