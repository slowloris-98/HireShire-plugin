"""Create (or refresh) the plugin's Python venv in ${CLAUDE_PLUGIN_DATA}.

Run from the SessionStart hook. Two rules shape this file:

* The venv goes in the **data** dir, never the install dir. The install dir is
  replaced wholesale on every plugin update, which would silently delete a
  2.5 GB torch install and leave the engine unable to import.
* Idempotency is decided by comparing the shipped requirements against a lock
  copy in the data dir, not by testing whether the venv directory exists. A
  half-finished install leaves a directory behind; it does not leave a matching
  lock file, so the next session repairs it.

Also deliberately dependency-free: it runs on the system interpreter before the
venv exists, so it may only import the standard library.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or (ROOT / "data"))

VENV_DIR = DATA / "venv"
REQUIREMENTS = ROOT / "requirements-core.txt"
LOCK = DATA / "requirements.lock"


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    """The interpreter inside the venv, by absolute path.

    Everything downstream must invoke this rather than a bare `python`: Claude
    Code's hook exec form cannot spawn the `.cmd`/`.bat` shims Windows installs,
    and a bare `python` there can resolve to the Microsoft Store alias stub.
    """
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def is_current() -> bool:
    """True when the venv exists and was built from the shipped requirements."""
    if not venv_python().exists() or not LOCK.exists():
        return False
    if not REQUIREMENTS.exists():
        return True
    return LOCK.read_bytes() == REQUIREMENTS.read_bytes()


def main() -> int:
    if is_current():
        return 0

    DATA.mkdir(parents=True, exist_ok=True)

    py = venv_python()
    if not py.exists():
        print(f"HireShire: creating venv at {VENV_DIR} (this takes a moment)", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)

    if not REQUIREMENTS.exists():
        print(f"HireShire: no requirements file at {REQUIREMENTS}", file=sys.stderr)
        return 1

    print(
        "HireShire: installing dependencies. The first run downloads PyTorch and "
        "two small transformer models (~2.5 GB) and can take several minutes.",
        flush=True,
    )
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "-q", "-r", str(REQUIREMENTS)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Leave the lock absent so the next session retries rather than assuming
        # a broken environment is good.
        print(f"HireShire: dependency install failed\n{result.stderr[-2000:]}", file=sys.stderr)
        return result.returncode

    # Only now, on success, does the lock get written.
    LOCK.write_bytes(REQUIREMENTS.read_bytes())
    print("HireShire: ready. Run /hireshire:setup to get started.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
