"""The minimal overview page — its five sections, and its two scopes.

The page's whole promise is that a job appears in exactly one of its five lists,
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

from hireshire.applier import reasons, worker
from hireshire.models.job import Job, Location
from hireshire import run_ids
from hireshire.reporting import data, overview
from hireshire.reporting.render import duration, e
from hireshire.storage.db import DECLINED_BY_USER, PHASE_PIPELINE, Database

RUN = "2026-09-09T06-51-12Z"
OLDER = "2026-09-01T00-00-00Z"


def _run_id_at_local(year, month, day, hour) -> str:
    """The run id of a sweep that started at a given *local* wall-clock time.

    Built this way round rather than written out because the day a sweep belongs to is
    its local date, and a literal UTC run id would straddle local midnight on some
    machines and not others — so a hand-written pair would be two sweeps on one day in
    London and two different days in Auckland.
    """
    local = datetime(year, month, day, hour, 0, 0)   # naive: read as local
    return local.astimezone(timezone.utc).strftime(run_ids.RUN_ID_FMT)


# Two sweeps on one local day, a third still running on it, and one two days later.
DAY = "2026-09-09"
DAY_A = _run_id_at_local(2026, 9, 9, 8)
DAY_B = _run_id_at_local(2026, 9, 9, 18)
DAY_C = _run_id_at_local(2026, 9, 9, 22)
NEXT_DAY = _run_id_at_local(2026, 9, 11, 8)


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
           skipped=False, reason=None, rerank=7.19,
           scored_at="2026-09-09T07:00:00+00:00", **over) -> None:
    """One `matches` row. `scored_at` is a parameter because the lifetime loader
    chooses a job's canonical row by it — rows written at the same instant leave it
    nothing to choose between."""
    raw = _raw(job_id, relevance_score=score, skipped=skipped,
               skip_reason=reason, rerank_score=rerank, **over)
    db.upsert_match(
        run_id, job_id, raw["board_token"], raw["title"], score, shortlisted,
        skipped, reason, run_id, scored_at, json.dumps(raw),
        encoder_score=raw["encoder_score"], rerank_score=rerank,
    )


def _apply(db: Database, job_id: str, status: str = "submitted",
           error: str | None = None, from_backlog: bool = False) -> None:
    db.record_applied(
        job_id, "acme", "Backend Engineer", f"https://example.com/jobs/{job_id}",
        "2026-09-09T08:00:00+00:00", status, None, error,
        from_backlog=from_backlog,
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


def _day_db(tmp_path) -> Database:
    """One job, judged by two sweeps on `DAY` and re-judged two days later.

    That shape is the whole point: `matches` is keyed `(run_id, job_id)`, so j1 holds
    three rows, and each scope has to pick the right one. The `scored_at` values are
    distinct because the canonical row is chosen by `MAX(scored_at)` — rows written at
    the same instant leave it nothing to choose between.
    """
    db = _db(tmp_path)
    plan = (
        (DAY_A, 88, True, None, "2026-09-09T09:00:00+00:00"),
        (DAY_B, 88, True, None, "2026-09-09T19:00:00+00:00"),
        (NEXT_DAY, 10, False, "rerank_below_cutoff", "2026-09-11T09:00:00+00:00"),
    )
    for run_id, score, shortlisted, reason, scored_at in plan:
        db.start_progress(run_id, False)
        db.insert_jobs(run_id, [_job("j1")])
        _match(db, run_id, "j1", score=score, shortlisted=shortlisted,
               reason=reason, scored_at=scored_at)

    # Pipeline rows for the two that finished: ten minutes and twenty.
    db.finalise_run(DAY_A, PHASE_PIPELINE, "2026-09-09T08:00:00+00:00",
                    "2026-09-09T08:10:00+00:00", {"completed": True})
    db.finalise_run(DAY_B, PHASE_PIPELINE, "2026-09-09T18:00:00+00:00",
                    "2026-09-09T18:20:00+00:00", {"completed": True})
    return db


# --- the four lists partition the jobs -----------------------------------------

_SECTIONS = ("applied", "attention", "shortlisted", "filtered", "seen")


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


def test_an_application_that_stopped_short_needs_attention(tmp_path):
    """Known issue A4. An `error` row used to be listed under Jobs Applied and counted
    in its tile, beside real submissions, so a form stuck on a question looked like a
    finished application and the user never learned they had to act."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    db.insert_jobs(RUN, [_job("j6")])
    # Paragraph-length, as rows recorded before the one-line rule are, with a "U.S."
    # in it — a sentence split cut the first real render there — and matching no
    # fixed label, so the page has to clip it rather than replace it.
    long = ("Blocked after the phone field refused a U.S. number twice, and the retry "
            "was then stopped by a permission prompt, so the filled form was left unsent "
            "and needs the user to review it and submit it themselves.")
    _apply(db, "j6", "error", long)

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["attention"]
    assert _section_of(snap, "j1") == ["applied"]
    assert db.overview_counts(RUN)["applied"] == 1
    assert db.overview_counts(None)["applied"] == 1

    html = overview.build(snap, RUN)
    block = _job_block(html, "j6")
    # One line, cut at a word and past the "U.S."; the whole message is the tooltip.
    line, full = overview._attention_reason({"applied_error": long})
    assert full == long and line.endswith("…") and len(line) <= 141
    assert "U.S. number" in line
    assert long.startswith(line[:-1]) and long[len(line) - 1] == " "
    assert line in block
    assert f'title="{long}"' in block
    assert html.index('id="acc:applied"') < html.index('id="acc:attention"') \
        < html.index('id="acc:shortlisted"')


