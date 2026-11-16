"""Run every discovery tool in turn: slugs (Greenhouse, Lever, Ashby, Gem), Eightfold, Oracle.

A maintenance tool, never run by the engine. Each tool keeps its own rules and
output; this only forwards the flags they share and reports the worst exit code.
One tool's failed index pages do not stop the next: they are already reported,
and its cache means a rerun fetches only what failed.

    python scripts/discover_all.py                      # dry run: report only
    python scripts/discover_all.py --crawls 1 --write   # merge into config/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_eightfold  # noqa: E402
import discover_oracle  # noqa: E402
import discover_slugs  # noqa: E402

TOOLS = (discover_slugs, discover_eightfold, discover_oracle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--crawls", type=int, default=3)
    parser.add_argument("--cdx-attempts", type=int, default=6)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    forwarded = ["--crawls", str(args.crawls), "--cdx-attempts", str(args.cdx_attempts)]
    forwarded += ["--write"] if args.write else []

    worst = 0
    for tool in TOOLS:
        print(f"\n=== {tool.__name__} ===")
        worst = max(worst, tool.main(forwarded))
    return worst


if __name__ == "__main__":
    sys.exit(main())
