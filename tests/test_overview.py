"""The minimal overview page — its four sections, and its two scopes.

The page's whole promise is that a job appears in exactly one of its four lists,
and that a number on it means what its label says. Most of what follows pins those
two claims, because both are invisible when they break: a job showing up twice
still renders, and a count that quietly means something else still prints.

The tiles and the sections deliberately do *not* agree: the tiles are a cumulative
funnel, the sections a partition. Tests that look like they contradict each other on
that point are testing the two different things.
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


def _job_block(html: str, job_id: str) -> str:
    """The markup of one job entry, anchored on its id.

    Closed at the first `</details>` because nothing inside a `.job-body` is a
    `<details>`. This exists because the obvious version did not work: splitting on
    a bare `<details class="job">` never matches, since the real tag carries an id,
    so the "block" came back as the whole document and every assertion against it
    passed whatever the entry itself actually said.
    """
    at = html.index(f'id="j:{job_id}"')
    return html[at:html.index("</details>", at)]


def _snapshot(db: Database, run_id: str | None = RUN, **over) -> dict:
    snap = data.overview_snapshot(db, run_id, run={
        "started_at": "2026-09-09T06:51:12+00:00",
        "finished_at": "2026-09-09T07:09:54+00:00",
        "usage": {"cost_usd": 1.87},
        "in_progress": False,
    })
    snap.update(over)
    return snap


# --- the four lists partition the jobs -----------------------------------------

_SECTIONS = ("applied", "shortlisted", "filtered", "seen")


def _section_of(snap: dict, job_id: str) -> list[str]:
    """Every section holding this job. The point is that it is never more than one."""
    return [s for s in _SECTIONS if job_id in [r["job_id"] for r in snap[s]]]


def test_every_job_appears_in_exactly_one_section(tmp_path):
    """The page's one real job of work. A job in two sections would double the only
    list on it that says what is left to do."""
    snap = _snapshot(_populated(tmp_path))

    assert _section_of(snap, "j1") == ["applied"]
    for job_id in ("j2", "j3", "j4", "j5"):
        assert len(_section_of(snap, job_id)) == 1, job_id


def test_a_cluster_sibling_follows_its_verdict_not_the_tail(tmp_path):
    """It was judged, just once for the whole cluster — the same rule
    `data._never_scored` applies, reached here through SQL. j4 carries j1's 82
    without j1's shortlist flag, so it belongs with the judged-but-not-picked."""
    snap = _snapshot(_populated(tmp_path))
    assert _section_of(snap, "j4") == ["filtered"]


def test_the_last_section_holds_what_a_free_gate_killed(tmp_path):
    """j3 is `rerank_below_cutoff` — a verdict from the cross-encoder, which costs
    nothing to run. That is what separates the last section from the one above it,
    where the rows cleared both free gates and merely never got a call."""
    snap = _snapshot(_populated(tmp_path))
    assert [r["job_id"] for r in snap["seen"]] == ["j3"]


def test_a_call_cap_drop_is_filtered_not_seen(tmp_path):
    """The distinction the last two sections turn on. A cap drop cleared every free
    gate and ran out of budget — it is still a relevant job and comes back next
    sweep — so it sits with "yet to be scored", not with the gate casualties."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=4.40)
    db.insert_jobs(RUN, [_job("j6")])

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["filtered"]
    assert [r["job_id"] for r in snap["seen"]] == ["j3"]


def test_a_yoe_drop_falls_to_the_last_section(tmp_path):
    """The other free-gate verdict, and the one this page used to count as relevant.
    Same description, same `candidate_years`, same answer — a verdict, not a
    deferral."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="yoe_below_requirement",
           rerank=6.80)
    db.insert_jobs(RUN, [_job("j6")])

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["seen"]


def test_the_last_section_is_ordered_by_the_cross_encoder(tmp_path):
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=2.40)
    db.insert_jobs(RUN, [_job("j6")])

    snap = _snapshot(db)
    assert [r["rerank_score"] for r in snap["seen"]] == [2.40, 1.02]


