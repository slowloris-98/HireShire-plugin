from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone

from hireshire.matcher.config import TitleFilterConfig
from hireshire.matcher.scorer import MatchResult
from hireshire.models.job import Job

_PATTERN_CACHE: dict[str, re.Pattern[str] | None] = {}


def _pattern(keyword: str) -> re.Pattern[str] | None:
    """Compile one keyword into a whole-word matcher, or None if it is blank.

    The boundaries are lookarounds rather than `\\b` because they are applied
    *conditionally*: `\\b` asserts a transition, so its meaning flips with the adjacent
    character and a trailing one on `"sr."` would demand the very word character the
    keyword is trying to stop at. A keyword that already begins or ends in punctuation
    therefore gets no boundary on that side.

    Stripping is load-bearing: setup drafts `"sr. "` with a trailing space, and a
    boundary placed after that space would never match."""
    if keyword in _PATTERN_CACHE:
        return _PATTERN_CACHE[keyword]

    kw = keyword.strip().lower()
    if not kw:
        # A blank keyword is a config slip, and an unguarded one would match everything.
        _PATTERN_CACHE[keyword] = None
        return None

    lead = r"(?<!\w)" if _is_word_char(kw[0]) else ""
    trail = r"(?!\w)" if _is_word_char(kw[-1]) else ""
    compiled = re.compile(lead + re.escape(kw) + trail)
    _PATTERN_CACHE[keyword] = compiled
    return compiled


def _is_word_char(ch: str) -> bool:
    """Match the `\\w` class the boundary lookarounds are written against."""
    return ch.isalnum() or ch == "_"


def title_matches(title_lower: str, keywords: Sequence[str]) -> bool:
    """True when any keyword appears in the already-lowercased title as a whole word.

    Whole-word rather than substring because a title-gate drop is a verdict, not a
    skip: excluding `intern` must not silently retire Internal Tools Developer for the
    life of the install. Multi-word phrases and punctuation (`"staff engineer"`,
    `"manager, engineering"`) are matched literally."""
    return any(
        pattern.search(title_lower)
        for pattern in (_pattern(kw) for kw in keywords)
        if pattern is not None
    )


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
    Matching is case-insensitive and whole-word — see `title_matches`.
    """
    includes = cfg.include_keywords
    excludes = cfg.exclude_keywords

    passing: list[Job] = []
    filtered: list[MatchResult] = []

    for job in jobs:
        title_lower = job.title.lower()

        if title_matches(title_lower, excludes):
            reason = "title_excluded"
        elif includes and not title_matches(title_lower, includes):
            reason = "title_no_include_match"
        else:
            passing.append(job)
            continue

        filtered.append(filtered_result(job, reason, run_id))

    return passing, filtered