def test_an_excluded_employer_needs_attention_not_a_shortlist_slot(tmp_path):
    """`exclude_companies` names portals that need an account login, so no sweep can
    ever apply to them. Those jobs used to sit under Jobs Shortlisted, indistinguishable
    from work the applier had simply not reached yet. The applier records an `excluded`
    row instead, which lands here — and must not move the Jobs applied tile, which
    counts submissions."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j7")])
    _match(db, RUN, "j7", score=84, shortlisted=True, rerank=8.40)
    # The text older installs stored. The page prints the fixed label instead, and
    # keeps the stored text whole as the tooltip.
    old = ("Requires human verification — this employer's portal needs an account "
           "login, so apply to it yourself.")
    _apply(db, "j7", "excluded", old)

    snap = _snapshot(db)
    assert _section_of(snap, "j7") == ["attention"]
    assert db.overview_counts(RUN)["applied"] == 1        # j1, the real submission
    block = _job_block(overview.build(snap, RUN), "j7")
    assert f"{worker.EXCLUDED_REASON} · " in block
    # Escaped, because the stored text carries an apostrophe and the page escapes it.
    assert f'title="{e(old)}"' in block


def test_a_job_the_backlog_gave_up_on_needs_attention(tmp_path):
    """The applier retries a job whose sessions fail to launch for `backlog_hours` and
    then stops. That used to be silent — the job kept its shortlist row and no `applied`
    row, so it sat under Jobs Shortlisted for good, reading as work still to come. The
    `expired` row lands here instead, and like every other non-submission it must leave
    the Jobs applied tile alone."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j8")])
    _match(db, RUN, "j8", score=86, shortlisted=True, rerank=8.60)
    reason = worker.expired_reason(72)
    _apply(db, "j8", "expired", reason)

    snap = _snapshot(db)
    assert _section_of(snap, "j8") == ["attention"]
    assert db.overview_counts(RUN)["applied"] == 1        # j1, the real submission
    assert e(reason) in _job_block(overview.build(snap, RUN), "j8")
    # One line, whole: nothing is clipped, so the tooltip is not needed.
    assert overview._attention_reason({"applied_error": reason}) == (reason, reason)


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


def test_a_location_skip_is_filtered_not_shortlisted_or_seen(tmp_path):
    """The applier's verdict, not the matcher's: the job was judged and shortlisted,
    and the posting page then turned out to state a location the user does not accept.
    It belongs with the jobs that cleared both free gates — it cleared them — so it is
    filtered, not in the last section, and it must leave Jobs Shortlisted, which is
    the section that says what the applier still has to do."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=79, shortlisted=False, reason="location_mismatch",
           rerank=7.50, applier_location="London, UK")
    db.insert_jobs(RUN, [_job("j6")])

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["filtered"]


def test_a_location_skip_keeps_its_score_and_its_reason(tmp_path):
    """The regression guard for the one trap in this change. `_job_entry` had a single
    `judged` tuple driving two different things — whether to print the score, and
    whether to print the reason label. Appending the new reason to it keeps the score
    and silently deletes the label, on the one section whose entire question is *why*
    a job is there. Both halves have to hold, so both are asserted."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=79, shortlisted=False, reason="location_mismatch",
           rerank=7.50, applier_location="London, UK")
    db.insert_jobs(RUN, [_job("j6")])

    block = _job_block(overview.build(_snapshot(db), RUN), "j6")
    assert '<span class="job-s">79</span>' in block, "a judged job keeps its score"
    assert "Outside your search locations" in block, "and says why it is here"
    assert 'page says &quot;London, UK&quot;' in block
    assert '<span class="job-s">—</span>' not in block


def test_a_location_skip_without_a_recorded_location_still_reads(tmp_path):
    """Rows written before the applier recorded the page's text, and any skip where it
    was unreadable. The label stands on its own; nothing renders an empty quotation."""
    db = _populated(tmp_path)
    _match(db, RUN, "j6", score=79, shortlisted=False, reason="location_mismatch",
           rerank=7.50)
    db.insert_jobs(RUN, [_job("j6")])

    block = _job_block(overview.build(_snapshot(db), RUN), "j6")
    assert "Outside your search locations" in block
    assert "page says" not in block


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

    with db._lock:
        by_sql = {
            r["job_id"] for r in db._conn.execute(
                f"SELECT m.job_id FROM matches m WHERE {db._judged_sql('m')}"
            )
        }
    by_python = {
        r["job_id"] for r in db.load_all_matches(RUN) if not data._never_scored(r)
    }
    assert by_sql == by_python

    # And the lifetime loader ranks on that same predicate: every judged job comes
    # before every never-scored one, whatever scores they carry.
    ranked = [not data._never_scored(r) for r in db.load_lifetime_matches(limit=100)]
    assert ranked == sorted(ranked, reverse=True)


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


