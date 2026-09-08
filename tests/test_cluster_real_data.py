"""Clustering, checked against descriptions taken verbatim from a production database.

Two halves. The first pins the *mechanism* with synthetic text, because a word count
is only meaningful if you can say exactly what counts as a word. The second pins the
*outcomes* against real postings, because every threshold in `cluster.py` was derived
from them and would otherwise be a number nobody could defend.

**Provenance.** `tests/fixtures/cluster_descriptions.json` holds eleven postings
scraped on 2026-07-21, with their full description text — untruncated, because
truncating changes the word diff and would silently invalidate every number here.
They were selected from an audit of 192,700 rows across the ten largest sweeps.

The three employers are not arbitrary; each pins a different property:

- **Veterinary Emergency Group** carries both behaviours in one title family. Four
  postings differ only by clinic and must merge; `(Overnight)` and `(Part Time)` are
  separate jobs and must not. This is the case the old title-based key got wrong.
- **SpaceX** is the failure that forced the redesign. Its titles are qualified by
  programme, so stripping the trailing parenthetical collapsed unrelated engineering
  roles into one cluster and retired the losers permanently under
  `duplicate_of_cluster`, which is not retryable.
- **sweetgreen** is the cost this design accepts. It ships byte-identical text for
  `Assistant Coach` and `Assistant Restaurant Manager`. With the title ignored,
  nothing separates them — so that merge is asserted here deliberately, to keep the
  trade visible in the suite rather than surfacing later as a bug report.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hireshire.funnel.cluster import (
    DEFAULT_MAX_WORD_DIFF,
    group,
    normalise_description,
    word_diff,
)
from hireshire.models.job import Job

FIXTURE = Path(__file__).parent / "fixtures" / "cluster_descriptions.json"


def make_job(job_id, title="A Job", board="acme", content_text="text", age_days=0) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=board,
        job_id=str(job_id),
        title=title,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now - timedelta(days=age_days),
        content_text=content_text,
        scraped_at=now,
    )


def load_fixture(board=None, title_contains=None):
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if board:
        rows = [r for r in rows if r["board_token"] == board]
    if title_contains:
        rows = [r for r in rows if title_contains in r["title"]]
    return rows


def jobs_from(rows):
    return [
        make_job(r["job_id"], title=r["title"], board=r["board_token"],
                 content_text=r["content_text"])
        for r in rows
    ]


def cluster_sizes(jobs, **kw):
    return sorted(len(c) for c in group(jobs, **kw))


# --- the metric ------------------------------------------------------------------

BASE = tuple(f"word{i}" for i in range(200))


def text(tokens) -> str:
    return " ".join(tokens)


def test_a_substitution_costs_two_not_one():
    """One word out and one word in. Every threshold in cluster.py is calibrated in
    these units, so this is the assertion that stops the scale drifting."""
    changed = ("replacement",) + BASE[1:]
    assert word_diff(BASE, changed) == 2


def test_an_insertion_costs_one():
    assert word_diff(BASE, BASE + ("extra",)) == 1


def test_identical_text_is_zero():
    assert word_diff(BASE, BASE) == 0


def test_whitespace_and_case_do_not_count_as_differences():
    """Boards emit non-breaking spaces and re-cased titles for the same requisition."""
    a = normalise_description("Senior  Engineer\nRemote")
    b = normalise_description("senior engineer remote")
    assert a == b
    assert word_diff(a, b) == 0


@pytest.mark.parametrize("insertions,should_merge", [(0, True), (5, True), (10, True),
                                                     (11, False), (40, False)])
def test_the_threshold_is_inclusive_at_ten(insertions, should_merge):
    a = make_job("a", content_text=text(BASE))
    b = make_job("b", content_text=text(BASE + tuple(f"x{i}" for i in range(insertions))))
    merged = len(group([a, b])) == 1
    assert merged is should_merge


def test_the_threshold_is_configurable():
    a = make_job("a", content_text=text(BASE))
    b = make_job("b", content_text=text(BASE + ("x", "y", "z")))
    assert len(group([a, b], max_word_diff=2)) == 2
    assert len(group([a, b], max_word_diff=3)) == 1


# --- descriptions that are absent, not merely different --------------------------

@pytest.mark.parametrize("empty", [None, "", "   "])
def test_postings_without_a_description_are_never_clustered(empty):
    """An absent description is an absence of evidence, not evidence of uniqueness.
    `strip_html` turns markup-only HTML into "", so both forms occur in practice."""
    jobs = [make_job(i, content_text=empty) for i in range(3)]
    assert cluster_sizes(jobs) == [1, 1, 1]


def test_an_absent_description_never_joins_a_real_cluster():
    jobs = [
        make_job("a", content_text=text(BASE)),
        make_job("b", content_text=text(BASE)),
        make_job("empty", content_text=None),
    ]
    assert cluster_sizes(jobs) == [1, 2]


# --- anchoring ------------------------------------------------------------------

def test_the_representative_is_the_best_scoring_member():
    """The cluster is judged on its strongest copy, and `group` puts it first."""
    jobs = [make_job(i, content_text=text(BASE)) for i in ("a", "b", "c")]
    clusters = group(jobs, {"a": -3.0, "b": -0.5, "c": -2.5})
    assert len(clusters) == 1
    assert clusters[0][0].job_id == "b"


def test_merging_does_not_chain_through_intermediate_members():
    """A and B differ by 6, B and C by 6, A and C by 12. Under transitive merging C
    would join via B and inherit a verdict from a posting 12 words away. Every member
    must be within the threshold of the representative *itself*."""
    a = make_job("a", content_text=text(BASE))
    b = make_job("b", content_text=text(BASE + tuple(f"x{i}" for i in range(6))))
    c = make_job("c", content_text=text(BASE + tuple(f"x{i}" for i in range(12))))
    # Scores force `a` to anchor, so `c` is measured against `a`, not against `b`.
    clusters = group([a, b, c], {"a": 3.0, "b": 2.0, "c": 1.0})

    assert sorted(len(x) for x in clusters) == [1, 2]
    for members in clusters:
        rep = members[0]
        for sib in members[1:]:
            assert word_diff(
                normalise_description(rep.content_text),
                normalise_description(sib.content_text),
            ) <= DEFAULT_MAX_WORD_DIFF


def test_clusters_never_span_two_employers():
    """Agency boilerplate is shared across companies; a match there is not a repost."""
    a = make_job("a", board="acme", content_text=text(BASE))
    b = make_job("b", board="globex", content_text=text(BASE))
    assert cluster_sizes([a, b]) == [1, 1]


# --- real postings ---------------------------------------------------------------

def test_veg_merges_clinics_and_splits_qualifiers():
    """The case the title key got wrong, end to end.

    Four postings differ only by clinic (0-4 words) and are one requisition.
    `(Overnight)` is 13 words away and `(Part Time)` is 99 — different jobs.
    """
    rows = load_fixture("veterinaryemergencygroupst")
    assert len(rows) == 6
    assert cluster_sizes(jobs_from(rows)) == [1, 1, 4]


def test_veg_the_four_clinic_postings_are_the_ones_that_merged():
    rows = load_fixture("veterinaryemergencygroupst")
    biggest = max(group(jobs_from(rows)), key=len)
    assert all("(" not in j.title for j in biggest), (
        "a qualified posting was absorbed into the plain-location cluster"
    )


def test_spacex_programme_variants_stay_separate():
    """The regression that forced the redesign: stripping the trailing parenthetical
    merged Asset Engineering, Facilities and Raptor Manufacturing Systems into one
    cluster, and the losers were retired permanently under a non-retryable reason."""
    rows = load_fixture("spacex")
    assert len(rows) == 3
    assert cluster_sizes(jobs_from(rows)) == [1, 1, 1]


def test_sweetgreen_template_collision_merges_and_that_is_accepted():
    """A cost this design takes on knowingly, asserted so it stays visible.

    sweetgreen ships byte-identical text for two genuinely different roles. With the
    title ignored there is nothing left to separate them, so they merge and one is
    retired. Fixing this would mean reintroducing title comparison, which is what
    caused the far larger SpaceX failure above. If this test ever fails, the trade
    was changed deliberately — update the module docstring in cluster.py too.
    """
    rows = load_fixture("sweetgreen")
    assert {r["title"] for r in rows} == {"Assistant Coach", "Assistant Restaurant Manager"}
    a, b = (normalise_description(r["content_text"]) for r in rows)
    assert word_diff(a, b) == 0
    assert cluster_sizes(jobs_from(rows)) == [2]
