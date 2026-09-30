"""Which CLI drives one application, and how its answer is read back.

The apply session used to be `claude -p` and nothing else. It can now also be
`codex exec`, so a user whose Claude allowance is the binding constraint can put the
browser-driving half of the sweep on their ChatGPT plan instead — `applier.provider`,
asked by `/hireshire:setup` when auto-apply is switched on.

**There is no runtime failover, by design**, and `make_session` mirrors
`matcher.scorer.make_backend` exactly for it: one provider is chosen from config, one
session class is built, and an unbuildable one raises. `run_apply_worker` turns that
raise into its existing `blocked` state, which records nothing and leaves every job
shortlisted for the backlog — a *deferral*. Retrying a job on the other CLI would be a
second browser session against a form the first may already have submitted, which is
the one thing the applier must never risk.

The two CLIs differ in four ways that each needed probing (codex-cli 0.157.0):

* **Codex exposes MCP tools under their bare names** (`browser_navigate`), with the
  server as a separate field on the event rather than part of the tool name. Claude
  Code namespaces them (`mcp__playwright__browser_navigate`). `tool_prefix` carries the
  difference into the prompt, so `apply_one.md` stays one file — see
  `worker.TOOL_PREFIX_TOKEN`.
* **`approval_policy="never"` alone denies every MCP tool call**, with
  `"MCP tool call requires approval, but approval policy is never"` — measured, on the
  first navigate. Neither the sandbox mode nor `default_tools_approval_mode="auto"`
  changes it; only `"approve"` pre-approves the server's tools. The accepted values are
  `auto | prompt | writes | approve`, and `auto` is *not* the permissive one.
* **The sandbox must be `workspace-write`, not the judge's `read-only`.** Codex's
  sandbox reaches the MCP server's operations, not merely its own shell: under
  `read-only`, `browser_navigate` to a `file://` URL failed outright. Under
  `workspace-write` the navigation, the form and the screenshot all work, and no
  `network_access` override is needed — the browser is its own process, so the network
  restriction that covers Codex's shell does not apply to it. The writable root is
  `-C`, which is `dirs.cwd`, and `worker.session_dirs` already guarantees cwd contains
  `out_dir`; the screenshot is therefore inside the writable root by construction.
* **The answer is the LAST `agent_message`, and here that is load-bearing rather than
  tidy.** One successful probe run emitted four: three premature
  `{"status":"submitted","screenshot":null}` before the form had been touched, then the
  real outcome. Reading the first would have recorded a submitted application with no
  screenshot for a form nobody had filled. `codex_cli.parse_events` already takes the
  last one (openai/codex#19816).

`--output-schema` does survive with MCP tools active at this version with this
`--disable` list, which openai/codex#15451 warns it may not; that was the gate this
feature had to clear before any of it was written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import ValidationError

from hireshire import claude_cli, codex_cli, paths
from hireshire.applier.config import ApplierSettings


class UnreadableResult(RuntimeError):
    """The session exited cleanly but its answer could not be read as an outcome.

    Never a deferral: a clean exit may have come *after* the submit click, so the
    caller records it as an `error` telling the user to check rather than retrying it.
    """


class ApplySession:
    """One provider's way of running an apply session and reading its answer."""

    #: How the browser tools are named to this session's model, as a prefix on the
    #: bare Playwright tool names. Substituted into `apply_one.md`.
    tool_prefix: str = ""

    def argv(self, scratch: Path, dirs) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        raise NotImplementedError

    def parse(self, stdout: bytes):
        raise NotImplementedError

    def exit_detail(self, stdout: bytes, stderr: bytes, returncode: int | None) -> str:
        raise NotImplementedError


