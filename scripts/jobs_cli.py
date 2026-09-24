"""Outcomes the user records by hand, as fixed-shape commands.

The applier reaches a verdict for most jobs, but three of its verdicts hand the job
back to the user: `error` (a session stopped short of submitting), `excluded` (the
employer's portal needs an account login) and `expired` (the backlog's window closed
before any session got through). All three land under the overview page's **Needs
Attention** section, and all three mean the same thing — do this one yourself.

Until this file existed there was no way to say it had been done. The row sat under
Needs Attention for good, and a *shortlisted* job the user applied to by hand was
applied to a second time by the next sweep, because nothing had retired it.

    python scripts/jobs_cli.py list
    python scripts/jobs_cli.py applied  --job-id 4f9c21a8
    python scripts/jobs_cli.py declined --job-id 4f9c21a8 --job-id 7b02ee31

Two outcomes, deliberately asymmetric, because an application is a row in `applied`
and a decision not to apply is not:

* `applied` counts toward the Jobs applied tile and moves the job to Jobs Applied.
* `declined` writes no application. It deletes any failed attempt and un-shortlists
  the job with `DECLINED_BY_USER`, which files it under Jobs Filtered with a reason
  the user can read. See that constant for why it cannot be an `applied` status.

Both are one-way and both are idempotent.

Fixed argv is the whole point of the file, exactly as in `setup_cli.py`: Claude Code
matches permission rules against the exact Bash command string, so a stable argv is
what lets `scripts/approve.py` recognise these and stop asking. `--job-id` repeats
rather than taking a list, so marking three jobs is one command and one approval.

**Every write rebuilds the two dashboard pages before returning.** They are static
files that only the engine rewrites, and between sweeps nothing rewrites them at all
— so without this the user would paste the command, the database would change, and
the page they are looking at would not.

Runs inside the plugin venv, via `scripts/run_engine.py`, so it may import the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/jobs_cli.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire import paths, reporting  # noqa: E402
from hireshire.storage.db import get_db  # noqa: E402

# Names the guard in scripts/approve.py whitelists. Keep the two in step: a
# subcommand missing there still works, it just prompts.
SUBCOMMANDS = (
    "list",
    "applied",
    "declined",
)

#: The whole of time. `load_pending_applications` is the backlog loader and takes the
#: window's opening as a parameter; passing the epoch turns it into "every shortlisted
#: job never applied to", which is what a user triaging by hand wants to see. A job
#: from three sweeps ago is the likeliest one they dealt with themselves, and it is
#: also the one no per-run page can show them.
_EPOCH = "1970-01-01T00:00:00+00:00"


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def _rebuild_reports() -> dict[str, str]:
    """Rewrite both dashboard pages from the database. Never raises.

    `last_run.json` already records the three things `reporting.refresh` needs, so
    this reads them rather than guessing at a results directory — the same rule the
    skills follow with `--paths`.

    `final=True` only bypasses the refresh throttle. Whether the pages arm their meta
    refresh still comes from the pipeline's `runs` row, so a live sweep's page keeps
    reloading and a finished one does not. A sweep running concurrently is harmless:
    its next tick reads the same database and renders the same change.
    """
    try:
        pointer = json.loads(paths.LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    run_id = pointer.get("run_id")
    stamp = pointer.get("stamp")
    results_dir = pointer.get("results_dir")
    if not (run_id and stamp and results_dir):
        return {}
    reporting.refresh(run_id, Path(results_dir), stamp, final=True)
    return {k: str(v) for k, v in reporting.report_paths(Path(results_dir), stamp).items()}


def _row(record: dict, *, attention: bool) -> dict:
    """One job, in the shape the skill reads back to the user."""
    out = {
        "job_id": record.get("job_id"),
        "title": record.get("title") or "",
        "company": record.get("board_token") or record.get("company") or "",
        "url": record.get("absolute_url") or record.get("job_url") or "",
        "llm_score": record.get("relevance_score"),
    }
    if attention:
        out["status"] = record.get("applied_status") or ""
        out["reason"] = " ".join((record.get("applied_error") or "").split())
        out["at"] = record.get("applied_at") or ""
    return out


def cmd_list(args: argparse.Namespace) -> int:
    """Every job the user could still deal with by hand, at lifetime scope.

    Two existing loaders, no new SQL. Neither is scoped to a run on purpose: run scope
    would need a `matches` row in that sweep, and a job the backlog carried across
    sweeps has none — which is precisely the job that gets stuck.
    """
    db = get_db()
    attempts = db.load_applied_matches(None)
    _print_json({
        "attention": [
            _row(r, attention=True) for r in attempts
            if r.get("applied_status") != "submitted"
        ],
        "shortlisted": [
            _row(r, attention=False)
            for r in db.load_pending_applications(_EPOCH)
        ],
    })
    return 0


def _write(args: argparse.Namespace, action: str) -> int:
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for job_id in args.job_id:
        if action == "applied":
            outcome = db.mark_applied_by_hand(job_id, now)
        else:
            changed = db.decline_job(job_id)
            outcome = (
                "declined" if changed["deleted"] or changed["unshortlisted"]
                else "nothing_to_change"
            )
        results.append({"job_id": job_id, "action": action, "result": outcome})

    # Only pay for a rebuild when something actually moved. A run of no-ops should not
    # rewrite two pages.
    pages = {}
    if any(r["result"] not in ("unknown", "nothing_to_change") for r in results):
        pages = _rebuild_reports()
    _print_json({"results": results, "pages": pages})
    return 0


def cmd_applied(args: argparse.Namespace) -> int:
    return _write(args, "applied")


def cmd_declined(args: argparse.Namespace) -> int:
    return _write(args, "declined")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record an outcome the user reached by hand"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Jobs awaiting the user: needs-attention and shortlisted")

    for name, helptext in (
        ("applied", "Record that the user applied to these jobs themselves"),
        ("declined", "Record that the user is not pursuing these jobs"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--job-id", action="append", required=True,
                       help="Repeat for several jobs; ids come from `list`")
    return parser


HANDLERS = {
    "list": cmd_list,
    "applied": cmd_applied,
    "declined": cmd_declined,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        # A missing database, an unreadable pointer file. Written for the user, and the
        # skill relays them, so they must arrive intact rather than as a traceback.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
