"""Does a structured-extraction pre-filter earn a place in the funnel?

Offline evaluation only. **This changes no pipeline behaviour** — it reads the DB a
real sweep already wrote and reports what a deterministic role/YoE filter *would*
have done. Nothing here is imported by the engine.

## Why this is not the usual "cache the JSONs and save money" argument

That pitch assumes per-token API billing and repeated re-judging. Neither holds here:
`provider: claude_code` spends subscription quota, and `seen_jobs` retires a job the
moment it has an outcome, so nothing is judged twice. Caching buys almost nothing.

**The case is precision.** `rerank.min_score` is a raw logit the codebase itself calls
personal, uncomparable between users, and void when the model changes. Against junk
that is semantically tech-adjacent but categorically wrong — an "IT Support Technical
Instructor" outscoring real SWE roles — no threshold separates it from genuine matches
without also cutting them. A role-family equality test does.

## Why the filter is relative, never absolute

The plugin is not a SWE tool. Outside tech, titles are branded and generic, which is
why the bi-encoder threshold already carries this warning. So there is no hardcoded
"is this engineering" test anywhere below: a `role_family` is extracted for the
*resume* and for each JD, and the two are compared. A marketing candidate must MATCH
the "Influencer Social Media Strategist" posting that a SWE candidate rightly rejects.

That comparison only works against a closed vocabulary. Free text yields "Software
Engineering" vs "Backend" vs "Engineering" and the equality test becomes noise, so
both extractions must choose from ROLE_FAMILIES below.

## What it can and cannot conclude

The corpus is one real sweep: ~800 descriptions, but only ~50 carry an LLM score and
just 3 are shortlisted. Three positives cannot support precision/recall, so this
script deliberately does not report them. It reports extraction reliability (which the
corpus does support) and a safety floor: the filter must kill none of the shortlisted
jobs and nothing scored >= SAFETY_FLOOR.

Usage:
    python analysis/extraction_spike.py --limit 80
    python analysis/extraction_spike.py --report-only    # reuse the cache, no calls
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hireshire.matcher.resume import extract_resume_text  # noqa: E402

#: The closed vocabulary. Sized to the boards actually swept, which one real run shows
#: are mostly not tech — construction, veterinary and staffing employers outnumbered
#: software ones. A family missing from here becomes "Other", which never matches, so
#: gaps here read as false drops: widen it before trusting a kill.
ROLE_FAMILIES = [
    "Engineering", "Data/ML", "Design", "Product", "Marketing", "Sales",
    "Finance", "Legal", "Healthcare", "Education", "Operations",
    "Skilled Trades", "Construction", "Hospitality", "HR/Recruiting", "Other",
]

#: Cross-encoder cutoff the pipeline now runs at. The filter is only interesting for
#: jobs that survive it — everything below is already rejected a stage earlier.
MIN_SCORE = 3.0

#: Nothing the judge scored at or above this may be killed. Set below the shortlist
#: threshold on purpose: a filter that only just spares the 3 winners is not safe, it
#: is lucky.
SAFETY_FLOOR = 53

#: Years the JD may demand beyond the resume before it counts as a mismatch. Postings
#: asking 5-8 do hire 3-year candidates, so a strict comparison would be wrong more
#: often than the thing it is filtering.
YOE_TOLERANCE = 2

_JD_SCHEMA = {
    "type": "object",
    "properties": {
        "role_family": {"type": "string", "enum": ROLE_FAMILIES},
        "min_yoe": {
            "type": ["integer", "null"],
            "description": "Minimum years of experience the posting states. "
                           "null when it states none — do NOT guess or infer.",
        },
    },
    "required": ["role_family", "min_yoe"],
}

_RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "role_families": {
            "type": "array",
            "items": {"type": "string", "enum": ROLE_FAMILIES},
            "description": "Every family this candidate is credibly employable in.",
        },
        "total_yoe": {"type": "integer"},
    },
    "required": ["role_families", "total_yoe"],
}

_JD_SYSTEM = (
    "Extract two facts from a job posting. Choose role_family from the given enum "
    "only. For min_yoe, report ONLY an explicitly stated minimum number of years; if "
    "the posting does not state one, return null. Never infer years from seniority "
    "words like 'senior' or 'junior'."
)

_RESUME_SYSTEM = (
    "Extract the candidate's employable role families (from the enum) and their total "
    "years of professional experience. Be generous with role_families: list every "
    "family they could credibly be hired into, not just their current title."
)


async def _extract(prompt: str, system: str, schema: dict, model: str, sem) -> dict | None:
    """One `claude -p` call returning JSON, or None if it failed.

    Mirrors `hireshire/matcher/scorer.py`'s claude_code backend deliberately, including
    `--no-session-persistence`: without it every extraction leaves a transcript in
    ~/.claude/projects/ AND bills a background title-generation call against the same
    allowance, which would make this spike cost roughly double what it appears to.
    """
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--system-prompt", system,
            "--model", model,
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")},
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=180
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(out.decode(errors="replace"))
            # The CLI wraps the answer; the schema result is in `result`.
            inner = payload.get("result", payload)
            return json.loads(inner) if isinstance(inner, str) else inner
        except (ValueError, AttributeError):
            return None


def _load_rows(db_path: Path) -> list[dict]:
    """Judged/considered rows joined to their description text."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT m.job_id, m.title, m.board_token, m.relevance_score, m.rerank_score,"
        "       m.shortlisted, m.skip_reason, j.content_text "
        "FROM matches m JOIN jobs j ON j.job_id = m.job_id "
        "WHERE j.content_text IS NOT NULL AND LENGTH(j.content_text) > 200"
    ).fetchall()
    conn.close()
    # One row per job_id; a job can appear under several run_ids.
    seen, out = set(), []
    for r in rows:
        if r["job_id"] in seen:
            continue
        seen.add(r["job_id"])
        out.append(dict(r))
    return out