class ClaudeApplySession(ApplySession):
    """`claude -p`, on the user's Claude subscription. The default and the original.

    The prompt goes on stdin, never in argv: it opens with a Markdown heading today,
    but the SKILL.md it was split out of opened with `---`, which the CLI parsed as an
    option and failed every apply phase with. stdin also has no length limit and keeps
    the resume off the process table. `--no-session-persistence` for the scorer's
    reason: one transcript and one billed title per job, for nothing.

    The session brings its own browser server. A `claude -p` the engine starts is not
    guaranteed to load the plugin, and measured on a dev machine it did not: the
    `mcp__plugin_hireshire_playwright__*` tools were absent and only an unrelated
    user-level server was there. `--strict-mcp-config` pins it to the plugin's own
    `.mcp.json` whatever is installed, which also keeps the user's other MCP servers
    out of an unattended session — at the cost that the tools are named
    `mcp__playwright__*` here, which is what `tool_prefix` tells `apply_one.md`.

    Note what is deliberately absent: `--model` and `--effort` (this session takes the
    CLI's own defaults), and `--safe-mode`/`--tools ""`, which the judge sets and which
    would leave this session with no browser at all.
    """

    tool_prefix = "mcp__playwright__"

    def __init__(self, settings: ApplierSettings) -> None:
        self._settings = settings

    def argv(self, scratch: Path, dirs) -> list[str]:
        from hireshire.applier import worker

        return [
            "claude", "-p",
            "--permission-mode", "auto",
            "--no-session-persistence",
            "--mcp-config", worker._mcp_config(scratch),
            "--strict-mcp-config",
            "--output-format", "json",
            "--json-schema", json.dumps(worker.ApplyOutcome.model_json_schema()),
        ]

    def env(self) -> dict[str, str]:
        return claude_cli.subscription_env()

    def parse(self, stdout: bytes):
        from hireshire.applier.worker import ApplyOutcome

        raw = stdout.decode(errors="replace")
        try:
            return ApplyOutcome.model_validate(
                claude_cli.unwrap_envelope(json.loads(raw))
            )
        except (json.JSONDecodeError, RuntimeError, ValidationError) as exc:
            raise UnreadableResult(raw[:500]) from exc

    def exit_detail(self, stdout: bytes, stderr: bytes, returncode: int | None) -> str:
        # The reason lives in the envelope's `is_error`/`result`, and it is reported in
        # place of the raw stream it came from. Reading those by name is what fixed the
        # night of nine `exited 1` deferrals that logged their token counts and nothing
        # else — `envelope_failure` carries the argument.
        out = stdout.decode(errors="replace")
        return (
            f"claude CLI exited {claude_cli.describe_exit(returncode)}: "
            f"{claude_cli.exit_detail(claude_cli.envelope_failure(out) or out, stderr)}"
        )


