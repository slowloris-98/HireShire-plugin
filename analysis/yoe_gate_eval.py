"""Does the regex years-of-experience gate earn its place, at THIS tolerance?

Offline evaluation only. **This changes no pipeline behaviour** — it reads the DB a
real sweep already wrote, plus the label cache `analysis/extraction_spike.py` left
behind, and reports what `hireshire/funnel/experience.py` would have done.

## Why this exists separately from the spike

`analysis/results/extraction_prefilter.md` recorded a PASS on its safety floor:
nothing the filter killed had been shortlisted, and nothing scored >= 53. **That
result does not transfer to the shipped gate.** It was measured at
`YOE_TOLERANCE = 2` — two whole years of slack — and the shipped default is 0.5.
Four times tighter kills strictly more, so the floor has to be re-measured against
whatever tolerance is actually configured before anyone trusts it.

## What it can and cannot conclude

The corpus is one real sweep. 213 descriptions carry an LLM score and only 3 were
shortlisted, which cannot support precision/recall — so this reports the same two
things the spike did:

  1. parser agreement with the cached labels, split by ERROR DIRECTION, because
     reading higher than the truth is the only direction that kills a job wrongly;
  2. a safety floor: what the gate kills among jobs that cleared the cross-encoder
     cutoff, and the best judge score among the casualties.

Note the labels are themselves an LLM's output, not ground truth. Where the two
disagree the label is often the wrong one — it returned null on postings plainly
stating a minimum, and on anything phrased as "preferred", which the shipped parser
deliberately treats as a requirement. Read the printed disagreements before
believing either number.

Usage:
    python analysis/yoe_gate_eval.py
    python analysis/yoe_gate_eval.py --tolerance 2 --candidate-years 4
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire.funnel.experience import meets, parse_requirement  # noqa: E402
from hireshire.models.job import Job  # noqa: E402

#: Cross-encoder cutoff the pipeline runs at. The gate only matters for jobs that
#: survive it — everything below is already rejected a stage earlier.
MIN_SCORE = 3.0

#: Nothing the judge scored at or above this may be killed. Set below the shortlist
#: threshold on purpose: a filter that only just spares the winners is not safe, it
#: is lucky.
SAFETY_FLOOR = 53


def _plain_text(raw: str | None) -> str:
    """What the pipeline sees.

    `Job.content_text` is stripped to plain text by a `mode="before"` validator
    (`Job.strip_html`), but the `jobs` table holds the raw HTML for some rows. Running
    the parser over markup would measure a different input than production uses, so
    reuse the model's own validator rather than reimplementing it.
    """
    return Job.strip_html(raw) or ""


def _load(db_path: Path, labels: dict) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT m.job_id, m.title, m.board_token, m.rerank_score, m.relevance_score,"
        "       m.shortlisted, j.content_text "
        "FROM matches m JOIN jobs j ON j.job_id = m.job_id "
        "WHERE j.content_text IS NOT NULL AND LENGTH(j.content_text) > 200"
    ).fetchall()
    conn.close()

    seen, out = set(), []
    for r in rows:
        if r["job_id"] in seen or r["job_id"] not in labels:
            continue
        seen.add(r["job_id"])
        row = dict(r)
        row["text"] = f"{row['title']}\n\n{_plain_text(row['content_text'])}"
        out.append(row)
    return out


def _report(rows: list[dict], labels: dict, candidate: float, tolerance: float) -> int:
    print(f"\n{len(rows)} labelled descriptions, candidate_years={candidate}, "
          f"tolerance_years={tolerance}")

    # --- 1. parser agreement -----------------------------------------------------
    exact = higher = lower = invented = missed = 0
    disagreements = []
    for row in rows:
        gold = labels[row["job_id"]]
        req = parse_requirement(row["text"])
        got = req.min_years if req else None
        if gold is None and got is None:
            exact += 1
        elif gold is None:
            invented += 1
            disagreements.append((row, gold, got))
        elif got is None:
            missed += 1
        elif got == gold:
            exact += 1
        elif got > gold:
            higher += 1
            disagreements.append((row, gold, got))
        else:
            lower += 1

    n = max(len(rows), 1)
    print("\n=== 1. Parser vs the cached LLM labels ===")
    print(f"  exact agreement            : {exact}/{len(rows)} ({100 * exact / n:.0f}%)")
    print(f"  read HIGHER than the label : {higher}    <- the only direction that kills wrongly")
    print(f"  found one where label said none : {invented}  <- inspect these; the label is often wrong")
    print(f"  read lower than the label  : {lower}")
    print(f"  found none where label had one  : {missed}")

    if disagreements:
        print("\n  every disagreement in the risky direction:")
        for row, gold, got in sorted(disagreements, key=lambda d: -(d[2] or 0))[:20]:
            print(f"    label={str(gold):>4} regex={got:>4}  {row['title'][:58]}")

    # --- 2. safety floor ---------------------------------------------------------
    above = [r for r in rows if (r["rerank_score"] or -99) >= MIN_SCORE]
    kills = []
    for row in above:
        req = parse_requirement(row["text"])
        if not meets(req, candidate, tolerance):
            kills.append((row, req.min_years))

    print(f"\n=== 2. Effect above the {MIN_SCORE} cutoff ===")
    print(f"  jobs above the cutoff : {len(above)}")
    print(f"  the gate would kill   : {len(kills)}")
    for row, need in sorted(kills, key=lambda k: -(k[0]["rerank_score"] or 0)):
        judge = row["relevance_score"]
        print(f"    cross={row['rerank_score']:5.2f} judge={judge if judge is not None else '--':>4}"
              f"  {row['board_token'][:18]:18s} {row['title'][:36]:36s}"
              f" | needs {need:g}y vs {candidate:g}y")

    violations = [
        (row, need) for row, need in kills
        if row["shortlisted"] or (row["relevance_score"] or 0) >= SAFETY_FLOOR
    ]
    labelled = [r for r in rows if r["relevance_score"] is not None]
    scored_kills = [r for r, _ in kills if r["relevance_score"] is not None]
    best = max((r["relevance_score"] for r in scored_kills), default=None)

    print(f"\n=== 3. Safety floor (must kill nothing >= {SAFETY_FLOOR}) ===")
    print(f"  rows carrying a judge score : {len(labelled)} "
          f"(shortlisted: {sum(1 for r in labelled if r['shortlisted'])})")
    print("  NOTE: too few positives for precision/recall; this is a floor, not a metric.")
    print(f"  best judge score among the killed : "
          f"{best if best is not None else 'n/a - none of them were judged'}")
    if violations:
        print(f"  FAIL - killed {len(violations)} job(s) the judge rated highly:")
        for row, need in violations:
            print(f"    judge={row['relevance_score']} {row['title'][:45]} | needs {need:g}y")
        return 1
    print("  PASS - nothing the judge rated highly was killed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="hireshire.db (default: live install)")
    ap.add_argument("--cache", default=str(Path(__file__).parent / "cache" / "extraction.json"),
                    help="label cache written by extraction_spike.py")
    ap.add_argument("--candidate-years", type=float, default=None,
                    help="default: total_yoe from the cached resume extraction")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="default: the shipped ExperienceConfig.tolerance_years")
    args = ap.parse_args()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"no label cache at {cache_path} - run extraction_spike.py first",
              file=sys.stderr)
        return 1
    blob = json.loads(cache_path.read_text())
    labels = {k: v.get("min_yoe") for k, v in blob.get("jobs", {}).items()}

    candidate = args.candidate_years
    if candidate is None:
        candidate = (blob.get("resume") or {}).get("total_yoe")
    if candidate is None:
        print("no candidate years — pass --candidate-years", file=sys.stderr)
        return 1

    tolerance = args.tolerance
    if tolerance is None:
        from hireshire.funnel.config import ExperienceConfig
        tolerance = ExperienceConfig().tolerance_years

    from hireshire import paths
    db_path = Path(args.db) if args.db else paths.resolve_data("hireshire.db")
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1

    rows = _load(db_path, labels)
    if not rows:
        print("no labelled rows found in the database", file=sys.stderr)
        return 1
    print(f"reading {db_path}")
    return _report(rows, labels, float(candidate), float(tolerance))


if __name__ == "__main__":
    raise SystemExit(main())