def test_the_relevant_tile_drops_a_job_a_later_sweep_overturned(tmp_path):
    """The tile used to ask "did any row ever say so", which keeps a superseded
    reading alive for good: a job deferred on the call cap and later cut by the
    cross-encoder stayed relevant forever. It now reads the same canonical row the
    sections render. The per-run tile is unaffected — that sweep really did defer it,
    and `(run_id, job_id)` leaves it nothing to choose between."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j6")])
    _match(db, OLDER, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=2.94, scored_at="2026-09-01T07:00:00+00:00")
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=2.94)

    assert db.overview_counts(OLDER)["relevant"] == 1
    assert db.overview_counts(None)["relevant"] == 2       # j1 and j2, not j6


def test_the_lifetime_applier_bar_and_the_shortlist_tile_are_one_number(tmp_path):
    """The bar's denominator is every shortlisted representative the install has, which
    is what the tile counts. Two queries, so they agree only if they choose the same
    row per job."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j6")])
    _match(db, OLDER, "j6", score=81, shortlisted=True, rerank=8.10,
           scored_at="2026-09-01T07:00:00+00:00")
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=2.94)

    assert db.lifetime_progress()["shortlisted"] == \
        db.overview_counts(None)["shortlisted"]


def test_the_lifetime_loader_keeps_the_jobs_current_row(tmp_path):
    """One row per job_id, and the row is the newest — not the best it ever did.

    This reverses the original rule, and the reversal is the point. `matches` is keyed
    `(run_id, job_id)`, so a rescored job keeps its old row; "best" would let a reading
    a later sweep overturned outlive the one that replaced it."""
    db = _populated(tmp_path)          # j2 scored 71 in RUN
    db.insert_jobs(OLDER, [_job("j2")])
    _match(db, OLDER, "j2", score=91, rerank=9.50,
           scored_at="2026-09-01T07:00:00+00:00")

    j2 = [r for r in db.load_lifetime_matches(limit=100) if r["job_id"] == "j2"]
    assert len(j2) == 1
    assert j2[0]["relevance_score"] == 71


def test_a_rescored_job_is_listed_once_with_its_verdict(tmp_path):
    """The bug filed as issue R1. A cap drop is a deferral, so the job comes back and
    a later sweep writes it a second row. The page used to load its rows in two halves — those
    with a standing verdict and those without — each deduped only within itself, so a
    job holding one of each satisfied both and rendered twice, once under Shortlisted
    with its score and once under Jobs Filtered as "still eligible next sweep"."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j6")])
    _match(db, OLDER, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=8.30, scored_at="2026-09-01T07:00:00+00:00")
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=88, shortlisted=True, rerank=8.30)

    snap = _snapshot(db, None, live=False)
    assert _section_of(snap, "j6") == ["shortlisted"]
    assert [r["relevance_score"] for r in snap["shortlisted"]
            if r["job_id"] == "j6"] == [88]
    # And once in the markup: two entries would share a DOM id, which is what breaks
    # `_STATE_SCRIPT`'s restore across the meta refresh.
    assert overview.build(snap).count('id="j:j6"') == 1


def test_a_verdict_outranks_the_deferral_it_replaced(tmp_path):
    """The same stale row, one step further down the funnel. Both rows are unjudged
    here, so the old loader picked between them with `MAX(rerank_score)` — and the
    logit is deterministic, so the two tie and SQLite chose arbitrarily. That decided
    the job's label *and* its section: a cutoff verdict belongs in the last section,
    a cap drop in Jobs Filtered."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j6")])
    _match(db, OLDER, "j6", score=0, skipped=True, reason="llm_call_cap_reached",
           rerank=2.94, scored_at="2026-09-01T07:00:00+00:00")
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=2.94)

    snap = _snapshot(db, None, live=False)
    assert _section_of(snap, "j6") == ["seen"]
    assert [r["skip_reason"] for r in snap["seen"] if r["job_id"] == "j6"] == [
        "rerank_below_cutoff"
    ]


def test_a_failed_representatives_sibling_does_not_outrank_a_later_verdict(tmp_path):
    """Why the rule is "newest", not "the row with a verdict". `_judged_sql` is true of
    a cluster sibling whose representative *failed* — it carries the representative's
    error reason and a placeholder 0, with nothing behind it — so preferring a judged
    row would print that placeholder and bury the genuine cutoff verdict written
    later. Observed on a real install, on a job whose sibling row said
    `backend_unavailable`."""
    db = _populated(tmp_path)
    db.record_company(OLDER, "acme", "greenhouse", "ok", 1, 0.2, None)
    db.insert_jobs(OLDER, [_job("j6")])
    _match(db, OLDER, "j6", score=0, skipped=True, reason="backend_unavailable",
           rerank=2.89, cluster_representative="j2",
           scored_at="2026-09-01T07:00:00+00:00")
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=0, skipped=True, reason="rerank_below_cutoff",
           rerank=2.94)

    j6 = [r for r in db.load_lifetime_matches(limit=100) if r["job_id"] == "j6"]
    assert len(j6) == 1
    assert j6[0]["skip_reason"] == "rerank_below_cutoff"
    assert not j6[0].get("cluster_representative")


# --- the extra tile, and the one that was withdrawn ---------------------------


