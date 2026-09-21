"""Whole-word keyword matching for the title gate.

`apply_title_filter` is the `funnel.enabled: false` branch of `matcher.gate()` — live,
just conditional, and until this file existed it had no coverage at all. The matching
it shares with the streaming funnel is `title_matches`, so the boundary rules are
pinned here once and the funnel's own test only covers the wiring.

Why whole words: a `title_excluded` drop is a verdict, not a deferral. It is absent
from `_RETRYABLE_SKIP_REASONS`, so the job lands in `seen_jobs` and never comes back —
editing the keyword afterwards does not undo it. Substring matching made ordinary
terms destructive ("ios" matched Kiosk, "mobile" matched Automobile, "senior" matched
Seniority), and each one quietly deleted a slice of the user's market permanently.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hireshire.matcher.config import TitleFilterConfig
from hireshire.matcher.title_filter import apply_title_filter, title_matches
from hireshire.models.job import Job

RUN_ID = "test-run"


def make_job(title: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token="acme",
        job_id=title,
        title=title,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now,
        content_text="desc",
        scraped_at=now,
    )


def split(titles: list[str], *, include=None, exclude=None):
    """Return ({kept titles}, {title: skip_reason})."""
    cfg = TitleFilterConfig(
        include_keywords=include or [],
        exclude_keywords=exclude or [],
    )
    passing, filtered = apply_title_filter([make_job(t) for t in titles], cfg, RUN_ID)
    return {j.title for j in passing}, {r.title: r.skip_reason for r in filtered}


@pytest.mark.parametrize(
    "keyword, title, matches",
    [
        # The case that motivated the change.
        ("intern", "Intern", True),
        ("intern", "Data Intern (Summer)", True),
        ("intern", "Internal Tools Developer", False),
        ("intern", "International Sales Lead", False),
        # Nothing is stemmed, in either direction. Deliberate: a suffix allowance is a
        # guess about morphology, and the user can list the plural themselves.
        ("intern", "Interns", False),
        ("intern", "Internship Program", False),
        ("internship", "Intern", False),
        # Short terms were the worst offenders under substring matching.
        ("ios", "iOS Engineer", True),
        ("ios", "Kiosk Manager", False),
        ("ios", "Biosciences Analyst", False),
        ("mobile", "Mobile Robotics Engineer", True),
        ("mobile", "Automobile Design Engineer", False),
        ("senior", "Senior AI Engineer", True),
        ("senior", "Seniority Programs Lead", False),
        # Multi-word phrases match across the space and still bound at both ends.
        ("staff engineer", "Staff Engineer II", True),
        ("staff engineer", "Staff Engineering Manager", False),
        ("staff engineer", "Staff Accountant", False),
        ("head of", "Head of Engineering", True),
        ("head of", "Headhunter", False),
        # A keyword ending in a word char still bounds there, so "vp of" misses AVP.
        ("vp of", "VP of Engineering", True),
        ("vp of", "AVP of Analytics", False),
        # Matching is case-insensitive on both sides.
        ("SENIOR", "senior data scientist", True),
    ],
)
def test_keywords_match_whole_words(keyword, title, matches):
    assert title_matches(title.lower(), [keyword]) is matches


@pytest.mark.parametrize(
    "keyword, title, matches",
    [
        # Trailing space is stripped. Setup drafts "sr. " that way to dodge SRE under
        # the old substring rule; a boundary placed after the space would never fire.
        ("sr. ", "Sr. Software Engineer", True),
        ("sr.", "Sr. Software Engineer", True),
        # The period is escaped, so it is not a wildcard.
        ("sr.", "SRX Software Engineer", False),
        # "sr" bounds on both sides now, so the defensive spelling is no longer needed.
        ("sr", "Sr Software Engineer", True),
        ("sr", "SRE Manager", False),
        # A comma inside a phrase is matched literally.
        ("manager, engineering", "Manager, Engineering", True),
        ("manager, engineering", "Manager Engineering", False),
        # Hyphens and plus signs are literals, not regex syntax.
        ("full-stack", "Full-Stack Developer", True),
        ("full-stack", "Full Stack Developer", False),
        ("c++", "C++ Engineer", True),
    ],
)
def test_punctuated_keywords_are_literal_and_bound_conditionally(keyword, title, matches):
    assert title_matches(title.lower(), [keyword]) is matches


def test_a_blank_keyword_matches_nothing():
    """An empty string is `in` every title. Unguarded it would empty the whole sweep."""
    assert not title_matches("ai engineer", ["", "   "])


def test_exclude_wins_over_include():
    """Exclusions are tested first, so an overlapping include cannot rescue a title."""
    kept, dropped = split(
        ["Senior AI Engineer", "AI Engineer"],
        include=["ai engineer"],
        exclude=["senior"],
    )
    assert kept == {"AI Engineer"}
    assert dropped == {"Senior AI Engineer": "title_excluded"}


def test_include_list_gates_on_whole_words_too():
    kept, dropped = split(
        ["AI Engineer", "Engineering Manager", "Barista"],
        include=["engineer"],
    )
    # "Engineering" is a different word, so it no longer satisfies the include.
    assert kept == {"AI Engineer"}
    assert dropped == {
        "Engineering Manager": "title_no_include_match",
        "Barista": "title_no_include_match",
    }


def test_an_empty_include_list_gates_nothing():
    kept, dropped = split(["Barista", "AI Engineer"])
    assert kept == {"Barista", "AI Engineer"}
    assert dropped == {}
