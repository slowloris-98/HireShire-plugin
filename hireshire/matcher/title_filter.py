from __future__ import annotations

from datetime import datetime, timezone

from hireshire.matcher.config import TitleFilterConfig
from hireshire.matcher.scorer import MatchResult
from hireshire.models.job import Job


def filtered_result(job: Job, reason: str, run_id: str) -> MatchResult:
    """Build the skipped MatchResult for a job dropped by a title/relevance gate.

    Shared by apply_title_filter and the matcher funnel so every gate emits an
    identically-shaped rejection row."""
    return MatchResult(
        job_id=job.job_id,
        board_token=job.board_token,
        title=job.title,
        location=job.location.name,
        absolute_url=str(job.absolute_url),
        relevance_score=0,
        match_reasons=[],
        disqualifiers=[],
        recommend=False,
        skipped=True,
        skip_reason=reason,
        scored_at=datetime.now(timezone.utc),
        source_run_id=run_id,
    )


def apply_title_filter(
    jobs: list[Job],
    cfg: TitleFilterConfig,
    run_id: str,
) -> tuple[list[Job], list[MatchResult]]:
    """Split jobs into (passing_to_llm, title_filtered_results).

    A job is filtered out when:
    - its title contains any exclude_keyword, OR
    - include_keywords is non-empty and its title contains none of them
    Matching is case-insensitive substring.
    """
    includes = [kw.lower() for kw in cfg.include_keywords]
    excludes = [kw.lower() for kw in cfg.exclude_keywords]

    passing: list[Job] = []
    filtered: list[MatchResult] = []

    for job in jobs:
        title_lower = job.title.lower()

        if any(kw in title_lower for kw in excludes):
            reason = "title_excluded"
        elif includes and not any(kw in title_lower for kw in includes):
            reason = "title_no_include_match"
        else:
            passing.append(job)
            continue

        filtered.append(filtered_result(job, reason, run_id))

    return passing, filtered
