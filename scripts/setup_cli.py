"""Every action `/hireshire:setup` performs, as fixed-shape commands.

Setup used to run its Python by writing a heredoc to a temp file and passing that
file to the launcher. It worked, but it made the plugin feel like it was asking
permission for everything: Claude Code matches permission rules against the exact
Bash command string, and the heredoc body is part of that string, so no two calls
ever matched, no allowlist rule could cover them, and "don't ask again" never
stuck. A first-time user approved a dozen dialogs before seeing a single job.

Fixed argv is the whole point of this file. Each subcommand is a stable command
line, which `scripts/approve.py` can recognise and auto-approve, so the questions
setup asks are the *only* thing the user has to answer.

    python scripts/setup_cli.py install-config
    python scripts/setup_cli.py init-workspace "/home/me/job-search"
    python scripts/setup_cli.py install-resume "/home/me/cv.pdf" "/home/me/job-search"
    python scripts/setup_cli.py set matcher --json '{"threshold": 75}'
    python scripts/setup_cli.py write-profile --text "Senior account manager ..."
    python scripts/setup_cli.py warm-models

Payloads travel on argv rather than stdin or a temp file, deliberately: heredocs
and redirection are exactly what the approval guard has to refuse, so neither can
be the transport. A `targets` list of several dozen job titles is a few kilobytes,
well inside any argv limit.

Runs inside the plugin venv, via `scripts/run_engine.py`, so it may import the
engine — unlike `bootstrap.py` and `approve.py`, which run before the venv exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python scripts/setup_cli.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire import config_writer, paths, workspace  # noqa: E402
from hireshire.matcher.resume import extract_resume_text  # noqa: E402

PROFILE_FILENAME = "profile.md"

# Names the guard in scripts/approve.py whitelists. Keep the two in step: a
# subcommand missing there still works, it just prompts.
SUBCOMMANDS = (
    "install-config",
    "init-workspace",
    "find-resumes",
    "install-resume",
    "resume-text",
    "get",
    "field-docs",
    "set",
    "write-profile",
    "warm-models",
)


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def cmd_install_config(args: argparse.Namespace) -> int:
    installed = config_writer.install_user_config()
    if installed:
        for p in installed:
            print(f"installed {p}")
    else:
        print("config already present in the data directory; left untouched")
    return 0


def cmd_init_workspace(args: argparse.Namespace) -> int:
    ws = workspace.init_workspace(args.path)
    print(ws)
    return 0


def cmd_find_resumes(args: argparse.Namespace) -> int:
    found = workspace.find_resumes(args.workspace)
    for p in found:
        print(p)
    if not found:
        print("no PDFs in resume/original/", file=sys.stderr)
    return 0


def cmd_install_resume(args: argparse.Namespace) -> int:
    # Validates before copying, so a scanned PDF fails while the user can still
    # pick another file. The path printed is the copy's — that is what belongs in
    # matcher.resume_path and applier.resume_path, not the path they typed.
    dest = workspace.install_resume(args.src, args.workspace)
    print(dest)
    return 0


def cmd_resume_text(args: argparse.Namespace) -> int:
    text = extract_resume_text(args.path)
    print(text[: args.chars] if args.chars else text)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    _print_json(config_writer.read_config(args.phase))
    return 0


def cmd_field_docs(args: argparse.Namespace) -> int:
    _print_json(config_writer.field_docs(args.phase))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    try:
        values = json.loads(args.json)
    except json.JSONDecodeError as exc:
        raise config_writer.ConfigError(f"--json is not valid JSON: {exc}") from exc
    if not isinstance(values, dict):
        raise config_writer.ConfigError(
            f"--json must be an object of flat keys, got {type(values).__name__}"
        )
    _print_json(config_writer.write_config(args.phase, values))
    return 0


def cmd_write_profile(args: argparse.Namespace) -> int:
    """Write the reranker's query document into DATA and print where it landed.

    The path is resolved here rather than by the skill on purpose. A setup run in
    the Claude desktop app once wrote this file to a directory the engine never
    reads, which silently disabled the reranker for every later sweep — the only
    symptom was one line in a log. Printing the absolute path also replaces the
    "`ls` it to check" step the skill used to need.
    """
    text = args.text.strip()
    if not text:
        raise ValueError("--text is empty; the profile is the reranker's only query")
    paths.ensure_data_dirs()
    dest = paths.DATA / PROFILE_FILENAME
    dest.write_text(text + "\n", encoding="utf-8")
    print(dest)
    print(f"{len(text.split())} words; store the bare filename {PROFILE_FILENAME!r} "
          f"in matcher.search_profile_path")
    return 0


def cmd_warm_models(args: argparse.Namespace) -> int:
    """Pull the funnel's models now, while the user still expects to be waiting.

    Both are imported lazily by the engine, so skipping this moves the download into
    the middle of the first sweep. The cross-encoder is the one that matters most:
    it is not loaded until a batch reaches the rerank stage, which is the worst
    possible moment to discover it is missing.

    The names come from the config rather than being written out here. They were
    hardcoded, and when the two-stage cascade collapsed to a single model this
    warmed one model that no longer exists in the pipeline and missed nothing —
    a failure whose only symptom would have been a stall on the first real run.
    """
    from sentence_transformers import CrossEncoder, SentenceTransformer
    from hireshire.matcher.config import load_matcher_config

    funnel = load_matcher_config().funnel

    SentenceTransformer(funnel.encoder.model).encode(["warmup"])
    print(f"{funnel.encoder.model} ready", flush=True)
    CrossEncoder(funnel.rerank.model).predict([("warmup", "warmup")])
    print(f"{funnel.rerank.model} ready", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Actions performed by /hireshire:setup")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install-config", help="Copy the shipped default YAMLs into DATA")

    p = sub.add_parser("init-workspace", help="Create the user's job-search folder")
    p.add_argument("path")

    p = sub.add_parser("find-resumes", help="PDFs already in <workspace>/resume/original/")
    p.add_argument("workspace")

    p = sub.add_parser("install-resume", help="Validate and copy a resume into the workspace")
    p.add_argument("src")
    p.add_argument("workspace")

    p = sub.add_parser("resume-text", help="Extract a resume's text, for drafting targets")
    p.add_argument("path")
    p.add_argument("--chars", type=int, default=0, help="Truncate to this many characters")

    p = sub.add_parser("get", help="Print a phase's editable settings")
    p.add_argument("phase")

    p = sub.add_parser("field-docs", help="Print a phase's editable keys and what they mean")
    p.add_argument("phase")

    p = sub.add_parser("set", help="Write settings to a phase's YAML")
    p.add_argument("phase")
    p.add_argument("--json", required=True, help='Flat keys, e.g. \'{"threshold": 75}\'')

    p = sub.add_parser("write-profile", help="Write the search profile into DATA")
    p.add_argument("--text", required=True)

    sub.add_parser("warm-models", help="Download the funnel's bi-encoder and cross-encoder")
    return parser


HANDLERS = {
    "install-config": cmd_install_config,
    "init-workspace": cmd_init_workspace,
    "find-resumes": cmd_find_resumes,
    "install-resume": cmd_install_resume,
    "resume-text": cmd_resume_text,
    "get": cmd_get,
    "field-docs": cmd_field_docs,
    "set": cmd_set,
    "write-profile": cmd_write_profile,
    "warm-models": cmd_warm_models,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except (config_writer.ConfigError, workspace.WorkspaceError,
            FileNotFoundError, ValueError, OSError) as exc:
        # These are all messages written for the user — a rejected config key, a
        # scanned PDF, a workspace inside the install dir. The skill relays them,
        # so they must reach it intact rather than as a traceback.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