class CodexApplySession(ApplySession):
    """`codex exec`, on the user's ChatGPT plan.

    Same contract as the Claude session — one browser, one job, one structured
    `ApplyOutcome` — reached through a CLI that differs in every mechanism. The module
    docstring holds the four differences and how each was measured.

    The tools are stripped to the same set the judge strips
    (`codex_cli.APPLY_DISABLED_FEATURES`), which costs this session nothing: an MCP
    server is config, not a feature. `browser_use` and `computer_use` in particular
    stay off precisely *because* this session has a browser — Codex's own would honour
    neither the Playwright server's `--output-dir` nor the roots rule `session_dirs`
    exists to enforce.
    """

    tool_prefix = ""

    def __init__(self, settings: ApplierSettings) -> None:
        self._exe = shutil.which("codex")
        if not self._exe:
            raise EnvironmentError(
                "codex CLI not found on PATH. Install Codex and run `codex login`."
            )
        if not settings.model or codex_cli.is_claude_model(settings.model):
            raise ValueError(
                f"applier.model is {settings.model!r}, which is not a Codex model. "
                "Run /hireshire:setup and choose a model for the codex provider."
            )
        self._settings = settings
        # Under CODEX_DIR rather than the run folder, for the same reason the judge's
        # `scoring_schema.json` is: it is constant per release, and DATA survives an
        # update. `--output-schema` takes a path, never the JSON itself.
        paths.CODEX_DIR.mkdir(parents=True, exist_ok=True)
        self._schema_path = paths.CODEX_DIR / "apply_outcome_schema.json"
        from hireshire.applier.worker import ApplyOutcome

        self._schema_path.write_text(
            json.dumps(codex_cli.strict_schema(ApplyOutcome.model_json_schema())),
            encoding="utf-8",
        )
        known = codex_cli.available_features()
        self._disabled = [
            f for f in codex_cli.APPLY_DISABLED_FEATURES if known is None or f in known
        ]

    def argv(self, scratch: Path, dirs) -> list[str]:
        from hireshire.applier import worker

        disable: list[str] = []
        for feature in self._disabled:
            disable += ["--disable", feature]
        return [
            self._exe, "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            # The working root is where the screenshots go, NOT CODEX_DIR: the judge
            # touches no files, while this session uploads a resume and writes a PNG.
            # It is also `workspace-write`'s writable root — see the module docstring.
            "-C", str(dirs.cwd),
            "-s", "workspace-write",
            "-c", 'approval_policy="never"',
            "-m", self._settings.model,
            "-c", f'model_reasoning_effort="{self._settings.effort}"',
            "-c", "project_doc_max_bytes=0",
            "-c", "include_environment_context=false",
            "-c", "skills.max_context_tokens=1",
            "-c", "agents.enabled=false",
            "-c", 'web_search="disabled"',
            *worker._mcp_overrides(scratch),
            *disable,
            "--output-schema", str(self._schema_path),
            "-",
        ]

    def env(self) -> dict[str, str]:
        return codex_cli.subscription_env()

    def parse(self, stdout: bytes):
        from hireshire.applier.worker import ApplyOutcome

        # `usage` is deliberately dropped: the applier has no `UsageTally` to meter it
        # into, unlike the judge. Reading it and discarding it would look like an
        # oversight, so this says it is not one.
        answer, _usage, error = codex_cli.parse_events(stdout)
        if answer is None:
            # A failed turn with no answer is UNREADABLE, not a launch failure — and
            # this is where the mapping deliberately differs from `CodexBackend`'s,
            # which raises for the matcher to retry. A session that drove a form for
            # ten minutes and then failed its turn may already have submitted it, so
            # the job is retired with a message telling the user to check rather than
            # handed back to the backlog for a second application.
            raise UnreadableResult(error or stdout.decode(errors="replace")[:500])
        try:
            return ApplyOutcome.model_validate(json.loads(answer))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise UnreadableResult(answer[:500]) from exc

    def exit_detail(self, stdout: bytes, stderr: bytes, returncode: int | None) -> str:
        _answer, _usage, error = codex_cli.parse_events(stdout)
        detail = error or claude_cli.exit_detail(
            stdout.decode(errors="replace")[:500], stderr
        )
        return f"codex CLI exited {returncode}: {detail}"


_SESSIONS: dict[str, type[ApplySession]] = {
    "claude_code": ClaudeApplySession,
    "codex": CodexApplySession,
}


def make_session(settings: ApplierSettings) -> ApplySession:
    """The session for `applier.provider`, or raise if it cannot be built.

    Empty means `claude_code`, the same default an empty `matcher.provider` gets. There
    is deliberately **no `LLM_PROVIDER` env fallback**, which the matcher has for its
    BYO-key era: an environment variable set for scoring must never silently move the
    browser onto another CLI.

    Raises `EnvironmentError` or `ValueError`, both of which `run_apply_worker` turns
    into the blocked deferral — nothing recorded, every job left for the backlog.
    """
    provider = (settings.provider or "claude_code").lower()
    cls = _SESSIONS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown applier.provider '{provider}'. "
            f"Choose from: {', '.join(_SESSIONS)}"
        )
    return cls(settings)
