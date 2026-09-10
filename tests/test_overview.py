"""The minimal overview page — the two accordions, the tail, and its two scopes.

The page's whole promise is that a job appears in exactly one of its three lists,
and that a number on it means what its label says. Most of what follows pins those
two claims, because both are invisible when they break: a job showing up twice
still renders, and a count that quietly means something else still prints.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from hireshire.models.job import Job, Location
from hireshire.reporting import data, overview
from hireshire.reporting.render import duration
from hireshire.storage.db import Database

RUN = "2026-09-09T06-51-12Z"
OLDER = "2026-09-01T00-00-00Z"


# --- fixtures -----------------------------------------------------------------


def _db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def _job(job_id: str, token: str = "acme", title: str = "Backend Engineer") -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        source="greenhouse",
        board_token=token,
        job_id=job_id,
        title=title,
        location=Location(name="Remote"),
        absolute_url="https://example.com/jobs/" + job_id,  # type: ignore[arg-type]
        updated_at=now,
        scraped_at=now,
        content_text="We need a backend engineer.",
    )


def _raw(job_id: str, **over) -> dict:
    record = {
        "job_id": job_id,
        "board_token": "acme",
        "title": "Backend Engineer",
        "absolute_url": f"https://example.com/jobs/{job_id}",
        "relevance_score": 78,
        "encoder_score": 0.51,
        "rerank_score": 7.19,
        "core_skills_score": 34,
        "core_skills_rationale": "Strong Python and service work.",
        "experience_score": 30,
        "experience_rationale": "Six years, mostly backend.",
        "education_bonus_score": 14,
        "education_rationale": "CS degree, no certifications.",
        "match_reasons": ["Python throughout", "Owned a payments service"],
        "disqualifiers": ["No Kubernetes"],
        "skipped": False,
        "skip_reason": None,
    }
    record.update(over)
    return record


def _match(db: Database, run_id: str, job_id: str, *, score=78, shortlisted=False,
           skipped=False, reason=None, rerank=7.19, **over) -> None:
    raw = _raw(job_id, relevance_score=score, skipped=skipped,
               skip_reason=reason, rerank_score=rerank, **over)
    db.upsert_match(
        run_id, job_id, raw["board_token"], raw["title"], score, shortlisted,
        skipped, reason, run_id, "2026-09-09T07:00:00+00:00", json.dumps(raw),
        encoder_score=raw["encoder_score"], rerank_score=rerank,
    )


def _apply(db: Database, job_id: str, status: str = "submitted") -> None:
    db.record_applied(
        job_id, "acme", "Backend Engineer", f"https://example.com/jobs/{job_id}",
        "2026-09-09T08:00:00+00:00", status, None, None,
    )


def _populated(tmp_path) -> Database:
    """One run, holding one of everything the partition has to tell apart.

    j5 is the case a `skip_reason = 'duplicate_of_cluster'` test misses: a sibling
    whose representative failed inherits *that* reason, so it says
    `backend_unavailable` and is a judged job all the same.
    """
    db = _db(tmp_path)
    db.record_company(RUN, "acme", "greenhouse", "ok", 5, 0.2, None)
    db.insert_jobs(RUN, [_job(f"j{n}") for n in range(1, 6)])

    _match(db, RUN, "j1", score=82, shortlisted=True, rerank=8.10)
    _match(db, RUN, "j2", score=71, rerank=7.19)
    _match(db, RUN, "j3", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=1.02)
    _match(db, RUN, "j4", score=82, skipped=True, reason="duplicate_of_cluster",
           rerank=8.10, cluster_representative="j1")
    _match(db, RUN, "j5", score=0, skipped=True, reason="backend_unavailable",
           rerank=6.00, cluster_representative="j2")
    _apply(db, "j1")
    return db


def _snapshot(db: Database, run_id: str | None = RUN, **over) -> dict:
    snap = data.overview_snapshot(db, run_id, run={
        "started_at": "2026-09-09T06:51:12+00:00",
        "finished_at": "2026-09-09T07:09:54+00:00",
        "usage": {"cost_usd": 1.87},
        "in_progress": False,
    })
    snap.update(over)
    return snap


# --- the three lists partition the jobs ---------------------------------------


def test_an_applied_job_appears_once_and_only_in_the_applied_list(tmp_path):
    """The page's one real job of work. A job in both accordions would double the
    only list on it that says what is left to do."""
    snap = _snapshot(_populated(tmp_path))

    assert [r["job_id"] for r in snap["applied"]] == ["j1"]
    assert "j1" not in [r["job_id"] for r in snap["scored"]]
    assert "j1" not in [r["job_id"] for r in snap["tail"]]


def test_a_cluster_sibling_is_scored_not_tail(tmp_path):
    """It was judged, just once for the whole cluster — the same rule
    `data._never_scored` applies, reached here through SQL."""
    snap = _snapshot(_populated(tmp_path))
    assert "j4" in [r["job_id"] for r in snap["scored"]]
    assert "j4" not in [r["job_id"] for r in snap["tail"]]


def test_the_tail_holds_only_jobs_no_one_judged(tmp_path):
    snap = _snapshot(_populated(tmp_path))
    assert [r["job_id"] for r in snap["tail"]] == ["j3"]


def test_the_tail_is_ordered_by_the_cross_encoder(tmp_path):
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=4.40)
    db.insert_jobs(RUN, [_job("j6")])

    snap = _snapshot(db)
    assert [r["rerank_score"] for r in snap["tail"]] == [4.40, 1.02]


def test_the_sql_judged_predicate_matches_never_scored(tmp_path):
    """`db._judged_sql` has to reach inside `raw_json` for `cluster_representative`,
    because no column carries it. Keying off `skip_reason` instead looks equivalent
    and is not — a sibling inherits its representative's reason — and when the two
    disagree a judged job silently drops into the never-scored table."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="llm_call_cap_reached")
    _match(db, RUN, "j7", score=0, skipped=True, reason="api_error")
    db.insert_jobs(RUN, [_job("j6"), _job("j7")])

    by_sql = {r["job_id"] for r in db.load_lifetime_matches(judged=True, limit=100)}
    by_python = {
        r["job_id"] for r in db.load_all_matches(RUN) if not data._never_scored(r)
    }
    assert by_sql == by_python


