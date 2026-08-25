#!/bin/sh
# Hook entry point for the PreToolUse guard. See scripts/approve.py for what it
# decides and why.
#
# This wrapper exists for one reason: cost. The guard fires on every Bash call in
# a session where the plugin is enabled, and starting Python means running the
# launcher's interpreter probe first — several subprocesses, on a hook that blocks
# the tool call. The overwhelming majority of those calls have nothing to do with
# this plugin, and a substring test rules them out for the price of a shell
# builtin.
#
# Failing open is deliberate and safe: no output means no decision, and the user
# is prompted exactly as they are today.

payload=$(cat)

case "$payload" in
    *hireshire.sh*|*plugin_hireshire_playwright*) ;;
    *) exit 0 ;;
esac

# Delegate through the launcher, which is the only place allowed to name an
# interpreter — macOS has no bare `python`, and Windows ships a Store stub called
# `python3` that exists on PATH and does not work.
printf '%s' "$payload" | sh "$(dirname -- "$0")/hireshire.sh" --approve
exit 0
