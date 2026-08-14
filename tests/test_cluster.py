"""Title normalisation for duplicate-requisition clustering.

Every case here is drawn from a real sweep. The failure mode being guarded against
is over-merging: collapsing two genuinely different jobs into one costs the user a
match they never learn existed, and leaves no trace anywhere. Under-merging only
costs a budget slot, so the normaliser is deliberately biased toward doing nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hireshire.funnel.cluster import cluster_key, group, normalise_title, pick_representative
from hireshire.funnel.rerank import RerankScores
from hireshire.models.job import Job


def same(a: str, b: str) -> bool:
    return normalise_title(a) == normalise_title(b)


# --- must merge: cosmetic differences between copies of one requisition ----------

@pytest.mark.parametrize(
    "a,b",
    [
        # Townsquare Media posted this 31 times; one copy carried its station name.
        ("Multi-Media Account Executive", "Multi-Media Account Executive - 101.5"),
        # EquipmentShare posted this 12 times with a discipline suffix.
        ("Territory Account Manager", "Territory Account Manager (Pump, Power & HVAC)"),
        ("Account Manager", "Account Manager (NYC or LA)"),
        ("Sales Associate", "Sales Associate- The Woodlands, TX"),
        ("Account Manager", "  Account Manager  "),
        ("Account Manager", "ACCOUNT MANAGER"),
        ("Client Success Manager", "Client Success Manager #48213"),
        ("Client Success Manager", "Client Success Manager - REQ-4471"),
        # Stacked suffixes: the normaliser loops until nothing more comes off.
        ("Account Manager", "Account Manager (Remote) - 101.5"),
    ],
)
def test_cosmetic_variants_cluster_together(a, b):
    assert same(a, b), f"{a!r} and {b!r} should be one cluster"


# --- must NOT merge: genuinely different jobs ------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        # A seniority prefix is the job, not decoration.
        ("Multi-Media Account Executive", "Senior Multi-Media Account Executive"),
        # A trailing *word* is a real qualifier. This is why the req-id pattern
        # demands digits after its separator.
        ("Account Executive", "Account Executive | Growth"),
        ("Account Executive", "Account Executive - Enterprise"),
        # A bare trailing number is usually a level, not a requisition id — hence
        # the separator requirement in _TRAILING_REQ.
        ("Analyst", "Analyst 3"),
        ("Software Engineer", "Software Engineer III"),
        # Different roles that merely share a prefix.
        ("Behavior Technician", "Registered Behavior Technician"),
        ("Customer Success Manager", "Customer Success Associate"),
    ],
)
def test_distinct_roles_stay_separate(a, b):
    assert not same(a, b), f"{a!r} and {b!r} must not be merged"


def test_a_title_is_never_normalised_out_of_existence():
    """A title that is nothing but a station number must keep an identity of its
    own rather than collapsing onto the empty string and joining every other
    stripped title."""
    assert normalise_title("- 101.5") != ""
    assert normalise_title("- 101.5") != normalise_title("- 99.7")


def test_non_breaking_spaces_do_not_split_a_cluster():
    assert same("Account Manager", "Account Manager")


def test_empty_and_none_titles_do_not_raise():
    assert normalise_title("") == ""
    assert normalise_title(None) == ""


# --- keys and representatives ----------------------------------------------------

def make_job(job_id: str, title: str, board: str = "acme", age_days: int = 0) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=board,
        job_id=job_id,
        title=title,
        location={"name": "Remote"},
        absolute_url="https://example.com/job",
        updated_at=now - timedelta(days=age_days),
        content_text="desc",
        scraped_at=now,
    )


def test_the_company_is_part_of_the_key():
    """'Account Manager' at two employers is two jobs. Only repeats within one
    board token can be the same requisition."""
    a = make_job("1", "Account Manager", board="acme")
    b = make_job("2", "Account Manager", board="globex")
    assert cluster_key(a) != cluster_key(b)


def test_group_buckets_by_key():
    jobs = [
        make_job("1", "Account Manager"),
        make_job("2", "Account Manager (Remote)"),
        make_job("3", "Client Success Manager"),
    ]
    clusters = group(jobs)
    assert len(clusters) == 2
    assert sorted(len(v) for v in clusters.values()) == [1, 2]


def test_representative_is_the_best_scoring_member():
    """The cluster is judged on its strongest copy, not whichever was scraped
    first — otherwise scrape order decides what the LLM sees."""
    members = [make_job("1", "AM"), make_job("2", "AM"), make_job("3", "AM")]
    scores = {
        "1": RerankScores(wide=-4.0, refined=-3.0),
        "2": RerankScores(wide=-1.0, refined=-0.5),  # best
        "3": RerankScores(wide=-2.0, refined=-2.5),
    }
    assert pick_representative(members, scores).job_id == "2"


def test_a_refined_member_beats_an_unrefined_one_with_a_bigger_number():
    """Same scale trap as the budget: refinement dominates the raw float."""
    members = [make_job("1", "AM"), make_job("2", "AM")]
    scores = {
        "1": RerankScores(wide=-9.0, refined=-8.0),
        "2": RerankScores(wide=99.0),
    }
    assert pick_representative(members, scores).job_id == "1"


def test_ties_break_toward_the_most_recent_posting():
    """Equal scores: prefer the freshest, which is likeliest to still be open."""
    old = make_job("old", "AM", age_days=30)
    new = make_job("new", "AM", age_days=1)
    scores = {
        "old": RerankScores(wide=-1.0, refined=-1.0),
        "new": RerankScores(wide=-1.0, refined=-1.0),
    }
    assert pick_representative([old, new], scores).job_id == "new"


def test_a_member_with_no_score_never_wins():
    members = [make_job("scored", "AM"), make_job("unscored", "AM")]
    scores = {"scored": RerankScores(wide=-5.0)}
    assert pick_representative(members, scores).job_id == "scored"