# --- the four numbers ---------------------------------------------------------


def test_filtered_counts_survivors_of_the_cross_encoder(tmp_path):
    """Not "reached the reranker" and not "thrown away" — what cleared `min_score`
    and was therefore eligible for an LLM call. Cluster siblings are out, exactly as
    the matcher's own `stages["above_cutoff"]` leaves them out."""
    counts = _populated(tmp_path).overview_counts(RUN)
    assert counts == {"seen": 5, "filtered": 2, "shortlisted": 1, "applied": 1}


def test_lifetime_counts_a_resurfaced_job_once(tmp_path):
    """Unlike the old dashboard's totals, which sum per-run counts and say so."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j1")])
    _match(db, OLDER, "j1", score=64)

    assert db.overview_counts(None)["seen"] == 5
    assert db.overview_counts(OLDER)["seen"] == 1


def test_the_lifetime_loader_keeps_a_jobs_best_showing(tmp_path):
    """One row per job_id, and the row is the sweep where it did best — a job that
    scored 82 once and 64 later is an 82."""
    db = _populated(tmp_path)
    db.insert_jobs(OLDER, [_job("j2")])
    _match(db, OLDER, "j2", score=91, rerank=9.50)

    rows = db.load_lifetime_matches(judged=True, limit=100)
    j2 = [r for r in rows if r["job_id"] == "j2"]
    assert len(j2) == 1
    assert j2[0]["relevance_score"] == 91


# --- the two extra tiles ------------------------------------------------------


def test_a_finished_sweep_shows_a_fixed_duration(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert "18m 42s" in html
    assert ">Took<" in html


def test_a_live_sweep_counts_up_from_its_start(tmp_path):
    """`finished_at` is only written when the pipeline's run row lands, so a running
    sweep has none and `now` stands in."""
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert duration(started.isoformat(), None).startswith("5m")


def test_an_unmeasured_sweep_shows_a_dash_not_zero_dollars(tmp_path):
    """`usage` is written once, at the matcher's finalise. Printing $0.00 mid-sweep
    would claim the run was free — the same rule the CSV's blank llm_score follows."""
    snap = _snapshot(_populated(tmp_path), usage=None)
    html = overview.build(snap, RUN)
    assert "$0.00" not in html
    assert "$1.87" in overview.build(_snapshot(_populated(tmp_path)), RUN)


def test_the_lifetime_page_has_no_duration_or_cost(tmp_path):
    """Neither is a fact about an install — the tiles belong to one sweep."""
    html = overview.build(data.overview_snapshot(_populated(tmp_path), None), None)
    assert ">Took<" not in html
    assert "Est. cost" not in html


# --- the page itself ----------------------------------------------------------


