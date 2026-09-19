"""
Query and record job applications in the shared SQLite datastore's `applied`
table. Used by the `/apply` Claude Code skill so it shares one source of truth
with the engine's apply worker (both read/write the same table).

    python scripts/applied_cli.py list                      # print applied job_ids, one per line
    python scripts/applied_cli.py pending                   # shortlisted, not yet applied to (JSON)
    python scripts/applied_cli.py record --job-id j1 \
        --board-token acme --title "Backend Engineer" \
        --url https://example.com/jobs/j1 --status submitted
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as `python scripts/applied_cli.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire.storage.db import DEFAULT_DB_PATH, get_db


def _default_backlog_hours() -> int:
    """The user's `backlog_hours`, so the skill and the sweep agree on what is pending."""
    from hireshire.applier.config import ApplierSettings, load_applier_config

    try:
        return load_applier_config().settings.backlog_hours
    except Exception:  # noqa: BLE001 - no config yet is not a reason to fail a read
        return ApplierSettings().backlog_hours


def main() -> None:
    parser = argparse.ArgumentParser(description="Query/record applications in the HireShire DB")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to the SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Print already-applied job_ids, one per line")

    pend = sub.add_parser(
        "pending",
        help="Print shortlisted jobs with no application record, best first, as JSON",
    )
    pend.add_argument("--hours", type=int, default=None,
                      help="How far back to look (default: backlog_hours from applier.yaml)")

    rec = sub.add_parser("record", help="Insert/replace one application record")
    rec.add_argument("--job-id", required=True)
    rec.add_argument("--board-token", default="")
    rec.add_argument("--title", default="")
    rec.add_argument("--url", default="")
    rec.add_argument("--status", required=True,
                     help='"submitted" | "error" | "excluded"')
    rec.add_argument("--screenshot", default=None)
    rec.add_argument("--error", default=None)

    args = parser.parse_args()
    db = get_db(args.db)

    if args.command == "list":
        for job_id in sorted(db.applied_ids()):
            print(job_id)
        return

    if args.command == "pending":
        hours = args.hours if args.hours is not None else _default_backlog_hours()
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        print(json.dumps(db.load_pending_applications(since.isoformat()), indent=2))
        return

    if args.command == "record":
        db.record_applied(
            args.job_id, args.board_token, args.title, args.url,
            datetime.now(timezone.utc).isoformat(), args.status,
            args.screenshot, args.error,
        )
        print(f"recorded {args.job_id} (status={args.status})")
        return


if __name__ == "__main__":
    main()
