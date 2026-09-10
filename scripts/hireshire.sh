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
#   hireshire.sh --paths                  print ROOT= and DATA=; installs nothing
#   hireshire.sh --status                 is a recurring sweep running? installs nothing
#   hireshire.sh --stop                   stop a running sweep; installs nothing
#   hireshire.sh --session-end            SessionEnd hook; hook payload on stdin
#   hireshire.sh --approve                PreToolUse guard; reads a hook payload on stdin
#   hireshire.sh --bootstrap              create/refresh the venv
#   hireshire.sh --monitor                run the recurring sweep (start in background)
#   hireshire.sh --sweep                  run ONE sweep, then exit (find-jobs, scheduler)
#   hireshire.sh <script.py> [args...]    run an engine entrypoint in the venv
#
# --paths exists because skills must not name the data directory themselves.
# `${CLAUDE_PLUGIN_DATA}` expands to a different directory in the Claude desktop
# app than in the terminal or the VS Code extension, so a skill that substitutes
# it writes somewhere the engine never reads.
#
# --status exists because a skill must not claim a sweep is running without asking.
# --monitor is the only entrypoint that honours the user's poll_interval_hours;
# `orchestrate.py` defaults to 4 hours and never reads their config.
#
# --stop is the counterpart to --monitor. The sweep is meant to end with the session
# that started it, but on Windows it has outlived one more than once, leaving a
# sweeper on the database that the user could only reach through Task Manager. The
# kill is tree-wide because the monitor re-execs twice and the pid on record is the
# leaf: /T reaches the apply subprocess and the browser under it, and the parents
# unwind by themselves, each being blocked in subprocess.run waiting on its child.
#
# --session-end is what makes that automatic. It is the SessionEnd hook, and it reuses
# --stop's kill after checking the payload's `reason`, because `clear` and `resume`
# are not endings — killing a sweep on /clear would be a new bug of the same shape.
# It cannot fire when Claude Code is force-killed, which is why the sweep also watches
# CLAUDE_PID for itself. Neither half is sufficient alone, and neither involves this
# script: see the note on --monitor below for why no pid is passed from the shell.
#
# --approve is what the PreToolUse hook runs, via scripts/approve.sh. It decides
# whether a command is one of this plugin's own and can skip the permission prompt,
# which is what stops setup asking a dozen times for its own plumbing. It installs
# nothing and imports only the stdlib, because it has to answer on a machine where
# the venv does not exist yet — the first thing setup runs is the install itself.
#
# --check is what the SessionStart hook runs. It must stay fast: a hook blocks the
# user's first turn, so anything slow there is silence they cannot explain. The
# ~2 GB install belongs to --bootstrap, which the setup skill runs *after* telling
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
    --paths)     exec "$PY" "$ROOT/scripts/bootstrap.py" --paths ;;
    --status)    exec "$PY" "$ROOT/scripts/bootstrap.py" --status ;;
    --stop)      exec "$PY" "$ROOT/scripts/bootstrap.py" --stop ;;
    --session-end) exec "$PY" "$ROOT/scripts/bootstrap.py" --session-end ;;
    --approve)   exec "$PY" "$ROOT/scripts/approve.py" ;;
    --bootstrap) exec "$PY" "$ROOT/scripts/bootstrap.py" ;;
    # NOTHING about the session is passed from here, and that is the fix for a real
    # bug: this branch used to export $$ and $PPID for the sweep's watchdog to poll.
    # Git Bash is MSYS and MSYS keeps its OWN pid namespace, so those are not Windows
    # pids — `ps` shows PID 1684 against WINPID 14072 for the same shell. The watchdog
    # polls them with Win32 OpenProcess, which knows only Windows pids, so it read two
    # meaningless numbers as dead and killed a healthy sweep 60 seconds in.
    # `run_orchestration.py` reads CLAUDE_PID out of the environment instead: Claude
    # Code sets it, in the right namespace, and it is inherited without help.
    --monitor)   exec "$PY" "$ROOT/scripts/run_orchestration.py" ;;
    # --sweep is one cycle of exactly the same program, and it is what
    # /hireshire:find-jobs and the OS scheduler entry run. It replaces
    # `hireshire.sh orchestrate.py --once`, which went through run_engine.py — a second
    # launcher with no status registration and no session watchdog, so a find-jobs sweep
    # was invisible to --status, unreachable by --stop, and outlived its session.
    --sweep)     exec "$PY" "$ROOT/scripts/run_orchestration.py" --once ;;
    "")          echo "usage: hireshire.sh [--check|--paths|--status|--stop|--session-end|--approve|--bootstrap|--monitor|--sweep|<script.py> [args]]" >&2; exit 2 ;;
    *)           exec "$PY" "$ROOT/scripts/run_engine.py" "$@" ;;
esac
