"""The deterministic years-of-experience gate.

Two halves: the parser (`hireshire/funnel/experience.py`) and the funnel stage that
acts on it (`matcher._process_batch`). The parser tests double as the regression
suite for the context guards — each rejection case below is a sentence that appears
in a real posting and that a naive `\\d+ years` reads as a requirement.
"""
from __future__ import annotations

import asyncio

import pytest

from hireshire.funnel.config import ExperienceConfig
from hireshire.funnel.experience import meets, parse_requirement
from matcher import (
    YOE_SKIP_REASON,
    _CallBudget,
    _RETRYABLE_SKIP_REASONS,
    _process_batch,
)
from test_budget import RUN_ID, FakeReranker, make_job


def run_batch(jobs, exp_cfg, reranker=None, min_score=0.0, cap=0):
    """`_process_batch` with the experience gate configured."""
    return asyncio.run(
        _process_batch(
            jobs, reranker or FakeReranker(), min_score, _CallBudget(cap), RUN_ID,
            True, 10, exp_cfg,
        )
    )


def enabled(years: float, tolerance: float = 0.5) -> ExperienceConfig:
    return ExperienceConfig(
        enabled=True, candidate_years=years, tolerance_years=tolerance
    )


# --- the parser ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("We want 5+ years of experience.", 5),
    ("Requires 5 plus years of professional experience", 5),
    ("Minimum of 8 yrs of industry experience", 8),
    ("At least five years of relevant experience as a Superintendent", 5),
    ("15+ years of industry experience in senior software engineering roles", 15),
    ("Looking for 5+ years of experience", 5),
    ("Candidates with 5+ years of experience preferred", 5),
])
def test_a_stated_minimum_is_read_however_it_is_phrased(text, expected):
    assert parse_requirement(text).min_years == expected


@pytest.mark.parametrize("text", [
    "3-7 years of professional experience required",
    "3 to 7 years of professional experience required",
    "3–7 years of professional experience required",
])
def test_a_range_is_read_as_its_lower_bound(text):
    """And as ONE requirement, not two.

    The upper bound is matched purely so it is consumed: left alone, "3-7 years"
    would also match "7 years" and the pair would look like two separate stated
    minimums of 3 and 7.
    """
    req = parse_requirement(text)
    assert req.min_years == 3
    assert req.stated == (3.0,)


def test_the_lowest_of_several_requirements_governs():
    """Postings routinely stack requirements of different weights.

    The lowest is the conservative reading: it drops only a candidate who misses
    even the easiest bar the posting names. Measured against 213 LLM-labelled
    descriptions it was the only aggregation that never read HIGHER than the label,
    which is the error direction that kills a job wrongly.
    """
    req = parse_requirement(
        "8+ years of experience leading teams. "
        "12+ years of experience in full-stack development. "
        "3+ years of experience running large-scale infrastructure."
    )
    assert req.min_years == 3
    assert req.stated == (8.0, 12.0, 3.0)


def test_preferred_is_treated_exactly_like_required():
    """Deliberate, and a departure from the LLM extractor this replaces.

    Employers use the words interchangeably, and the spike's prompt — which reported
    only explicitly-required years — returned null on most preference-phrased
    postings and lost them from the gate entirely.
    """
    assert parse_requirement("2+ years of experience preferred").min_years == 2
    assert parse_requirement("2+ years of experience required").min_years == 2


@pytest.mark.parametrize("text", [
    "A great place to work with excellent benefits.",
    # Company history. The canonical false positive: read naively this is a 30-year
    # requirement, and it sits in the first paragraph of the posting.
    "For 30 years, Per Scholas has been on a mission to drive mobility.",
    "With over 100 years of combined experience, our team...",
    # Benefits and tenure, not requirements.
    "401(k) matching after 2 years of service",
    "Performance review every 2 years of employment experience",
    # Retrospective and eligibility phrasing.
    "I left that job 5 years ago after gaining experience",
    "Must be 18 years of age or older with experience",
])
def test_text_that_states_no_requirement_yields_nothing(text):
    assert parse_requirement(text) is None


def test_an_empty_description_yields_nothing():
    assert parse_requirement(None) is None
    assert parse_requirement("") is None


# --- the decision ----------------------------------------------------------------

@pytest.mark.parametrize("text,candidate,expected", [
    # The worked examples from the design, at the default 0.5 tolerance.
    ("preferred 4+ years of experience", 4.0, True),
    ("preferred 4+ years of experience", 3.5, True),   # exactly on the boundary
    ("preferred 5+ years of experience", 4.0, False),
    ("preferred 5+ years of experience", 3.5, False),
    ("3-7 years of experience", 2.6, True),
    ("3-7 years of experience", 2.4, False),
    # Nothing gates from above: the upper bound of a range is not a ceiling.
    ("3-7 years of experience", 15.0, True),
    # An unstated requirement always passes.
    ("A great place to work.", 0.0, True),
])
def test_the_candidate_is_kept_when_they_reach_the_bar_within_tolerance(
    text, candidate, expected
):
    assert meets(parse_requirement(text), candidate, 0.5) is expected


