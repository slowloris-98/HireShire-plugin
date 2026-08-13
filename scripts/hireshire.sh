#!/bin/sh
# Single entry point for every way the plugin starts Python.
#
# Finding a usable interpreter is genuinely fiddly across platforms, and getting
# it wrong fails silently at install time, so it is solved once here rather than
# copied into the hook, the monitor and three skills:
#
#   * macOS has no bare `python` at all — Apple removed /usr/bin/python in 12.3
#     and Homebrew installs `python3` only.
#   * Windows ships a Microsoft Store "App Execution Alias" stub named
#     python3.exe that EXISTS on PATH, prints an ad, and exits non-zero. So
#     testing with `command -v` picks the stub and dies while the real `python`
#     sits right next to it. The candidate has to actually be run.
#
# Hence: execute each candidate and keep the first that reports Python >= 3.10.
#
# Usage:
#   hireshire.sh --check                  session-start probe; installs nothing
#   hireshire.sh --bootstrap              create/refresh the venv
#   hireshire.sh --monitor                run the recurring sweep
#   hireshire.sh <script.py> [args...]    run an engine entrypoint in the venv
#
# --check is what the SessionStart hook runs. It must stay fast: a hook blocks the
# user's first turn, so anything slow there is silence they cannot explain. The
# 2.5 GB install belongs to --bootstrap, which the setup skill runs *after* telling
# them how long it will take.

set -e

ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"

find_python() {
    for candidate in python3 python py; do
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PY=$(find_python) || {
    echo "HireShire: no Python 3.10+ found on PATH." >&2
    echo "  macOS:   brew install python" >&2
    echo "  Windows: https://python.org/downloads (tick 'Add to PATH')" >&2
    echo "  Linux:   apt install python3 python3-venv" >&2
    exit 1
}

case "$1" in
    --check)     exec "$PY" "$ROOT/scripts/bootstrap.py" --check ;;
    --bootstrap) exec "$PY" "$ROOT/scripts/bootstrap.py" ;;
    --monitor)   exec "$PY" "$ROOT/scripts/run_orchestration.py" ;;
    "")          echo "usage: hireshire.sh [--check|--bootstrap|--monitor|<script.py> [args]]" >&2; exit 2 ;;
    *)           exec "$PY" "$ROOT/scripts/run_engine.py" "$@" ;;
esac