def test_a_title_gate_job_reaches_the_page_with_no_score(tmp_path):
    """The rejections `matcher.py` deliberately keeps out of `matches` — there can be
    tens of thousands a run. They exist only in `jobs`, so `load_unmatched_jobs` is
    the only way onto the page, and they sort last because they have no score at
    all."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j9", title="Barista")])

    snap = _snapshot(db)
    assert _section_of(snap, "j9") == ["seen"]
    # After the rows that do carry a logit, never before them.
    assert [r["job_id"] for r in snap["seen"]] == ["j3", "j9"]
    assert snap["seen"][-1].get("rerank_score") is None


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


def test_relevant_counts_survivors_of_every_free_gate(tmp_path):
    """Not "reached the reranker" and not "thrown away" — what cleared both LLM-free
    gates and was therefore worth a call. Cluster siblings are out, as the matcher's
    own `stages["above_cutoff"]` leaves them out."""
    counts = _populated(tmp_path).overview_counts(RUN)
    assert counts == {"seen": 5, "relevant": 2, "shortlisted": 1, "applied": 1}


def test_a_yoe_drop_is_not_a_relevant_job(tmp_path):
    """The tile's one semantic change. The YoE gate runs *after* the `min_score`
    cutoff, so its casualties used to be counted as relevant — a job the resume
    cannot qualify for, printed as though it were still in the running.

    Note this deliberately diverges from the matcher's own `stages["above_cutoff"]`,
    which counts YoE drops in on purpose. The tile and that console line answer
    different questions."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="yoe_below_requirement",
           rerank=6.80)
    db.insert_jobs(RUN, [_job("j6")])

    counts = db.overview_counts(RUN)
    assert counts["seen"] == 6
    assert counts["relevant"] == 2


def test_a_call_cap_drop_is_still_a_relevant_job(tmp_path):
    """The other side of that line, and the reason the rule is a list of two reasons
    rather than "anything skipped". A cap drop is a deferral, not a verdict."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=4.40)
    db.insert_jobs(RUN, [_job("j6")])

    assert db.overview_counts(RUN)["relevant"] == 3


def test_lifetime_counts_a_resurfaced_job_once(tmp_path):
    """`COUNT(DISTINCT job_id)`, so a job that came back in a later sweep is one
    job — not one per sweep that saw it."""
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
    """Neither is a fact about an install — the tiles belong to one sweep. They are
    the *only* difference between the two scopes; everything else on the lifetime
    page is the same markup fed different data."""
    html = overview.build(data.overview_snapshot(_populated(tmp_path), None), None)
    assert ">Took<" not in html
    assert "Est. cost" not in html


def test_both_scopes_carry_the_same_header(tmp_path):
    db = _populated(tmp_path)
    per_run = overview.build(_snapshot(db), RUN)
    lifetime = overview.build(data.overview_snapshot(db, None), None)

    for html in (per_run, lifetime):
        assert "<span>HireShire</span>" in html
        assert "<h1>Control room</h1>" in html
        # One line of instruction, and it sits between the tiles and the first
        # section. This ordering is also why no filter chrome may be hoisted out of
        # `.acc-body`: the first `class="acc"` has to stay the first section's tag.
        assert html.count('<p class="hint">') == 1
        assert html.index('class="stats"') < html.index('class="hint"') < html.index('class="acc"')

    # The scope is the eyebrow's second word, and it is what tells the two apart.
    assert "<span>All sweeps</span>" in lifetime
    assert f"<span>Run {RUN}</span>" in per_run


# --- the page itself ----------------------------------------------------------


def test_all_four_accordions_render_with_their_counts(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    # `class` stays the first attribute of every `.acc` — this count is why. The
    # ids below are the assertion that actually means "four sections".
    assert html.count('<details class="acc"') == 4
    for key in ("applied", "shortlisted", "filtered", "seen"):
        assert f'id="acc:{key}"' in html
    for label in ("Jobs Applied", "Jobs Shortlisted (to be applied)",
                  "Jobs Filtered (yet to be scored or not picked)",
                  "Total Jobs Seen"):
        assert label in html
    assert "<h2" not in html
    # One applied (j1); none shortlisted-but-unapplied; three filtered — j2 and the
    # two siblings j4 and j5; one seen — j3, below the cutoff.
    assert '<span class="n">1</span>' in html
    assert '<span class="n">3</span>' in html


def test_the_tiles_say_what_they_count(tmp_path):
    """The page's other promise. Nothing else asserts these strings, and a label that
    quietly means something else still prints."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    for label in ("Jobs in scope", "Relevant jobs", "Jobs shortlisted", "Jobs applied"):
        assert f">{label}</span>" in html
    # The qualifier that does not fit in a tile lives in the tooltip and the hint.
    assert 'title="Total jobs in the given location and time window"' in html
    assert "In scope means matching your location" in html


