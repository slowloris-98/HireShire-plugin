"""Rules every `codex exec` call the engine makes has to follow, kept in one place.

The Codex counterpart of `claude_cli`: the judge can score on a ChatGPT plan through
the local Codex CLI the same way it scores on a Claude plan through `claude -p`. The
two CLIs differ in ways that each broke something when probed, which is why this
module exists rather than a few flags inline in the scorer:

- `--output-schema` takes a *path*, and the schema is sent as OpenAI strict
  Structured Outputs. A raw pydantic schema is rejected with HTTP 400 until every
  object says `additionalProperties: false` — see `strict_schema`.
- The answer is the **last** `agent_message` in the `--json` event stream, not the
  first. openai/codex#19816 (open as of 0.154.0) applies the schema to intermediate
  messages too, so the first schema-shaped message is not necessarily the answer.
- An `item.completed` of type `error` is not a failure. Trimming the skills catalog
  to nothing emits one on every call ("Exceeded skills context budget"); only a
  top-level `error` or `turn.failed` means the call failed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

# Codex bills an API organisation instead of the ChatGPT plan when either is set —
# `CODEX_API_KEY` is the one `codex exec` documents, `OPENAI_API_KEY` the one
# `load_dotenv()` may have put here for the BYO-key `openai` provider.
_API_AUTH_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

# Features whose tools or context a judge never uses. Each one left on is sent with
# every call: measured on codex-cli 0.154.0, a one-line prompt cost 11,207 input
# tokens as shipped and 1,769 with this list plus the `-c` overrides in the scorer.
# Tools also matter for correctness, not just cost — openai/codex#15451 reports
# `--output-schema` being silently dropped while tools are active.
#
# Filtered through `available_features()` before use, because `--disable` with a name
# this CLI version does not know is an error, and a feature renamed in a later release
# must not turn every judge call into a failure.
JUDGE_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "hooks",
    "skill_search",
    "tool_suggest",
    "sleep_tool",
    "goals",
    "personality",
)

# Keywords OpenAI strict Structured Outputs does not accept. `maxLength` is advertised
# by `ScoringSchema` as a cost hint; the field validators still clip to it after
# parsing, so dropping it here loses the hint and nothing else.
_UNSUPPORTED_KEYWORDS = frozenset({"default", "title", "maxLength", "minLength"})


def subscription_env() -> dict[str, str]:
    """The process environment without the API auth variables, so Codex uses the
    ChatGPT sign-in from `codex login` rather than billing an API key."""
    return {k: v for k, v in os.environ.items() if k not in _API_AUTH_VARS}


def strict_schema(schema: Any) -> Any:
    """Rewrite a pydantic JSON schema into the strict dialect `--output-schema` needs.

    Every object gets `additionalProperties: false` and lists every property as
    required, recursively (including `$defs`); unsupported keywords are dropped.
    `$ref`/`$defs` are left as they are — strict mode supports them.
    """
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_KEYWORDS}
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out


def parse_events(stdout: bytes) -> tuple[str | None, dict | None, str | None]:
    """Read a `codex exec --json` event stream.

    Returns `(answer_text, usage, error)`: the text of the last `agent_message`, the
    `turn.completed` usage block, and the message of a top-level `error` or
    `turn.failed` event. Any of them may be None. Lines that are not JSON are skipped,
    as are item-level `error`s (see the module docstring).
    """
    answer = usage = error = None
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                answer = item.get("text")
        elif kind == "turn.completed":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        elif kind == "turn.failed":
            err = event.get("error")
            error = (err.get("message") if isinstance(err, dict) else None) or str(err)
        elif kind == "error":
            error = str(event.get("message") or event)
    return answer, usage, error


def available_features(timeout: float = 30.0) -> set[str] | None:
    """Feature names this `codex` knows, or None when they could not be listed.

    `codex features list` prints one feature per line, name first. None means the
    caller cannot filter, and should pass the full list rather than none of it — the
    difference is a failed call versus a judge that can run shell commands.
    """
    exe = shutil.which("codex")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "features", "list"], capture_output=True, timeout=timeout,
            env=subscription_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = {
        line.split()[0]
        for line in proc.stdout.decode(errors="replace").splitlines()
        if line.strip()
    }
    return names or None


def check(timeout: float = 60.0) -> dict:
    """What setup needs to offer the `codex` provider, without starting a model.

    `installed` and `logged_in` gate the choice; `models` are the ones the catalog
    marks for listing, each with the efforts it supports that this engine accepts
    (`ultra` "delegates automatically", which a one-shot judge must not do). Only
    read-only subcommands run here, and nothing is billed.
    """
    result: dict = {"installed": False, "version": None, "logged_in": False,
                    "auth": None, "models": []}
    exe = shutil.which("codex")
    if not exe:
        return result
    result["installed"] = True
    env = subscription_env()

    def _run(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run([exe, *args], capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError):
            return None

    proc = _run("--version")
    if proc and proc.returncode == 0:
        result["version"] = proc.stdout.decode(errors="replace").strip()
    proc = _run("login", "status")
    if proc is not None:
        # The status line goes to stderr on some versions and stdout on others.
        text = (proc.stdout + proc.stderr).decode(errors="replace").strip()
        result["auth"] = text.splitlines()[0] if text else None
        result["logged_in"] = proc.returncode == 0 and "logged in" in text.lower()
    proc = _run("debug", "models")
    if proc and proc.returncode == 0:
        result["models"] = _listed_models(proc.stdout)
    return result


_JUDGE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _listed_models(raw: bytes) -> list[dict]:
    try:
        catalog = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return []
    entries = catalog.get("models", []) if isinstance(catalog, dict) else catalog
    models = []
    for m in entries if isinstance(entries, list) else []:
        if not isinstance(m, dict) or m.get("visibility") != "list" or not m.get("slug"):
            continue
        levels = [
            lvl.get("effort") if isinstance(lvl, dict) else lvl
            for lvl in m.get("supported_reasoning_levels") or []
        ]
        models.append({
            "model": m["slug"],
            "name": m.get("display_name") or m["slug"],
            "efforts": [e for e in _JUDGE_EFFORTS if e in levels],
        })
    return models


def toml_path(path: os.PathLike | str) -> str:
    """A path as a TOML literal string for `-c key=<value>`.

    Forward slashes, single quotes: a Windows backslash inside a basic (double-quoted)
    TOML string is an escape, and `C:\\Users` would fail to parse.
    """
    return "'" + str(path).replace("\\", "/") + "'"