def test_both_accordions_render_with_their_counts(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.count('<details class="acc">') == 2
    assert "Applied" in html and "Scored, not applied" in html
    # One applied; three scored-not-applied — j2 and the two siblings j4 and j5.
    assert '<span class="n">1</span>' in html
    assert '<span class="n">3</span>' in html


def test_the_only_prose_is_the_judges_own_reasoning(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert "Strong Python and service work." in html
    assert "Owned a payments service" in html
    assert "No Kubernetes" in html


def test_a_never_scored_job_carries_no_score_key_at_all(tmp_path):
    """Not a blank, not a zero — absent, so no renderer can print one. Budget drops
    are written with a placeholder `relevance_score=0`, and printing that reads as a
    verdict the model never gave."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    payload = json.loads(
        html.split('id="ov-tail-data">')[1].split("</script>")[0].replace("<\\/", "</")
    )
    assert payload and "relevance_score" not in payload[0]
    assert set(payload[0]) == {"t", "c", "l", "x", "u"}


def test_a_failed_representatives_sibling_shows_no_score(tmp_path):
    """j5 inherited `backend_unavailable` and a placeholder `relevance_score` of 0.
    It belongs in the scored list — it had its shot at the judge — but printing that
    0 beside a real job claims a verdict nothing produced. Six of these rendered a
    bold "0" on the first run against live data."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    entry = [
        block for block in html.split('<details class="job">')
        if "Backend unavailable" in block
    ]
    assert len(entry) == 1
    assert '<span class="job-s">—</span>' in entry[0]
    assert '<span class="job-s">0</span>' not in html


def test_a_duplicate_sibling_keeps_the_score_it_inherited(tmp_path):
    """The other kind of sibling: its representative answered, and the verdict is
    that answer. One LLM call, copied — not an absent one."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    # j4 in the scored list, carrying j1's 82 without j1's shortlist flag; j1 itself
    # is over in the applied list, flagged.
    assert '<span class="job-s">82</span>' in html
    assert '<span class="job-s hit">82</span>' in html


def test_the_page_is_a_complete_local_document(tmp_path):
    """Both overviews are opened over `file://` and never published, so unlike the
    matching report they bring their own skeleton."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.startswith("<!doctype html>")
    assert "<body>" in html


def test_it_reloads_only_while_a_sweep_is_running(tmp_path):
    db = _populated(tmp_path)
    assert 'http-equiv="refresh"' not in overview.build(_snapshot(db), RUN)
    assert 'http-equiv="refresh"' in overview.build(_snapshot(db, live=True), RUN)


def test_the_accordion_css_actually_reaches_the_page(tmp_path):
    """`document()` grew an `extra_css` parameter for this; without it the rules are
    written and never delivered, which is what happened to the dashboard's pills."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert '.acc[open] > summary::after { content: "\\00d7"; }' in html
    assert "summary::-webkit-details-marker" in html


def test_html_in_a_job_title_is_escaped(tmp_path):
    db = _populated(tmp_path)
    _match(db, RUN, "j1", score=82, shortlisted=True,
           title="<script>alert(1)</script>")
    html = overview.build(_snapshot(db), RUN)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_title_cannot_close_the_json_block(tmp_path):
    db = _populated(tmp_path)
    _match(db, RUN, "j3", score=0, skipped=True, reason="rerank_below_cutoff",
           title="</script> and then some")
    html = overview.build(_snapshot(db), RUN)
    body = html.split('id="ov-tail-data">')[1].split("</script>")[0]
    assert "and then some" in body


# --- placement and wiring -----------------------------------------------------


def test_the_two_scopes_land_in_the_two_places(tmp_path):
    """Lifetime at the results root, per-sweep beside that run's CSVs. Neither may
    be resolved against the working directory — a plugin's cwd is whatever project
    the session was launched from."""
    import hireshire.reporting as reporting
    from hireshire import paths

    targets = reporting.report_paths(tmp_path, RUN)
    assert targets["overview"].parent == paths.results_root()
    assert targets["overview"].name == "overview.html"
    assert targets["run_overview"].parent == tmp_path
    assert targets["run_overview"].name == f"{RUN}_overview.html"


def test_a_refresh_writes_both_scopes(tmp_path, monkeypatch):
    """The wiring, end to end: one `refresh` call has to produce both files, and the
    lifetime one must not be skipped on the final write however recently it ran."""
    import hireshire.reporting as reporting

    db = _populated(tmp_path)
    root = tmp_path / "results"
    run_dir = root / RUN
    monkeypatch.setattr(reporting.paths, "results_root", lambda: root)
    monkeypatch.setattr(reporting, "get_db", lambda: db)
    monkeypatch.setattr(reporting, "_last_refresh", 0.0)
    # As if the lifetime page had just been written: only `final` may override it.
    monkeypatch.setattr(reporting, "_last_lifetime", __import__("time").monotonic())

    reporting.refresh(RUN, run_dir, RUN, final=True)

    assert (root / "overview.html").exists()
    assert (run_dir / f"{RUN}_overview.html").exists()


# --- the failure contract -----------------------------------------------------


def test_losing_the_page_is_reported_as_none_not_raised(tmp_path):
    """A report is a diagnostic. Losing one must never take down a sweep whose CSV,
    JSON and database rows are already safe."""
    blocked = tmp_path / "file.txt"
    blocked.write_text("not a directory", encoding="utf-8")
    assert overview.write(_snapshot(_populated(tmp_path)), blocked / "overview.html") is None


def test_a_broken_snapshot_never_takes_down_the_run(tmp_path):
    assert overview.write({}, tmp_path / "overview.html") is None


def test_an_empty_install_still_renders(tmp_path):
    """Nothing swept, nothing scored, nothing applied — the page a first-time user
    sees before their first sweep finishes."""
    html = overview.build(data.overview_snapshot(_db(tmp_path), None), None)
    assert "Nothing yet." in html
    assert "ov-tail-data" not in html
