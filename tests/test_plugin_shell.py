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


def test_monitors_are_declared_under_experimental():
    """Top-level `monitors` still loads but warns, and a future release will
    require the nested form."""
    m = _json(".claude-plugin/plugin.json")
    assert "monitors" not in m
    assert m["experimental"]["monitors"].startswith("./")


def test_marketplace_points_at_this_repo():
    mk = _json(".claude-plugin/marketplace.json")
    entry = next(p for p in mk["plugins"] if p["name"] == "hireshire")
    # Self-hosting: the plugin IS the repo root.
    assert entry["source"] == "./"


def test_monitor_config_is_a_bare_array_with_a_named_entry():
    mons = _json("monitors/monitors.json")
    assert isinstance(mons, list), "monitors.json is an array, not an object"
    entry = mons[0]
    # The name is what stops a second skill invocation spawning a duplicate sweep.
    assert entry["name"] == "orchestration"
    assert entry["when"] == "on-skill-invoke:start-orchestration"


def test_monitor_command_cannot_reference_user_config():
    """Claude Code rejects the whole monitor rather than substituting, so the
    interval has to be read from the user's config by the wrapper instead."""
    raw = (ROOT / "monitors" / "monitors.json").read_text(encoding="utf-8")
    assert "${user_config" not in raw


def test_session_start_hook_is_shaped_correctly():
    hooks = _json("hooks/hooks.json")["hooks"]["SessionStart"]
    cmd = hooks[0]["hooks"][0]
    assert cmd["type"] == "command"
    assert "--bootstrap" in cmd["command"]
    # The install can pull ~2.5 GB; the default 600s timeout is not enough.
    assert cmd.get("timeout", 0) >= 900


def test_every_launch_path_goes_through_the_one_launcher():
    """Interpreter discovery is solved in exactly one place. Two platform traps
    make that worth enforcing: macOS has no bare `python` (Apple removed
    /usr/bin/python in 12.3), and Windows ships a Microsoft Store stub named
    python3.exe that exists on PATH, prints an ad and exits non-zero — so
    `command -v python3` picks the broken one while the real `python` sits next
    to it."""
    hook = _json("hooks/hooks.json")["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    monitor = _json("monitors/monitors.json")[0]["command"]

    for cmd in (hook, monitor):
        assert "hireshire.sh" in cmd
        assert "python" not in cmd, "interpreter choice belongs in the launcher"


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