def _verdict(jd: dict, resume: dict) -> tuple[bool, str]:
    """(kill, why). Absent data always keeps the job — see the null policy above."""
    fam = jd.get("role_family")
    if fam and fam not in resume["role_families"]:
        return True, f"role_family {fam} not in {resume['role_families']}"
    min_yoe = jd.get("min_yoe")
    if isinstance(min_yoe, int) and min_yoe > resume["total_yoe"] + YOE_TOLERANCE:
        return True, f"needs {min_yoe}y vs {resume['total_yoe']}y (+{YOE_TOLERANCE})"
    return False, ""


def _report(rows: list[dict], cache: dict, resume: dict) -> None:
    parsed = [r for r in rows if cache.get(r["job_id"])]
    print("\n=== 1. Extraction reliability ===")
    print(f"  descriptions attempted : {len(rows)}")
    print(f"  parsed cleanly         : {len(parsed)} "
          f"({100 * len(parsed) / max(len(rows), 1):.0f}%)")
    stated = [r for r in parsed if isinstance(cache[r["job_id"]].get("min_yoe"), int)]
    print(f"  stated a min_yoe       : {len(stated)} "
          f"({100 * len(stated) / max(len(parsed), 1):.0f}%) "
          "-- the rest are null and are KEPT")

    above = [r for r in parsed if (r["rerank_score"] or -9) >= MIN_SCORE]
    kills = [(r, _verdict(cache[r["job_id"]], resume)[1])
             for r in above if _verdict(cache[r["job_id"]], resume)[0]]
    print(f"\n=== 2. Effect above min_score {MIN_SCORE} ===")
    print(f"  jobs above the cutoff  : {len(above)}")
    print(f"  the filter would kill  : {len(kills)}")
    for r, why in sorted(kills, key=lambda t: -(t[0]["rerank_score"] or 0)):
        score = r["relevance_score"]
        print(f"    cross={r['rerank_score']:5.2f} judge={score if score is not None else '--':>4}"
              f"  {r['board_token'][:20]:20s} {r['title'][:38]:38s} | {why}")

    print(f"\n=== 3. Safety floor (must kill nothing >= {SAFETY_FLOOR}) ===")
    violations = [
        (r, why) for r, why in kills
        if r["shortlisted"] or (r["relevance_score"] or 0) >= SAFETY_FLOOR
    ]
    labelled = [r for r in parsed if r["relevance_score"] is not None]
    print(f"  rows carrying a judge score : {len(labelled)} "
          f"(shortlisted: {sum(1 for r in labelled if r['shortlisted'])})")
    print("  NOTE: too few positives for precision/recall; this is a floor, not a metric.")
    if violations:
        print(f"  FAIL — killed {len(violations)} job(s) the judge rated highly:")
        for r, why in violations:
            print(f"    judge={r['relevance_score']} {r['title'][:45]} | {why}")
    else:
        print("  PASS — nothing the judge rated highly was killed.")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="hireshire.db (default: live install)")
    ap.add_argument("--resume", default=None, help="resume PDF (default: from config)")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--limit", type=int, default=0, help="0 = every description")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--cache", default=str(Path(__file__).parent / "cache" / "extraction.json"))
    ap.add_argument("--report-only", action="store_true", help="no calls; reuse cache")
    args = ap.parse_args()

    from hireshire import paths
    db_path = Path(args.db) if args.db else paths.resolve_data("hireshire.db")
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1

    rows = _load_rows(db_path)
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} descriptions from {db_path}")

    cache_path = Path(args.cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cache, resume = blob.get("jobs", {}), blob.get("resume")

    if not args.report_only:
        if not shutil.which("claude"):
            print("claude CLI not on PATH", file=sys.stderr)
            return 1
        sem = asyncio.Semaphore(args.concurrency)

        if resume is None:
            resume_path = args.resume
            if not resume_path:
                from hireshire.matcher.config import load_matcher_config
                resume_path = load_matcher_config().settings.resume_path
            print(f"extracting resume: {resume_path}")
            resume = await _extract(
                extract_resume_text(resume_path)[:12000],
                _RESUME_SYSTEM, _RESUME_SCHEMA, args.model, sem,
            )
            if not resume:
                print("resume extraction failed", file=sys.stderr)
                return 1
            print(f"  -> {resume}")

        todo = [r for r in rows if r["job_id"] not in cache]
        print(f"extracting {len(todo)} descriptions ({len(rows) - len(todo)} cached)")
        results = await asyncio.gather(*[
            _extract(f"{r['title']}\n\n{r['content_text'][:8000]}",
                     _JD_SYSTEM, _JD_SCHEMA, args.model, sem)
            for r in todo
        ])
        for r, res in zip(todo, results):
            if res:
                cache[r["job_id"]] = res
        cache_path.write_text(json.dumps({"resume": resume, "jobs": cache}, indent=2))
        print(f"cache -> {cache_path}")

    if not resume:
        print("no resume extraction in cache; run without --report-only", file=sys.stderr)
        return 1
    _report(rows, cache, resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