def test_a_finished_sweep_shows_a_fixed_duration(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert "18m 42s" in html
    assert ">Took<" in html


def test_a_live_sweep_counts_up_from_its_start(tmp_path):
    """`finished_at` is only written when the pipeline's run row lands, so a running
    sweep has none and `now` stands in."""
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert duration(started.isoformat(), None).startswith("5m")


def test_no_scope_prints_what_the_sweep_cost(tmp_path):
    """The `Est. cost` tile is off (`render.SHOW_COST`): the figure is a list-price
    estimate, not a bill. The snapshot still carries it on the per-run scope, so this
    checks the renderer and not just the absence of data. `tests/test_reporting.py`
    owns the switch itself."""
    per_run = overview.build(_snapshot(_populated(tmp_path)), RUN)
    lifetime = overview.build(data.overview_snapshot(_populated(tmp_path), None), None)
    for html in (per_run, lifetime):
        assert "Est. cost" not in html
        assert "$1.87" not in html
        assert "$0.00" not in html


def test_the_lifetime_page_has_no_duration(tmp_path):
    """How long it took is a fact about a sweep, so `Took` appears on the run page
    alone. That tile is the only thing the three scopes disagree about: the day page
    substitutes `Sweeps` + `Avg per sweep` for it, the lifetime page has neither
    because a mean over months is not a number anyone acts on, and everything else on
    both is the same markup fed different data."""
    html = overview.build(data.overview_snapshot(_populated(tmp_path), None), None)
    assert ">Took<" not in html
    assert ">Sweeps</span>" not in html and ">Avg per sweep</span>" not in html


def test_both_scopes_carry_the_same_header(tmp_path):
    db = _populated(tmp_path)
    per_run = overview.build(_snapshot(db), RUN)
    lifetime = overview.build(data.overview_snapshot(db, None), None)
    day = overview.build(data.overview_snapshot(db, None, run_ids=[RUN]), DAY)

    for html in (per_run, lifetime, day):
        assert "<h1>HireShire</h1>" in html
        assert 'class="eyebrow"' not in html
        # One line of instruction, and it sits between the tiles and the first
        # section. This ordering is also why no filter chrome may be hoisted out of
        # `.acc-body`: the first `class="acc"` has to stay the first section's tag.
        assert html.count('<p class="hint">') == 1
        assert html.index('class="stats"') < html.index('class="hint"') < html.index('class="acc"')

    # The subtitle names the scope, and it is what tells the three apart.
    assert "<span>Lifetime Dashboard</span>" in lifetime
    assert f"<span>Dashboard Run: {RUN}</span>" in per_run
    assert f"<span>Dashboard Day: {DAY}</span>" in day


# --- the day scope ------------------------------------------------------------


def test_a_day_counts_a_job_two_of_its_sweeps_saw_once(tmp_path):
    """`matches` is keyed `(run_id, job_id)`, so a job two of the day's sweeps judged
    holds two rows. The day page has to choose one, exactly as the lifetime page does,
    or the tiles read double and the list below prints the job twice."""
    db = _day_db(tmp_path)
    counts = db.overview_counts(None, run_ids=[DAY_A, DAY_B])
    snap = data.overview_snapshot(db, None, run_ids=[DAY_A, DAY_B])

    assert counts["seen"] == 1
    assert counts["relevant"] == 1
    assert counts["shortlisted"] == 1
    assert _section_of(snap, "j1") == ["shortlisted"]


def test_a_day_shows_the_verdict_that_day_reached(tmp_path):
    """The load-bearing half: the scope filter sits *inside* the canonical aggregate, so
    each job's chosen row is its newest **among that day's sweeps**. Outside, the
    aggregate would pick the job's all-time newest row and the outer `WHERE` would then
    discard the job entirely — a job this day judged would vanish from this day's page
    the moment a later sweep touched it."""
    db = _day_db(tmp_path)

    first = data.overview_snapshot(db, None, run_ids=[DAY_A, DAY_B])
    later = data.overview_snapshot(db, None, run_ids=[NEXT_DAY])
    lifetime = data.overview_snapshot(db, None)

    # The first day still holds its own verdict, and the job has not gone missing.
    assert _section_of(first, "j1") == ["shortlisted"]
    assert first["shortlisted"][0]["relevance_score"] == 88
    # The later sweep overturned it, and both the later day and lifetime say so.
    assert _section_of(later, "j1") == ["seen"]
    assert _section_of(lifetime, "j1") == ["seen"]


def test_the_day_page_trades_took_for_how_many_and_how_long(tmp_path):
    db = _day_db(tmp_path)
    html = overview.build(
        data.overview_snapshot(db, None, run_ids=[DAY_A, DAY_B]), DAY
    )

    assert ">Sweeps</span>" in html
    assert ">Avg per sweep</span>" in html
    assert ">Took<" not in html
    assert "Est. cost" not in html
    assert f"{overview.TITLE} — {DAY}" in html


def test_the_day_tiles_count_the_sweeps_and_average_the_finished_ones(tmp_path):
    """A sweep in flight has no pipeline `runs` row, so it counts in `Sweeps` and not
    in the average. The two tiles therefore do not multiply out to the day, which is
    why `measured` exists to say which ones were timed."""
    db = _day_db(tmp_path)
    # A third sweep on the same day, still running: progress row, no pipeline row.
    db.start_progress(DAY_C, False)

    summary = data.day_summary(db, [DAY_A, DAY_B, DAY_C])

    assert summary["sweeps"] == 3
    assert summary["measured"] == 2
    # 10 minutes and 20 minutes.
    assert summary["avg_seconds"] == 900.0


def test_an_unmeasured_average_is_an_em_dash_but_zero_is_zero(tmp_path):
    """Same rule as the results CSV's blank `llm_score`: a printed zero reads as a
    measured zero. A sweep that finished inside a second, though, *was* measured — so
    the None/zero distinction has to be carried by the value, never by truthiness."""
    db = _day_db(tmp_path)
    base = data.overview_snapshot(db, None, run_ids=[DAY_A, DAY_B])

    none_html = overview.build({**base, "avg_seconds": None}, DAY)
    zero_html = overview.build({**base, "avg_seconds": 0.0}, DAY)

    assert '<span class="stat-n">—</span>' in none_html
    assert '<span class="stat-n">0s</span>' in zero_html


def test_a_day_with_no_sweeps_reads_zero_rather_than_the_whole_install(tmp_path):
    """`IN ()` is a SQLite syntax error and `if run_ids:` falls through to lifetime
    scope, so a truthiness test here would print every job the install has ever seen
    under a heading naming one empty day."""
    db = _day_db(tmp_path)

    assert db.overview_counts(None, run_ids=[]) == {
        "seen": 0, "relevant": 0, "shortlisted": 0, "applied": 0
    }
    assert db.load_lifetime_matches(50, run_ids=[]) == []
    assert db.load_unmatched_jobs(None, 50, run_ids=[]) == []
    assert db.load_applied_matches(None, run_ids=[]) == []
    assert db.lifetime_progress(run_ids=[])["sweeps"] == 0
    assert db.pipeline_spans([]) == []
    assert data.day_run_ids(db, "") == []

    snap = data.overview_snapshot(db, None, run_ids=[])
    assert snap["counts"] == {"seen": 0, "relevant": 0, "shortlisted": 0, "applied": 0}
    assert snap["scope"] == "day" and snap["sweeps"] == 0
    assert snap["avg_seconds"] is None


def test_a_day_picks_its_sweeps_by_the_date_on_their_folders(tmp_path):
    db = _day_db(tmp_path)
    day = run_ids.day_of_run_id(DAY_A)

    picked = data.day_run_ids(db, day)

    assert day == DAY
    assert DAY_A in picked and DAY_B in picked
    assert NEXT_DAY not in picked
    assert all(run_ids.day_of_run_id(r) == day for r in picked)


def test_a_day_page_reloads_only_while_that_days_sweep_is_running(tmp_path):
    """The lifetime page cannot tell whether a sweep is live and neither can this one —
    scanning every run for one in progress would count a crashed sweep as live forever.
    `refresh` knows, so it passes the answer in. No `stopped` chip either: that is a
    fact about one sweep."""
    db = _day_db(tmp_path)

    live = overview.build(
        data.overview_snapshot(db, None, run_ids=[DAY_A], live=True), DAY)
    idle = overview.build(
        data.overview_snapshot(db, None, run_ids=[DAY_A], live=False), DAY)

    assert 'http-equiv="refresh"' in live
    assert 'http-equiv="refresh"' not in idle
    assert 'class="chip">stopped</span>' not in idle


def test_a_snapshot_with_no_scope_key_renders_exactly_as_it_did(tmp_path):
    """Snapshots are built by hand all over this file. The renderer derives the scope
    from `snapshot["scope"]` and falls back to the old `run_id is not None` test, so
    every one of them keeps rendering what it rendered before the day scope existed."""
    db = _populated(tmp_path)
    for run_id, label in ((RUN, RUN), (None, None)):
        snap = data.overview_snapshot(db, run_id) if run_id is None else _snapshot(db)
        bare = {k: v for k, v in snap.items() if k != "scope"}
        assert overview.build(bare, label) == overview.build(snap, label)


# --- the page itself ----------------------------------------------------------


def test_all_five_accordions_render_with_their_counts(tmp_path):
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    # `class` stays the first attribute of every `.acc` — this count is why. The
    # ids below are the assertion that actually means "five sections".
    assert html.count('<details class="acc"') == 5
    for key in _SECTIONS:
        assert f'id="acc:{key}"' in html
    for label in ("Jobs Applied", "Needs Attention", "Jobs Shortlisted (to be applied)",
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
    # `r` is the two-word verdict and `j` the job id the "I applied" button needs.
    # Neither is a score, which is the thing this exact set exists to hold the line on.
    assert set(payload[0]) == {"t", "c", "l", "r", "x", "u", "j"}
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


def test_the_tail_says_why_each_job_is_there(tmp_path):
    """The one question this section could not answer. Its two halves get the reason
    from two different columns — `matches.skip_reason` for the jobs a paid-for gate
    dropped, `jobs.gate_reason` for the title gate's own — and both land in one
    column."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j9", title="Barista")])
    db.record_gate_reasons(RUN, [("j9", "title_excluded")])

    html = overview.build(_snapshot(db), RUN)
    payload = json.loads(
        html.split('id="ov-tail-data">')[1].split("</script>")[0].replace("<\\/", "</")
    )
    by_id = {r["j"]: r for r in payload}
    assert by_id["j9"]["r"] == "Title excluded"
    # j3 is in the same section, from `matches`, dropped by the cross-encoder.
    assert by_id["j3"]["r"] == "Below cutoff"
    assert "<th>Reason</th>" in html
    # The caps are on the columns they name, at the widths the grid above them uses.
    # Reason sits ahead of Location, so a stale index truncates a two-word verdict and
    # lets the locations — the thing the rule was written for — run off the right edge.
    # On real data all three are needed at once: with any of them missing the last
    # column, which is now the "I applied" button, goes off the edge of the box.
    assert ".scroll-y td:nth-child(3) { max-width:  8rem;" in html   # company, as 8rem
    assert ".scroll-y td:nth-child(5) { max-width: 11rem;" in html   # location, as 11rem
    assert ".scroll-y td.wide { min-width: 12rem; }" in html         # title, as 12rem
    assert ".scroll-y td:nth-child(4) {" not in html


def test_a_tail_row_with_no_recorded_reason_reads_as_a_dash(tmp_path):
    """Rows written before the column existed. An em dash, the same statement the
    score columns make: nothing here is known, rather than a reason invented for it."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j9", title="Barista")])

    html = overview.build(_snapshot(db), RUN)
    payload = json.loads(
        html.split('id="ov-tail-data">')[1].split("</script>")[0].replace("<\\/", "</")
    )
    assert next(r for r in payload if r["j"] == "j9")["r"] == "—"


