# Setup, skills and the plugin shell

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

## Setup writes config

- **Setup never shows YAML.** `hireshire/config_writer.py` is a whitelisted,
  ruamel-based writer that preserves comments and CRLF and validates the patched
  document against the phase's pydantic model *before* writing. **The writer is
  YAML 1.2 and every reader is PyYAML, which is YAML 1.1**, so a string like `no`,
  `on` or `off` is written bare and read back as a bool. That shipped in 0.15.0:
  `disability: no` failed `ApplierSettings`, and `run_orchestration` turned auto-apply
  off for the whole sweep. So `_quote_ambiguous` asks PyYAML about every string it
  writes, and validation runs on the **PyYAML re-parse of the rendered text**, never
  on ruamel's tree — the tree is what passed that file. Do not "simplify" either back.
- **Setup edits what the last run left behind, so every branch must write its own keys
  — including the ones whose value is the default.** The config lives in DATA and
  survives updates and re-runs, so a key setup does not write keeps its old value. That
  is invisible on a fresh install, where every default is already correct, and it breaks
  the moment a user *changes* an answer. It shipped that way on all four provider
  branches: question 10's `claude_code` and API-key options and question 11's
  `claude_code` option described a provider and never wrote one, so choosing Claude
  after using Codex left `provider: codex` in the file and the sweep carried on using
  Codex while the user had just been told otherwise.

  The scoring half also has to write `model`, and that is the sharper edge:
  `ClaudeCodeBackend` passes `matcher.model` to `claude -p` and validates nothing (the
  `codex_cli.is_claude_model` guard runs only in the Codex direction), so a Codex model
  left behind fails every scoring call — the breaker trips and the remainder is
  `backend_unavailable`, which is at least retryable and printed. **No guard was added
  for that direction**: a name the regex does not match can still be legitimate, a
  Bedrock `us.anthropic.claude-*` among them, and the console already prints
  `provider/model` on every sweep. `applier.model` is genuinely inert for the Claude
  session, which passes no `--model`, so a leftover there is harmless — and
  `tests/test_apply_worker.py` pins that, so restoring the flag "for symmetry" cannot
  break switching back quietly.

  `tests/test_plugin_shell.py::test_every_provider_branch_writes_its_own_provider_key`
  checks this over the skill's `bash` blocks rather than its prose, because a branch that
  only *describes* its write is exactly the bug. Same family as the "a shipped default
  never reaches an existing install" rule behind `exclude_companies`/`amazon`.


## Skills, permissions, packaging

- **Plugin-bundled MCP tools are namespaced** `mcp__plugin_hireshire_playwright__*`,
  not `mcp__playwright__*`. A rule written against the bare server key never fires.
- **A skill must not state runtime facts it has not asked for.** Three live failures of
  this kind cost a user real trust: one skill announced a running sweep that did not
  exist, another wrote the search profile to a directory it had guessed, and
  `start-orchestration` promised sweeps stopped with the session long after that had
  stopped being true. `--paths` answers the directory question and the skills are
  required to ask; the liveness question has no launcher answer any more, so a skill
  reports what the background task and its startup line told it and nothing further.
  `tests/test_plugin_shell.py` greps for all three regressions.
- **A skill may write `${CLAUDE_PLUGIN_ROOT}`; it must never write
  `${CLAUDE_PLUGIN_DATA}`.** Claude Code expands both inside skill content, but the
  data one resolves differently per interface (see `docs/dev/paths-and-state.md`), so a
  skill that substitutes it writes where the engine never reads — and nothing fails
  loudly. Skills call `hireshire.sh --paths`, which prints `ROOT=` and `DATA=` and
  works before the venv exists. `tests/test_plugin_shell.py` fails the build if the
  placeholder reappears. This is the same "solve it in one place" argument as
  interpreter discovery, applied to directory discovery.
- **Every command a skill runs must be a fixed shape, because permission rules match
  the exact command string.** Setup used to run its Python by writing a heredoc to a
  temp file, so no two calls ever matched, no allowlist rule could cover them, and a
  first-time user approved ~15 dialogs before seeing a job. `scripts/setup_cli.py`
  exists to make each action one stable argv; `scripts/approve.py` is a `PreToolUse`
  hook that recognises those shapes and returns `permissionDecision: "allow"`, which
  is the supported way for a plugin to stop asking for its own plumbing.

  Three constraints on that guard, which is now a security boundary — whatever it
  approves runs with no prompt, ever:

  - **Silence is the default.** Unrecognised input produces no output and the user is
    asked exactly as before, so a bug in the guard costs friction, not authority.
  - **It never approves the launcher's bare `<script.py>` form**, which runs an
    arbitrary file. That is why `setup_cli.py` had to come first: nothing legitimate
    needs the escape hatch any more.
  - **It must stay stdlib-only and pre-venv**, like `bootstrap.py` — the first command
    it approves is the install that creates the venv.

  `approve.sh` wraps it with a substring pre-filter so unrelated Bash calls do not pay
  for the interpreter probe. Note that Windows needs three path spellings compared
  (`/d/...` from Git Bash, `C:/...`, `C:\...`) or the guard silently never matches.
  It approves **no** browser tool. The read-only three it used to approve served
  `/hireshire:apply`, removed with that skill along with its Playwright hook matcher;
  `tests/test_plugin_shell.py` fails if the matcher reappears. Note the
  sweep's apply worker runs each per-job session as `claude -p --permission-mode auto`
  and therefore bypasses it entirely: unattended auto-apply has no human checkpoint by
  design.

- **`userConfig` is not used** for anything load-bearing — its enable-time prompt
  has open bugs. The `setup` skill is the source of truth.
- **`.claude/settings.json` is gitignored, and must stay that way.** Same argument as
  the root `CLAUDE.md`: an install is a copy of this tree, so anything here ships. It
  once did — 51 dev allow rules (`sed -i`, `git mv`, scratchpad paths) plus
  `additionalDirectories` naming a developer's drive. The rules never fired, because a
  plugin directory is never trusted, but the refusal printed **645 characters to stderr
  on every judge call**, which is what let a real scoring failure be misread as a trust
  warning. Permissions a user needs are granted by `scripts/approve.py`, one recognised
  command at a time; a settings file is the opposite of that boundary. Dev permissions
  belong in `.claude/settings.local.json`, ignored alongside it.
- **Set an explicit `version` in `plugin.json`.** Omitting it pushes every commit at
  users. Semver + `CHANGELOG.md`.
- **The clean-machine test is the real acceptance test**: fresh user dir,
  marketplace add → install → setup → start-orchestration. Anything needing a terminal, a
  `git clone`, or a YAML file is a bug.

