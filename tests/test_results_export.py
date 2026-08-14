"""The all-jobs CSV — the file that answers "why didn't I see that job?".

The bug that motivated it: a sweep shortlisted nothing, and the only artefact was
an empty CSV. Every score the funnel computed had either been discarded or written
in a form that read as a verdict when it wasn't.
"""
from __future__ import annotations

import csv

from hireshire.results_export import FIELDS, all_jobs_name, write_all_jobs_csv


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
    path = tmp_path / all_jobs_name("2026-08-13_153054")
    assert write_all_jobs_csv([record(), record(job_id="j2")], path) == path

    rows = read(path)
    assert len(rows) == 2
    assert list(rows[0].keys()) == FIELDS


def test_the_four_scores_stay_in_four_columns(tmp_path):
    """They are a cosine, two logits from different models, and a percentage.
    Merging them would invite exactly the comparison that must never be made."""
    path = tmp_path / "x.csv"
    write_all_jobs_csv([record()], path)
    row = read(path)[0]

    assert row["llm_score"] == "72"
    assert row["cross_score_refined"] == "-1.4"
    assert row["cross_score_wide"] == "-3.2"
    assert row["bi_score"] == "0.61"


def test_a_budget_drop_has_a_blank_llm_score_not_a_zero(tmp_path):
    """`filtered_result` builds skip rows with relevance_score=0. Printing that as
    a score reads as "the model judged this worthless" — the misreading that hid a
    broken reranker for a whole run. The cross score is still there, because that
    IS the number that dropped it."""
    path = tmp_path / "x.csv"
    write_all_jobs_csv(
        [record(relevance_score=0, skipped=True, skip_reason="rerank_below_top_k",
                rerank_score=None, rerank_stage="wide", shortlisted=False)],
        path,
    )
    row = read(path)[0]

    assert row["llm_score"] == ""
    assert row["cross_score_wide"] == "-3.2"
    assert row["cross_score_refined"] == ""
    assert row["status"] == "rerank_below_top_k"


def test_a_cluster_sibling_keeps_the_inherited_score(tmp_path):
    """A sibling was judged — once, for the whole cluster — so its number stands.
    `cluster_representative` says where the verdict came from, so nothing looks
    like an independent second opinion."""
    path = tmp_path / "x.csv"
    write_all_jobs_csv(
        [record(job_id="j2", skipped=True, skip_reason="duplicate_of_cluster",
                cluster_representative="j1", cluster_size=31, shortlisted=False)],
        path,
    )
    row = read(path)[0]

    assert row["llm_score"] == "72", "an inherited verdict is still a verdict"
    assert row["cluster_representative"] == "j1"
    assert row["cluster_size"] == "31"
    assert row["status"] == "duplicate_of_cluster"


def test_missing_scores_render_blank_rather_than_zero(tmp_path):
    path = tmp_path / "x.csv"
    write_all_jobs_csv(
        [record(encoder_score=None, rerank_score=None, rerank_score_wide=None,
                rerank_stage=None)],
        path,
    )
    row = read(path)[0]
    assert row["bi_score"] == ""
    assert row["cross_score_refined"] == ""
    assert row["cross_score_wide"] == ""
    assert row["rerank_stage"] == ""


def test_status_distinguishes_scored_but_below_threshold(tmp_path):
    path = tmp_path / "x.csv"
    write_all_jobs_csv([record(relevance_score=40, shortlisted=False)], path)
    assert read(path)[0]["status"] == "scored_below_threshold"


def test_an_unwritable_path_returns_none_rather_than_raising(tmp_path):
    """A diagnostic must never take down a run whose real output is already safe."""
    assert write_all_jobs_csv([record()], tmp_path / "no-such-dir" / "x.csv") is None


def test_an_empty_run_still_writes_a_header(tmp_path):
    path = tmp_path / "x.csv"
    assert write_all_jobs_csv([], path) == path
    assert read(path) == []
    assert path.read_text(encoding="utf-8-sig").startswith("processed_at,")