def test_the_tail_offers_the_one_outcome_its_jobs_can_have(tmp_path):
    """A title-gated job was never shortlisted and has no `applied` row, so the only
    thing left to record about it is that the user applied to it themselves. The cell
    is in the row because the tail has no row body to put it in."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j9", title="Barista")])

    html = overview.build(_snapshot(db), RUN)
    assert overview._TAIL_MARK_LABEL in html
    assert '__MARK_LABEL__' not in html
    assert 'data-mark="applied"' in html
    # The script that copies the command has to ship with it, or the button is inert.
    assert "navigator.clipboard" in html
    # A row with no id gets an empty cell, never a button that cannot name anything.
    assert "r.j" in html and 'data-job="' in html


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
    # And one column the tail has alone, deliberately: thousands of its rows are
    # title-gate rejections, and why each one is there is the only question the
    # section could not answer. The four above it say it as a `.job-sub` sentence
    # instead, which a nowrap column could not hold.
    assert html.count(">Reason<") == 1


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


def test_an_application_off_the_backlog_says_so_and_only_it_does(tmp_path):
    """The one thing that separates a backlog application from any other on the page.

    Both rows read `submitted <time>`; without the clause the user cannot tell that
    j6 was shortlisted by an earlier sweep and only applied to now. It is a plain
    `job-sub`, not `warn` — a note about where the job came from, not a problem —
    and it must not reach a job applied to by the sweep that found it.
    """
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True)
    _apply(db, "j6", from_backlog=True)

    html = overview.build(_snapshot(db), RUN)
    backlog, ordinary = _job_block(html, "j6"), _job_block(html, "j1")
    assert '<span class="job-sub">submitted ' in backlog
    assert backlog.count(f"· {e(reasons.FROM_BACKLOG)}</span>") == 1
    assert '<span class="job-sub">submitted ' in ordinary
    assert reasons.FROM_BACKLOG not in ordinary


def test_needs_attention_never_carries_the_backlog_clause(tmp_path):
    """That section's question is what the user has to do now, so its line stays the
    cause. A backlog job that stopped short is recorded the same way as any other."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True)
    _apply(db, "j6", status="error", error="Posting closed", from_backlog=True)

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["attention"]
    assert reasons.FROM_BACKLOG not in overview.build(snap, RUN)


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
    assert targets["overview"].name == "Dashboard_Lifetime.html"
    assert targets["run_overview"].parent == tmp_path
    assert targets["run_overview"].name == f"Dashboard_{RUN}.html"


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

    assert (root / "Dashboard_Lifetime.html").exists()
    assert (run_dir / f"Dashboard_{RUN}.html").exists()