def test_every_tie_resolves_toward_keeping_the_job():
    """`candidate + tolerance == required` keeps.

    The gate retires jobs permanently, so the boundary belongs on the side that
    costs an LLM call rather than the side that discards a posting for ever.
    """
    req = parse_requirement("5+ years of experience")
    assert meets(req, 4.5, 0.5) is True
    assert meets(req, 4.49, 0.5) is False


# --- the funnel stage ------------------------------------------------------------

def test_a_job_asking_for_more_years_than_the_candidate_has_is_dropped():
    jobs = [make_job("5", content_text="We need 10+ years of experience in this.")]
    winners, dropped, _ = run_batch(jobs, enabled(4))

    assert winners == []
    assert [r.skip_reason for r in dropped] == [YOE_SKIP_REASON]


def test_the_drop_keeps_the_rerank_score_so_a_wrong_kill_is_visible():
    """The reason this stage runs after the reranker rather than before it.

    "Dropped on experience, but the cross-encoder scored it 5.0" is what separates a
    gate doing its job from a `candidate_years` that is set too low. Without the
    logit on the row there is nothing to tell the two apart.
    """
    jobs = [make_job("5", content_text="We need 10+ years of experience in this.")]
    _, dropped, _ = run_batch(jobs, enabled(4))

    assert dropped[0].rerank_score == 5.0
    assert dropped[0].yoe_required == 10.0


def test_a_job_stating_no_requirement_is_never_dropped_here():
    jobs = [make_job("5", content_text="A wonderful place to build your career.")]
    winners, dropped, _ = run_batch(jobs, enabled(4))

    assert [j.job_id for j, _ in winners] == ["5"]
    assert dropped == []
    # Nothing was stated, so there is no number to record.
    assert winners[0][0].content_text is not None


def test_the_requirement_is_recorded_even_when_the_gate_is_switched_off():
    """Reading the number is free; only acting on it is configurable.

    A column that appeared only once the gate was on would leave a user no way to
    find out what turning it on would cost them.
    """
    jobs = [make_job("5", content_text="We need 10+ years of experience in this.")]
    winners, dropped, _ = run_batch(jobs, ExperienceConfig())

    assert [j.job_id for j, _ in winners] == ["5"]
    assert dropped == []


@pytest.mark.parametrize("cfg", [
    ExperienceConfig(),                                        # shipped default
    ExperienceConfig(enabled=True, candidate_years=0),         # setup never ran
    ExperienceConfig(enabled=False, candidate_years=4),        # explicitly off
    None,                                                      # caller passed nothing
])
def test_the_gate_is_inert_until_it_is_both_enabled_and_given_a_number(cfg):
    """Two locks on the same door.

    An install that predates this feature, or one whose setup was skipped, must not
    start filtering every posting in the sweep against a candidate with "no
    experience" — which is what `candidate_years: 0` would mean if `enabled` alone
    were enough.
    """
    jobs = [make_job("5", content_text="We need 10+ years of experience in this.")]
    winners, dropped, _ = run_batch(jobs, cfg)

    assert [j.job_id for j, _ in winners] == ["5"]
    assert dropped == []


def test_the_drop_is_a_verdict_and_retires_the_job():
    """Deterministic in, deterministic out.

    Same description and same `candidate_years` produce the same answer, so with a
    4-hour poll and a 24-hour age window retrying it would re-run one computation
    ~6 times a day to reach the identical result. This reverses the guidance in
    analysis/results/extraction_prefilter.md, which was written about an LLM
    extractor — where a misparse IS a transient failure worth retrying.
    """
    assert YOE_SKIP_REASON not in _RETRYABLE_SKIP_REASONS


def test_the_cutoff_still_wins_when_a_job_fails_both_gates():
    """Ordering matters for the report, not for the outcome.

    A job below `min_score` is recorded as a cutoff drop even if it would also have
    failed on experience, which keeps `above_cutoff` counting what the matching
    report says it counts.
    """
    jobs = [make_job("1", content_text="We need 10+ years of experience in this.")]
    _, dropped, _ = run_batch(jobs, enabled(4), min_score=5.0)

    assert dropped[0].skip_reason == "rerank_below_cutoff"


def test_siblings_of_a_dropped_representative_share_its_fate_and_its_number():
    """A cluster is keyed on (board_token, description), so the whole group states
    the same requirement by construction — it is parsed once, for the representative.
    """
    shared = "We need 10+ years of experience running distributed systems at scale."
    jobs = [make_job(str(i), content_text=shared) for i in (7, 8, 9)]
    winners, dropped, _ = run_batch(jobs, enabled(4))

    assert winners == []
    assert len(dropped) == 3
    assert {r.skip_reason for r in dropped} == {YOE_SKIP_REASON}
    assert {r.yoe_required for r in dropped} == {10.0}