def test_the_only_prose_is_the_judges_own_reasoning(tmp_path):
    """Also the assertion that fails the moment anyone moves the three rendered
    lists to a JSON payload: prose reachable only from inside a script block is not
    on the page in any sense a reader would recognise."""
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
    # The column still exists, so all four sections carry the same six — rendered
    # as a literal dash by a script that has no number to print. An absent key and
    # a printed dash are not the same thing; only the key would invite a verdict.
    assert "<th>LLM</th>" in html
    assert "class='numeric blank'>—<" in html


def test_a_failed_representatives_sibling_shows_no_score(tmp_path):
    """j5 inherited `backend_unavailable` and a placeholder `relevance_score` of 0.
    It belongs in the scored list — it had its shot at the judge — but printing that
    0 beside a real job claims a verdict nothing produced. Six of these rendered a
    bold "0" on the first run against live data."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    block = _job_block(html, "j5")          # raises outright if j5 never rendered
    assert "Backend unavailable" in block   # the inherited reason, on its sub-line
    assert '<span class="job-s">—</span>' in block
    assert '<span class="job-s">0</span>' not in html


def test_a_duplicate_sibling_keeps_the_score_it_inherited(tmp_path):
    """The other kind of sibling: its representative answered, and the verdict is
    that answer. One LLM call, copied — not an absent one."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    # j4 in the scored list, carrying j1's 82 without j1's shortlist flag; j1 itself
    # is over in the applied list, flagged.
    assert '<span class="job-s">82</span>' in _job_block(html, "j4")
    assert '<span class="job-s hit">82</span>' in _job_block(html, "j1")


def test_open_accordions_survive_the_meta_refresh(tmp_path):
    """A live page reloads every 15 seconds. Every `<details>` therefore needs a
    stable id and the script that puts the open ones back, or the refresh shuts the
    job whose rationale the reader is halfway through."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    for acc in ("applied", "shortlisted", "filtered", "seen"):
        assert f'id="acc:{acc}"' in html
    assert 'id="j:j2"' in html
    assert "hs-overview-open" in html


def test_the_state_script_survives_storage_being_unavailable(tmp_path):
    """A `file://` origin can be opaque enough that touching sessionStorage throws.
    A report degrades; it does not die."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    state = html.split('KEY = "hs-overview-open"')[1]
    assert "try { store = window.sessionStorage" in state
    assert "catch (e) { return; }" in state


def test_the_page_is_a_complete_local_document(tmp_path):
    """Both overviews are opened over `file://` and never published, which is what
    licenses the meta refresh — so they bring their own skeleton."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.startswith("<!doctype html>")
    assert "<body>" in html


def test_it_reloads_only_while_a_sweep_is_running(tmp_path):
    db = _populated(tmp_path)
    assert 'http-equiv="refresh"' not in overview.build(_snapshot(db), RUN)
    assert 'http-equiv="refresh"' in overview.build(_snapshot(db, live=True), RUN)


def test_the_accordion_css_actually_reaches_the_page(tmp_path):
    """`document()` grew an `extra_css` parameter for this; without it the rules are
    written and never delivered, which is what happened to another page's CSS."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert '.acc[open] > summary::after { content: "\\00d7"; }' in html
    assert "summary::-webkit-details-marker" in html
    # The column header is a sticky grid row, not a `<thead>`, so BASE_CSS's
    # `.scroll-y thead th` never reaches it and it needs a rule of its own.
    assert ".job-head, .job > summary {" in html
    assert "sticky" in html.split(".job-head {")[1].split("}")[0]


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


# --- every section reads the same way ------------------------------------------


def _all_four(tmp_path) -> dict:
    """A snapshot with all four sections holding something.

    `_populated` leaves Jobs Shortlisted empty on purpose — its one shortlisted job
    is also the applied one, which is what the partition tests need — so a test
    about the *layout* of four sections has to add the missing row itself.
    """
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=79, shortlisted=True, rerank=7.80)
    db.insert_jobs(RUN, [_job("j6")])
    return _snapshot(db)