# --- the failure contract -----------------------------------------------------


def test_losing_the_page_is_reported_as_none_not_raised(tmp_path):
    """A report is a diagnostic. Losing one must never take down a sweep whose CSV,
    JSON and database rows are already safe."""
    blocked = tmp_path / "file.txt"
    blocked.write_text("not a directory", encoding="utf-8")
    assert overview.write(
        _snapshot(_populated(tmp_path)), blocked / overview.LIFETIME_NAME
    ) is None


def test_a_broken_snapshot_never_takes_down_the_run(tmp_path):
    assert overview.write({}, tmp_path / overview.LIFETIME_NAME) is None


def test_an_empty_install_still_renders(tmp_path):
    """Nothing swept, nothing scored, nothing applied — the page a first-time user
    sees before their first sweep finishes."""
    html = overview.build(data.overview_snapshot(_db(tmp_path), None), None)
    assert "Nothing yet." in html
    assert "ov-tail-data" not in html
    # No filter box over zero rows, and no script shipped to wire one.
    assert "filterable" not in html


# --- outcomes the user records by hand -----------------------------------------


def test_a_hand_marked_application_moves_to_jobs_applied(tmp_path):
    """The Needs Attention dead end, cleared. Three of the applier's verdicts hand the
    job back to the user, and until this existed there was nothing to do about it: the
    row stayed in that section for good however many times they applied by hand."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    _apply(db, "j6", "error", "Stuck on a required question — check whether it was sent.")
    assert _section_of(_snapshot(db), "j6") == ["attention"]

    db.mark_applied_by_hand("j6", "2026-09-10T09:00:00+00:00")

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["applied"]
    # The tile counts submissions, so it moves with the section — both key off the same
    # 'submitted' literal, which is why nothing else had to be taught about this.
    assert db.overview_counts(RUN)["applied"] == 2
    assert db.overview_counts(None)["applied"] == 2


def test_a_job_the_user_declined_is_filtered_and_keeps_its_score(tmp_path):
    """A decision not to apply is not an application, so it writes no `applied` row.

    It lands in Jobs Filtered under a label, exactly as the applier's location verdict
    does, and it keeps the number the judge gave it — `skipped` stays 0, which is what
    separates a job that lost from one nothing ever read.
    """
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    _apply(db, "j6", "excluded", worker.EXCLUDED_REASON)
    assert _section_of(_snapshot(db), "j6") == ["attention"]

    db.decline_job("j6")

    snap = _snapshot(db)
    assert _section_of(snap, "j6") == ["filtered"]
    # Not an application, so the tile does not move.
    assert db.overview_counts(RUN)["applied"] == 1

    block = _job_block(overview.build(snap, RUN), "j6")
    assert data.reason_label(DECLINED_BY_USER) in block
    # The score column prints the verdict, not an em dash: the judge did read this one.
    assert ">80<" in block


def test_declining_a_shortlisted_job_takes_it_off_the_applier_s_list(tmp_path):
    """The other half of the feature: a job the user applied to — or gave up on —
    before the sweep reached it must not be applied to anyway."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    assert _section_of(_snapshot(db), "j6") == ["shortlisted"]

    db.decline_job("j6")

    assert _section_of(_snapshot(db), "j6") == ["filtered"]
    assert db.load_pending_applications("1970-01-01T00:00:00+00:00") == []


