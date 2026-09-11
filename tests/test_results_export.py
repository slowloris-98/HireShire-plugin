"""The results CSV — one file, every job that reached the funnel, best first.

Two files used to answer two questions: a shortlist to work through, and a record
of the run's reasoning that answered "why didn't I see that job?". Their columns
overlapped heavily and neither answered the question underneath both — show me
everything, best first, and tell me what I have already applied to.

What most of this pins is the difference between "no score" and "a score of zero".
The funnel writes a placeholder `relevance_score=0` on every row it drops without a
judge call, and printing that as a verdict is what hid a broken reranker for a whole
run.
"""
from __future__ import annotations

import csv

from hireshire.results_export import FIELDS, results_name, write_results_csv


def record(**over) -> dict:
    base = {
        "job_id": "j1",
        "board_token": "acme",
        "title": "Account Manager",
        "location": "Remote",
        "absolute_url": "https://example.com/j1",
        "scored_at": "2026-08-13T22:44:10Z",
        "posted_at": "2026-08-12T00:00:00Z",
        "relevance_score": 72,
        "encoder_score": 0.61,
        "rerank_score_wide": -3.2,
        "rerank_score": -1.4,
        "rerank_stage": "refined",
        "recommend": True,
        "skipped": False,
        "skip_reason": None,
        "shortlisted": True,
        "cluster_size": 1,
        "cluster_representative": None,
    }
    base.update(over)
    return base


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_every_row_is_written_with_the_declared_columns(tmp_path):
    path = tmp_path / results_name("2026-08-13_153054")
    assert write_results_csv([record(), record(job_id="j2")], path) == path

    rows = read(path)
    assert len(rows) == 2
    assert list(rows[0].keys()) == FIELDS
    assert FIELDS == [
        "posted_at", "company", "job_title", "link",
        "llm_score", "cross_score", "applied", "shortlisted",
    ]


def test_the_two_scores_stay_in_two_columns(tmp_path):
    """One is a 0-100 percentage from an LLM judge, the other an unbounded logit
    personal to this user's profile. Merging them would invite exactly the
    comparison that must never be made."""
    path = tmp_path / "x.csv"
    write_results_csv([record()], path)
    row = read(path)[0]

    assert row["llm_score"] == "72"
    assert row["cross_score"] == "-1.4"


def test_the_row_carries_the_posting_date_not_the_processing_date(tmp_path):
    """`posted_at` is when the employer posted it. `scored_at` is when we got to
    it, and is deliberately not a column — a reader asking "how old is this job"
    would read the wrong one."""
    path = tmp_path / "x.csv"
    write_results_csv([record()], path)
    row = read(path)[0]

    assert row["posted_at"] == "2026-08-12T00:00:00Z"
    assert "2026-08-13T22:44:10Z" not in row.values()


def test_a_budget_drop_has_a_blank_llm_score_not_a_zero(tmp_path):
    """`filtered_result` builds skip rows with relevance_score=0. Printing that as
    a score reads as "the model judged this worthless" — the misreading that hid a
    broken reranker for a whole run. The cross score is still there, because that
    IS the number that dropped it."""
    path = tmp_path / "x.csv"
    write_results_csv(
        [record(relevance_score=0, skipped=True, skip_reason="rerank_below_cutoff",
                rerank_score=1.02, shortlisted=False)],
        path,
    )
    row = read(path)[0]

    assert row["llm_score"] == ""
    assert row["cross_score"] == "1.02"
    assert row["shortlisted"] == "no"


def test_a_cluster_sibling_keeps_the_inherited_score(tmp_path):
    """A sibling was judged — once, for the whole cluster — so its number stands.
    This is the one exception to "skipped means unscored", and it is why
    `_never_scored` looks at `cluster_representative` first."""
    path = tmp_path / "x.csv"
    write_results_csv(
        [record(job_id="j2", skipped=True, skip_reason="duplicate_of_cluster",
                cluster_representative="j1", cluster_size=31, shortlisted=False)],
        path,
    )
    assert read(path)[0]["llm_score"] == "72", "an inherited verdict is still a verdict"


def test_a_missing_cross_score_renders_blank_rather_than_zero(tmp_path):
    path = tmp_path / "x.csv"
    write_results_csv([record(rerank_score=None)], path)
    assert read(path)[0]["cross_score"] == ""


def test_applied_and_shortlisted_read_yes_or_no(tmp_path):
    """Two words a spreadsheet can filter on, not two empty-or-1 columns. `applied`
    comes from the caller because the `applied` table is keyed on the job alone and
    has no run to scope it by."""
    path = tmp_path / "x.csv"
    write_results_csv(
        [record(), record(job_id="j2", shortlisted=False)], path, {"j1"}
    )
    rows = read(path)

    assert [r["applied"] for r in rows] == ["yes", "no"]
    assert [r["shortlisted"] for r in rows] == ["yes", "no"]


def test_an_absent_applied_set_says_no_rather_than_guessing(tmp_path):
    path = tmp_path / "x.csv"
    write_results_csv([record()], path)
    assert read(path)[0]["applied"] == "no"


def test_rows_are_sorted_best_first_with_the_unjudged_last(tmp_path):
    """The reason this sorts in Python rather than leaning on the query.
    `load_all_matches` orders by `relevance_score DESC`, which reads a dropped row's
    placeholder 0 as a real score and files it among the genuine low scores instead
    of at the bottom. `_never_scored` is the only thing that knows the difference."""
    path = tmp_path / "x.csv"
    write_results_csv(
        [
            # A 9.9 logit, which would sort it first on the cross score alone, and a
            # placeholder 0 that the query would sort above the 41.
            record(title="Dropped", relevance_score=0, skipped=True,
                   skip_reason="rerank_below_cutoff", rerank_score=9.9),
            record(title="Low", relevance_score=41, rerank_score=4.0),
            record(title="High", relevance_score=88, rerank_score=4.0),
        ],
        path,
    )
    rows = read(path)
    assert [r["job_title"] for r in rows] == ["High", "Low", "Dropped"]
    assert [r["llm_score"] for r in rows] == ["88", "41", ""]


def test_the_cross_encoder_breaks_ties_among_the_unjudged(tmp_path):
    """Every row below the cutoff has a blank LLM score, so without this they come
    out in whatever order the database happened to hand over."""
    path = tmp_path / "x.csv"
    write_results_csv(
        [
            record(job_id="a", relevance_score=0, skipped=True,
                   skip_reason="rerank_below_cutoff", rerank_score=1.0),
            record(job_id="b", relevance_score=0, skipped=True,
                   skip_reason="rerank_below_cutoff", rerank_score=2.5),
            record(job_id="c", relevance_score=0, skipped=True,
                   skip_reason="rerank_below_cutoff", rerank_score=None),
        ],
        path,
    )
    assert [r["cross_score"] for r in read(path)] == ["2.5", "1.0", ""]


def test_an_unwritable_path_returns_none_rather_than_raising(tmp_path):
    """Called from a `finally`, so raising here would replace the real traceback of
    a failed sweep with its own."""
    assert write_results_csv([record()], tmp_path / "no-such-dir" / "x.csv") is None


def test_an_empty_run_still_writes_a_header(tmp_path):
    path = tmp_path / "x.csv"
    assert write_results_csv([], path) == path
    assert read(path) == []
    assert path.read_text(encoding="utf-8-sig").startswith("posted_at,")
