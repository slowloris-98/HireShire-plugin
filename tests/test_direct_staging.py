"""Unit tests for direct-portal job staging (hireshire.direct.staging)."""
from __future__ import annotations

import json

import pytest

from hireshire.direct.staging import load_staged, make_job_id, to_job


def _record(native_id: str = "114438158", **overrides) -> dict:
    record = {
        "native_id": native_id,
        "title": "Software Engineer",
        "url": f"https://jobs.apple.com/en-us/details/{native_id}/swe",
        "updated_at": "2026-08-06T19:54:28Z",
        "location": "Cupertino, California, United States",
    }
    record.update(overrides)
    return record


def _stage(tmp_path, company: str, records: list) -> str:
    run_id = "2026-08-06T00-00-00Z"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{company}.json").write_text(json.dumps(records), encoding="utf-8")
    return run_id


def test_job_id_is_namespaced_by_company():
    # seen_jobs is keyed on job_id alone across every platform, so a bare portal
    # id would collide with a BambooHR/Workday job of the same number.
    assert make_job_id("intuit", "23208") == "direct:intuit:23208"
    assert make_job_id("apple", "23208") != make_job_id("intuit", "23208")


def test_to_job_sets_source_and_strips_html():
    job = to_job(_record(content_html="<p>Build <b>great</b> things.</p>"), "apple")

    assert job.source == "direct"
    assert job.board_token == "apple"
    assert job.job_id == "direct:apple:114438158"
    assert job.content_text == "Build great things."  # strip_html validator ran
    assert job.location.name == "Cupertino, California, United States"
    assert job.updated_at.tzinfo is not None


def test_to_job_defaults_missing_location():
    job = to_job(_record(location=""), "google")
    assert job.location.name == "N/A"


def test_to_job_rejects_relative_url():
    # absolute_url is a pydantic HttpUrl; a relative href must not slip through.
    with pytest.raises(Exception):
        to_job(_record(url="/en-us/details/999/foo"), "apple")


def test_load_staged_counts_malformed_without_dropping_the_batch(tmp_path):
    run_id = _stage(tmp_path, "apple", [
        _record("1"),
        {"native_id": "2", "title": "No url or date"},      # missing required fields
        _record("3", url="/relative/path"),                  # invalid URL
        _record("4"),
    ])

    jobs_by_company, malformed = load_staged(run_id, tmp_path)

    assert [j.job_id for j in jobs_by_company["apple"]] == [
        "direct:apple:1", "direct:apple:4",
    ]
    assert malformed["apple"] == 2


def test_load_staged_dedupes_within_a_company(tmp_path):
    # A paginated scrape can re-emit a job when the portal reorders between pages.
    run_id = _stage(tmp_path, "intuit", [_record("23208"), _record("23208")])

    jobs_by_company, _ = load_staged(run_id, tmp_path)

    assert len(jobs_by_company["intuit"]) == 1


def test_load_staged_keeps_empty_company(tmp_path):
    run_id = _stage(tmp_path, "meta", [])

    jobs_by_company, malformed = load_staged(run_id, tmp_path)

    # Present but empty — records the company as reached with no jobs.
    assert jobs_by_company == {"meta": []}
    assert malformed == {}


def test_load_staged_missing_run_dir_is_not_an_error(tmp_path):
    assert load_staged("nope", tmp_path) == ({}, {})


def test_load_staged_survives_unreadable_file(tmp_path):
    run_id = "2026-08-06T00-00-00Z"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "google.json").write_text("{ not json", encoding="utf-8")
    (run_dir / "apple.json").write_text(json.dumps([_record()]), encoding="utf-8")

    jobs_by_company, malformed = load_staged(run_id, tmp_path)

    assert len(jobs_by_company["apple"]) == 1   # one bad file never costs the others
    assert "google" in malformed