def test_both_hand_recorded_states_keep_the_partition_whole(tmp_path):
    """The page's one real promise, with the two new states present at both scopes."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6"), _job("j7")])
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    _match(db, RUN, "j7", score=79, shortlisted=True, rerank=7.80)
    _apply(db, "j6", "error", "Stopped short.")
    db.mark_applied_by_hand("j6", "2026-09-10T09:00:00+00:00")
    db.decline_job("j7")

    for run_id in (RUN, None):
        snap = _snapshot(db, run_id)
        for job_id in ("j1", "j2", "j3", "j4", "j5", "j6", "j7"):
            assert len(_section_of(snap, job_id)) == 1, (run_id, job_id)


def test_each_section_offers_only_the_outcomes_it_can_actually_record(tmp_path):
    """A `file://` page cannot write to the database, so the button copies the command
    that can. Which buttons a section gets is not cosmetic: a job the funnel already
    dropped has no `applied` row and is not shortlisted, so `decline_job` would change
    nothing there — offering it would hand the user a command that reports
    `nothing_to_change`. A finished application gets neither."""
    db = _populated(tmp_path)
    db.insert_jobs(RUN, [_job("j6")])
    _match(db, RUN, "j6", score=80, shortlisted=True, rerank=7.90)
    _apply(db, "j6", "error", "Stopped short.")

    snap = _snapshot(db)
    html = overview.build(snap, RUN)

    # j6 needs attention: both outcomes are open to the user.
    assert 'data-mark="applied"' in _job_block(html, "j6")
    assert 'data-mark="declined"' in _job_block(html, "j6")
    # j2 lost on its score — they may have applied anyway, but there is nothing left
    # to decline.
    assert 'data-mark="applied"' in _job_block(html, "j2")
    assert 'data-mark="declined"' not in _job_block(html, "j2")
    # j1 is a finished application. Nothing to record.
    assert "job-mark" not in _job_block(html, "j1")
    # The command is the skill's, and the script that copies it shipped with the page.
    assert overview._MARK_COMMAND == "/hireshire:mark-applied"
    assert "navigator.clipboard" in html and "execCommand" in html


def test_a_page_with_nothing_to_act_on_ships_no_mark_script(tmp_path):
    """The same rule the other two scripts follow: no list, no script.

    The *styling* ships either way, like every other rule in OVERVIEW_CSS — it is the
    behaviour that is conditional, so this checks for the script and the buttons rather
    than for the class name.
    """
    db = _db(tmp_path)
    db.record_company(RUN, "acme", "greenhouse", "ok", 0, 0.1, None)
    html = overview.build(_snapshot(db), RUN)
    assert "navigator.clipboard" not in html
    # The markup, not the attribute selector or the comment the stylesheet carries
    # either way. It names the class rather than `<button type="button"`, because the
    # theme toggle is a button too and ships on every page — the old spelling would
    # pass or fail on attribute ordering, which is nobody's intent.
    assert 'class="job-mark"' not in html


# --- the per-company cap's hold line ----------------------------------------

def _limit(cap=2, hours=72):
    from types import SimpleNamespace
    return SimpleNamespace(max_per_company=cap, company_window_hours=hours)


def _held_db(tmp_path) -> Database:
    """j1 shortlisted at acme, which has two submissions this week; j2 at beta, none."""
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _match(db, RUN, "j1", shortlisted=True)
    _match(db, RUN, "j2", shortlisted=True, board_token="beta")
    for n, h in (("x1", 10), ("x2", 5)):
        db.record_applied(n, "acme", "t", "u", (now - timedelta(hours=h)).isoformat(),
                          "submitted", None, None)
    return db


def test_a_held_shortlisted_job_says_so_and_when(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "_company_limit", lambda: _limit())
    html = overview.build(_snapshot(_held_db(tmp_path)), RUN)

    held = _job_block(html, "j1")
    assert "Company limit reached · retries after" in held
    assert e("2 applications to acme in the last 3 days.") in held
    assert "Company limit" not in _job_block(html, "j2")


def test_no_hold_line_when_the_cap_or_the_applier_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "_company_limit", lambda: None)
    html = overview.build(_snapshot(_held_db(tmp_path)), RUN)
    assert "Company limit" not in html


def test_the_report_reads_the_cap_the_worker_would(tmp_path, monkeypatch):
    cfg = tmp_path / "applier.yaml"
    monkeypatch.setattr(data.paths, "config_file", lambda name: cfg)

    def write(*lines):
        cfg.write_text("\n".join(("settings:",) + lines) + "\n", encoding="utf-8")

    write("  enable_applier: true")
    limit = data._company_limit()
    assert (limit.max_per_company, limit.company_window_hours) == (2, 72)

    write("  enable_applier: false")
    assert data._company_limit() is None
    write("  enable_applier: true", "  max_per_company: 0")
    assert data._company_limit() is None
    cfg.unlink()
    assert data._company_limit() is None


# --- the theme toggle -------------------------------------------------------


def _theme_script(html: str) -> str:
    """The body-tail half of the toggle, isolated the way the state-script tests do it."""
    return html.split('var TKEY = "hs-overview-theme"')[1]


def test_every_scope_carries_the_theme_toggle(tmp_path):
    """One control, three pages. The scopes share one renderer, so the toggle is added
    once — but that is exactly the kind of thing a later scope-specific branch drops, and
    a dashboard whose sibling has the button and it does not is worse than none having
    it."""
    db = _populated(tmp_path)
    for snap, label in ((_snapshot(db), RUN),
                        (data.overview_snapshot(db, None), None),
                        (data.overview_snapshot(db, None, run_ids=[RUN]), DAY)):
        html = overview.build(snap, label)
        assert html.count('class="theme-toggle"') == 1
        assert html.count('id="hs-theme"') == 1
        # Inside the subtitle row, which is already a flex line carrying the chip.
        subtitle = html.split('<p class="subtitle">')[1].split("</p>")[0]
        assert 'class="theme-toggle"' in subtitle


def test_the_toggle_ships_even_with_nothing_to_act_on(tmp_path):
    """Unlike the filter and mark scripts, this one is not conditional on a list. The
    theme is a property of the page, not of its rows, so an empty sweep's dashboard is
    just as readable-or-not as a full one's."""
    db = _db(tmp_path)
    db.record_company(RUN, "acme", "greenhouse", "ok", 0, 0.1, None)
    html = overview.build(_snapshot(db), RUN)
    assert 'class="theme-toggle"' in html
    assert 'var TKEY = "hs-overview-theme"' in html


