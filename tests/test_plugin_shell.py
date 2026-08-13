"""Plugin manifest and shell tests.

These catch the class of mistake that only shows up on a clean install, where
nobody is watching: a manifest that fails to load, a skill whose name does not
produce the command users were told to type, or a path written into the install
directory, which is replaced wholesale on every plugin update.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

from hireshire import paths

ROOT = paths.ROOT
sys.path.insert(0, str(ROOT / "scripts"))
SKILLS = ("setup", "find-jobs", "start-orchestration", "apply")


def _json(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_plugin_manifest_is_valid_and_versioned():
    m = _json(".claude-plugin/plugin.json")
    # The name is the invocation namespace: `hireshire` is what makes the
    # documented `/hireshire:setup` work.
    assert m["name"] == "hireshire"
    # An explicit version means users update on a bump instead of on every commit.
    assert m["version"].count(".") == 2


def test_the_plugin_declares_no_monitors():
    """Dropped in 0.2.4. Plugin monitors are experimental and are skipped on hosts
    where the Monitor tool is unavailable, so the orchestration skill's promise that
    sweeps had started was sometimes false and it had no way to notice. The skill now
    starts the sweeper itself and confirms with `--status`."""
    m = _json(".claude-plugin/plugin.json")
    assert "monitors" not in m
    assert "monitors" not in m.get("experimental", {})
    assert not (ROOT / "monitors").exists()


def test_marketplace_points_at_this_repo():
    mk = _json(".claude-plugin/marketplace.json")
    entry = next(p for p in mk["plugins"] if p["name"] == "hireshire")
    # Self-hosting: the plugin IS the repo root.
    assert entry["source"] == "./"


def test_session_start_hook_probes_but_never_installs():
    """The hook blocks the user's first turn, so it must stay fast.

    It used to run `--bootstrap`, which meant a fresh install spent ~4 minutes
    downloading 2.5 GB before the user could be told anything — and the warning that
    explains the wait lives in the setup skill, which cannot run until the hook
    finishes. The download belongs to the skill; the hook only reports.
    """
    hooks = _json("hooks/hooks.json")["hooks"]["SessionStart"]
    cmd = hooks[0]["hooks"][0]
    assert cmd["type"] == "command"
    assert "--check" in cmd["command"]
    assert "--bootstrap" not in cmd["command"], "the hook must not install"
    assert cmd.get("timeout", 0) >= 900


def test_check_mode_cannot_install_anything():
    """`check()` may recover stranded data and report, nothing else."""
    import bootstrap

    called: list[str] = []
    original_env, original_run = bootstrap.venv.EnvBuilder, bootstrap.subprocess.run
    bootstrap.venv.EnvBuilder = lambda *a, **k: called.append("venv")  # type: ignore[assignment]
    bootstrap.subprocess.run = lambda *a, **k: called.append("pip")    # type: ignore[assignment]
    try:
        assert bootstrap.check() == 0
    finally:
        bootstrap.venv.EnvBuilder, bootstrap.subprocess.run = original_env, original_run

    assert called == [], f"check() must not build or install, but ran {called}"


def test_the_launcher_exposes_its_read_only_modes_separately():
    sh = (ROOT / "scripts" / "hireshire.sh").read_text(encoding="utf-8")
    assert "--check)" in sh and "--bootstrap)" in sh and "--monitor)" in sh
    # The two questions a skill must ask rather than assume: where DATA is, and whether
    # a sweep is already running. See the tests below for both.
    assert "--paths)" in sh
    assert "--status)" in sh


@pytest.mark.parametrize("mode", ["paths", "status"])
def test_the_read_only_modes_answer_without_building_anything(mode):
    """Both are questions a skill asks before it can do or say anything, so both must
    return on a machine where the venv does not exist yet."""
    import bootstrap

    called: list[str] = []
    original_env, original_run = bootstrap.venv.EnvBuilder, bootstrap.subprocess.run
    bootstrap.venv.EnvBuilder = lambda *a, **k: called.append("venv")  # type: ignore[assignment]
    bootstrap.subprocess.run = lambda *a, **k: called.append("pip")    # type: ignore[assignment]
    try:
        assert getattr(bootstrap, mode)() == 0
    finally:
        bootstrap.venv.EnvBuilder, bootstrap.subprocess.run = original_env, original_run

    assert called == [], f"--{mode} must answer a question, not build anything"


def test_the_orchestration_skill_verifies_before_it_reports():
    """It used to announce that sweeps had started purely because it had been invoked,
    so users were told a sweep was live when nothing was running."""
    text = (ROOT / "skills" / "start-orchestration" / "SKILL.md").read_text(encoding="utf-8")
    assert "--status" in text, "the skill must check the state it reports"


def _shell_blocks(text: str) -> str:
    """The ```bash fences only. Prose is exempt on purpose: the skills state these
    rules by naming the thing they forbid, and a check over the whole file would fail
    on its own instructions."""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = line.strip().startswith(("```bash", "```sh"))
            continue
        if inside:
            out.append(line)
    return "\n".join(out).lower()


@pytest.mark.parametrize("skill", SKILLS)
def test_no_skill_runs_a_command_that_detaches_a_process(skill):
    """A session that improvised `nohup … & disown` left a sweeper running after the
    session closed — while telling the user it would stop with the session. Anything
    the plugin starts has to be a child of the session that started it."""
    blocks = _shell_blocks((ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"))
    for detach in ("nohup", "disown", "setsid", "start /b"):
        assert detach not in blocks, (
            f"{skill}: background it through the harness, not by detaching ({detach})"
        )


def test_the_setup_skill_asks_for_selectable_options():
    """Left to judgment, the same skill text produced tappable options in some runs and
    a wall of numbered prose in others."""
    text = (ROOT / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "AskUserQuestion" in text


@pytest.mark.parametrize("skill", SKILLS)
def test_no_skill_substitutes_the_data_placeholder(skill):
    """Claude Code expands this placeholder in skill content, and it does NOT expand
    to the same directory on every interface: the terminal and the VS Code extension
    get `data/hireshire-hireshire`, the Claude desktop app gets `data/hireshire-inline`.

    A skill that writes there is writing somewhere the engine never reads, and nothing
    reports it — a setup run in the desktop app put the generated search profile in the
    wrong directory, which silently disabled the reranker for every later sweep. The
    launcher's `--paths` is the one supported answer.

    `${CLAUDE_PLUGIN_ROOT}` is deliberately still allowed: it is the install directory
    on all three, and every skill uses it to reach the launcher.
    """
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_DATA}" not in text, (
        f"{skill}: ask `hireshire.sh --paths` instead of substituting the placeholder"
    )


def test_every_launch_path_goes_through_the_one_launcher():
    """Interpreter discovery is solved in exactly one place. Two platform traps
    make that worth enforcing: macOS has no bare `python` (Apple removed
    /usr/bin/python in 12.3), and Windows ships a Microsoft Store stub named
    python3.exe that exists on PATH, prints an ad and exits non-zero — so
    `command -v python3` picks the broken one while the real `python` sits next
    to it."""
    hook = _json("hooks/hooks.json")["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    assert "hireshire.sh" in hook
    assert "python" not in hook, "interpreter choice belongs in the launcher"


def test_launcher_probes_by_execution_not_by_existence():
    sh = (ROOT / "scripts" / "hireshire.sh").read_text(encoding="utf-8")
    # Ignore comments — they discuss `command -v` precisely to explain the trap.
    code = "\n".join(
        line for line in sh.splitlines() if not line.lstrip().startswith("#")
    )
    assert "command -v" not in code, "existence checks pick the Windows Store stub"
    assert "sys.version_info" in code, "must confirm the candidate actually runs"
    # python3 first so macOS resolves; python next so Windows does.
    assert "python3 python py" in code


@pytest.mark.parametrize("skill", SKILLS)
def test_skills_never_hardcode_a_bare_python(skill):
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("python ", "python\t", '"$PY"')):
            pytest.fail(f"{skill}: bypasses the launcher -> {stripped[:70]}")


def test_venv_interpreter_path_branches_by_platform():
    import bootstrap

    posix = bootstrap.venv_python(pathlib.Path("/tmp/v"))
    assert posix.name in ("python", "python.exe")
    # The branch exists at all — the actual value depends on the host we run on.
    assert "Scripts" in str(posix) or "bin" in str(posix)


def test_mcp_server_is_portable():
    """The source repo hardcoded an absolute Windows npx.cmd path."""
    mcp = _json(".mcp.json")["mcpServers"]["playwright"]
    assert mcp["command"] == "npx"
    assert "@playwright/mcp@latest" in mcp["args"]


@pytest.mark.parametrize("skill", SKILLS)
def test_every_skill_has_frontmatter_with_a_matching_name(skill):
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert f"name: {skill}" in front, "frontmatter name drives the command name"
    assert "description:" in front


def test_apply_skill_uses_plugin_namespaced_playwright_tools():
    """A plugin-bundled MCP server's tools are `mcp__plugin_<plugin>_<server>__*`.
    A rule written against the bare server key never fires."""
    text = (ROOT / "skills" / "apply" / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__plugin_hireshire_playwright__browser_navigate" in text
    # The legacy names from the source repo were never real tools.
    for stale in ("playwright_navigate", "playwright_upload_file", "playwright_fill"):
        assert stale not in text


def test_no_mutable_state_is_written_into_the_install_dir():
    """ROOT is replaced on every update. Anything written there is lost."""
    import scraper

    for p in (scraper.USER_BAD_SLUGS_PATH, scraper.USER_RECOVERED_PATH,
              paths.DB_PATH, paths.RESULTS_DIR, paths.USER_CONFIG):
        assert paths.DATA in p.parents or p == paths.DATA, f"{p} must live under DATA"

    # ...and the read-only seed is the one thing that does come from the install dir.
    assert scraper.SEED_BAD_SLUGS_PATH.parent == paths.SHIPPED_CONFIG


def test_results_leave_the_data_dir_only_for_a_folder_the_user_chose():
    """Results are the one artifact allowed outside DATA — they belong to the user.

    The rule they must still obey is the one above: never inside ROOT, which is
    replaced wholesale on update. With no workspace configured the fallback keeps
    them in DATA, which is what installs predating the setting continue to do.
    """
    from hireshire import workspace

    assert paths.results_root() == paths.RESULTS_DIR
    assert paths.DATA in paths.RESULTS_DIR.parents

    with pytest.raises(workspace.WorkspaceError):
        workspace.init_workspace(paths.ROOT / "hireshire_job_search")


def test_requirements_exclude_the_dropped_heavy_dependencies():
    reqs = (ROOT / "requirements-core.txt").read_text(encoding="utf-8").lower()
    for dropped in ("browser-use", "fastapi", "uvicorn", "langgraph",
                    "langchain", "pdfminer", "sse-starlette"):
        assert dropped not in reqs
    # The reranker must not add a package — CrossEncoder ships inside this one.
    assert "sentence-transformers" in reqs
