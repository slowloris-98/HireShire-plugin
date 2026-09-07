"""
Work out what `funnel.rerank.min_score` and `funnel.encoder.threshold` should be for
*this* user, from runs they have already paid for.

Both settings are cutoffs, and neither is portable. `min_score` is a raw cross-encoder
logit against one person's search profile: it is not a probability, not a percentage,
and it means nothing for a different resume, a different `targets` list, or a different
model. Shipping one number and hoping is how a user ends up with an empty shortlist and
no error to explain it.

The data to do better is already in the database. Every scored row carries the
cross-encoder logit that let it through and the LLM score that came back, so for any
candidate cutoff you can ask the only two questions that matter:

    how many jobs would have reached the judge?      (what it costs)
    how many jobs the judge liked would be lost?     (what it costs you)

The right cutoff is the highest one that still keeps ~95% of the jobs the LLM rated at
or above your shortlist threshold. Read that off the table and write it with:

    setup_cli.py set funnel --json '{"rerank_min_score": 1.4}'

READ-ONLY. It opens the database, prints, and exits — it never writes config, because
a number chosen from a sample of one run deserves a human look first.

    python scripts/calibrate_cutoffs.py
    python scripts/calibrate_cutoffs.py --run-id 20260115-093000
    python scripts/calibrate_cutoffs.py --recall 0.90
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/calibrate_cutoffs.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from hireshire.matcher.config import load_matcher_config
from hireshire.storage.db import get_db

console = Console()

# Enough scored rows for the percentages to mean anything. Below this the curve is
# noise, and a cutoff read off it is worse than the shipped default.
MIN_SAMPLE = 30


def _curve(pairs: list[tuple[float, int]], threshold: int, label: str) -> Table:
    """One row per candidate cutoff: what it passes, and what it costs in recall."""
    good = [s for s, rel in pairs if rel >= threshold]
    table = Table(title=f"{label} (n={len(pairs)}, {len(good)} scored >= {threshold})")
    table.add_column("cutoff", justify="right")
    table.add_column("would pass", justify="right")
    table.add_column("% of jobs", justify="right")
    table.add_column("good jobs kept", justify="right")
    table.add_column("recall", justify="right")

    values = sorted(s for s, _ in pairs)
    # Deciles of the observed distribution rather than a fixed grid: the useful range
    # of a logit differs per user, and a hardcoded -5..5 would be mostly empty rows.
    marks = sorted({values[min(int(len(values) * f), len(values) - 1)] for f in
                    (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)})

    for cut in marks:
        passed = [s for s, _ in pairs if s >= cut]
        kept = [s for s in good if s >= cut]
        recall = len(kept) / len(good) if good else 0.0
        table.add_row(
            f"{cut:.3f}",
            str(len(passed)),
            f"{100 * len(passed) / len(pairs):.0f}%",
            f"{len(kept)}/{len(good)}",
            f"[{'green' if recall >= 0.95 else 'yellow' if recall >= 0.85 else 'red'}]"
            f"{100 * recall:.0f}%[/]",
        )
    return table


def _recommend(pairs: list[tuple[float, int]], threshold: int, target: float) -> float | None:
    """The highest cutoff still keeping `target` of the jobs the LLM liked."""
    good = sorted(s for s, rel in pairs if rel >= threshold)
    if not good:
        return None
    # Keep `target` of them, so drop at most the bottom (1 - target).
    idx = int(len(good) * (1.0 - target))
    return good[min(idx, len(good) - 1)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-id", help="Calibrate on one run instead of all history")
    ap.add_argument(
        "--recall", type=float, default=0.95,
        help="Fraction of LLM-approved jobs the cutoff must keep (default 0.95)",
    )
    args = ap.parse_args()

    config = load_matcher_config()
    threshold = config.settings.threshold
    db = get_db(config.settings.db_path)

    rows = db.calibration_rows(args.run_id)
    if len(rows) < MIN_SAMPLE:
        console.print(
            f"[yellow]Only {len(rows)} scored jobs on record — too few to calibrate "
            f"against (need {MIN_SAMPLE}).[/yellow]\n"
            "Run a few more sweeps at the shipped defaults first. Every scored job "
            "adds a data point, so this gets more reliable the longer it is left."
        )
        return 1

    rerank_pairs = [(r["rerank_score"], r["relevance_score"]) for r in rows]
    console.print(_curve(rerank_pairs, threshold, "funnel.rerank.min_score"))
    pick = _recommend(rerank_pairs, threshold, args.recall)
    if pick is not None:
        console.print(
            f"\n[bold]Suggested min_score: {pick:.3f}[/bold] — the highest cutoff that "
            f"still keeps {100 * args.recall:.0f}% of the jobs your LLM scored "
            f">= {threshold}. Currently set to {config.funnel.rerank.min_score}."
        )
        console.print(
            f"  [dim]sh scripts/hireshire.sh scripts/setup_cli.py set funnel "
            f"--json '{{\"rerank_min_score\": {pick:.3f}}}'[/dim]"
        )

    encoder_pairs = [
        (r["encoder_score"], r["relevance_score"])
        for r in rows if r["encoder_score"] is not None
    ]
    if encoder_pairs:
        console.print()
        console.print(_curve(encoder_pairs, threshold, "funnel.encoder.threshold"))
        console.print(
            "\n[dim]The encoder gate is title-only and free to run, so tightening it "
            "saves CPU, not LLM calls. Raise it only if sweeps are too slow — and "
            "stay well below the lowest score any good job scored.[/dim]"
        )

    console.print(
        "\n[dim]These numbers describe your resume and your target list. They do not "
        "transfer to another user, and they are void if funnel.rerank.model "
        "changes.[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