def test_the_stored_theme_is_applied_before_the_body(tmp_path):
    """The whole reason `document()` has a head hook.

    From the body tail the restore would paint the stylesheet's default and then swap
    it — on every one of the meta refresh's reloads, which is the case the accordion
    state script already exists for. So the stamp has to happen in `<head>`.
    """
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    stamp = html.index('root.setAttribute("data-theme", v)')
    assert stamp < html.index("<body>")
    assert stamp < html.index('<p class="subtitle">')
    # And the marker that reveals the button is stamped whatever storage does, so a
    # throwing sessionStorage leaves a working control rather than a hidden one.
    head = html.split("</head>")[0]
    assert head.index('"data-hs-js"') < head.index("sessionStorage")


def test_an_explicit_choice_wins_without_losing_the_system_default(tmp_path):
    """Both stamps and the fallback, all three of which the page needs.

    `[data-theme="dark"]` is what a click can reach; the media query is what an
    un-stamped page follows; and the `:not([data-theme="light"])` guard inside it is the
    only thing that lets a reader on a dark OS choose light. Drop any one and the toggle
    is half a control.
    """
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert ':root[data-theme="dark"] {' in html
    assert "@media (prefers-color-scheme: dark) {" in html
    assert ':root:not([data-theme="light"]) {' in html
    # The label follows the same three places, or a stamped page keeps offering the
    # theme it is already on.
    assert ':root[data-theme="dark"] .theme-toggle .t-off { display: inline; }' in html
    assert ':root:not([data-theme="light"]) .theme-toggle .t-off' in html


def test_the_ua_chrome_follows_the_theme_too(tmp_path):
    """`color-scheme` is what makes scrollbars and the filter inputs dark on a dark
    page. It needs all three places for the reason the tokens do: a value defined only
    inside the media query is simply absent in the un-stamped state."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert "color-scheme: light;" in html
    assert html.count("color-scheme: dark;") == 2


def test_the_theme_script_survives_storage_being_unavailable(tmp_path):
    """Same posture as the state script: a file:// origin can be opaque enough that
    touching storage throws, so the write is guarded and the click still works for the
    page in front of the reader. The head half returns rather than dying."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    head = html.split("</head>")[0]
    assert 'try { v = window.sessionStorage.getItem("hs-overview-theme"); }' in head
    assert "catch (e) { return; }" in head
    body = _theme_script(html)
    assert "try { window.sessionStorage.setItem(TKEY, value); } catch (e) {}" in body
    # The OS reading is guarded too — matchMedia is absent in old enough engines, and
    # the page must not lose its toggle over the label's wording.
    assert body.count("catch (e) {") >= 2


def test_the_two_scripts_do_not_share_a_key(tmp_path):
    """The accordion state and the theme are separate preferences, and three tests
    isolate the state script by splitting on its key literal — a second occurrence
    breaks the split rather than an assertion, which reads as nonsense."""
    html = overview.build(_snapshot(_populated(tmp_path)), RUN)
    assert html.count('KEY = "hs-overview-open"') == 1
    assert html.count('TKEY = "hs-overview-theme"') == 1