def test_each_job_list_is_filterable_and_bounded(tmp_path):
    """The whole point of the layout. Three of these sections used to be unbounded
    flat lists beside one that was not, which made a sweep with 300 filtered jobs a
    wall. The last section is not a `.filterable` — it keeps its own script, because
    it builds its rows from a payload and pages them in on scroll."""
    html = overview.build(_all_four(tmp_path), RUN)
    assert html.count('class="filterable"') == 3
    assert html.count('class="job-head"') == 3
    for key in ("applied", "shortlisted", "filtered"):
        assert f'id="rows:{key}"' in html
    # Every bounded list keeps its scroll position across the refresh; the paginated
    # one deliberately does not, since only its first page exists on load.
    assert html.count('data-keep-scroll="1"') == 3


def test_an_empty_section_gets_no_filter_chrome(tmp_path):
    """A filter over no rows and a header over no data are both noise, and an empty
    section is what a first-time user sees before their first sweep finishes."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.count('class="filterable"') == 2      # applied and filtered only
    assert "Nothing yet." in html


def test_all_four_sections_carry_the_same_six_columns(tmp_path):
    """Three `<span>` headers and one `<thead>`, but the same labels in the same
    order — a reader scanning down must not have the columns move under them."""
    html = overview.build(_all_four(tmp_path), RUN)
    for label in ("#", "Title", "Company", "Location", "LLM", "Cross"):
        assert html.count(f">{label}<") == 4, label


def test_a_row_carries_a_lowercased_filter_haystack(tmp_path):
    """Lowercased server-side so filtering is one `indexOf` per row per keystroke,
    and holding every column the filter claims to search."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    hay = _job_block(html, "j2").split('data-hay="')[1].split('"')[0]
    assert hay == hay.lower()
    for part in ("backend engineer", "acme", "remote"):
        assert part in hay


def test_the_rank_is_stamped_server_side(tmp_path):
    """The same rule the paginated section follows: row 214 stays row 214 rather
    than becoming "the third result for nurse"."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert '<span class="job-i">1</span>' in _job_block(html, "j1")


def test_a_drop_reason_survives_as_a_subline_not_a_column(tmp_path):
    """The labels are whole sentences, so they cannot be a nowrap column — and Jobs
    Filtered is the one section whose entire question is *why*."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=4.40)
    db.insert_jobs(RUN, [_job("j6")])

    block = _job_block(overview.build(_snapshot(db), RUN), "j6")
    assert '<span class="job-sub">Reached the run' in block


def test_the_applied_stamp_stays_a_subline(tmp_path):
    """Status plus timestamp, which is two facts and not a column either."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert '<span class="job-sub">submitted ' in _job_block(html, "j1")


def test_the_cross_encoder_column_reads_the_same_everywhere(tmp_path):
    """Two places, one format. The rendered rows and the payload's `x` key both
    print two decimals, and both print an em dash where no logit was taken."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert '<span class="job-x">8.10</span>' in _job_block(html, "j1")
    assert overview._cross({"rerank_score": None}) == "—"


def test_one_filter_script_serves_every_rendered_list(tmp_path):
    """Wired by class, so a fourth filterable section is markup and no script. The
    id-driven copy belongs to the paginated section alone."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.count('querySelectorAll(".filterable")') == 1
    assert html.count('getElementById("ov-filter")') == 1


def test_the_open_set_is_pruned_of_ids_that_no_longer_resolve(tmp_path):
    """A row leaves the page for good when it drops out of `MAX_JOB_ROWS` or a later
    sweep moves it to another section. Without the prune its id sits in
    sessionStorage for the rest of the session."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    state = html.split('KEY = "hs-overview-open"')[1]
    assert "live.push" in state
    assert "if (live.length !== open.length) write(KEY, live);" in state


def test_scroll_position_inside_a_list_survives_the_refresh(tmp_path):
    """A loss the bounded box introduced and has to pay for: the browser restores
    the document's scroll across a meta refresh but never an `overflow: auto`
    div's, so a reader 200 rows down would be snapped to the top every 15
    seconds."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    state = html.split('KEY = "hs-overview-open"')[1]
    assert "hs-overview-scroll" in html
    assert 'querySelectorAll("[data-keep-scroll]")' in state
    # Debounced: `scroll` fires per frame and `setItem` is a synchronous write.
    assert "clearTimeout(pending)" in state


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
    # No filter box over zero rows, and no script shipped to wire one.
    assert "filterable" not in html
