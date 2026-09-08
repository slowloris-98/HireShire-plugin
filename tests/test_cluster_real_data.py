"""Clustering checked against titles taken verbatim from a production database.

`test_cluster.py` covers the normaliser with cases transcribed by hand. This file
covers it with cases *extracted*, and the difference matters: hand-picked examples
inherit the author's model of what a duplicate looks like, which is the very thing
under test.

**Provenance.** 192,700 job rows across the ten largest sweeps in the reference
install's database (`HireShire/data/hireshire.db`, runs 2026-07-14 .. 2026-08-27).
Titles below are copied exactly, including the doubled spaces and non-breaking
spaces the boards really emit.

**Method.** Ground truth for "the same requisition" is a hash of the *full*
description text of two postings at one employer -- never their titles, which would
beg the question the normaliser exists to answer. Two corrections came out of
building that oracle and are worth recording, because both directions of the naive
version are wrong:

- A fingerprint of `length + first 300 + last 300` chars is useless. Greenhouse
  descriptions open with a boilerplate company intro and close with boilerplate EEO
  text, so at Anduril and Roblox it matched unrelated roles. Only a full hash
  separates them.
- Description equality does not imply one requisition. Employers template: sweetgreen
  ships identical text for `Assistant Coach` and `Restaurant Manager`, and Carvana
  A/B-tests four titles over one job. Description *difference* is strong evidence of
  two jobs; description *sameness* is weak evidence of one.

So a merge is asserted below only where the titles differ by location, requisition id
or whitespace alone -- and a split only where the parenthetical names a different
product line, shift, employment type or level, corroborated by differing text.

The bias is the one `cluster.py` documents: over-merging costs the user a job they
never learn existed, and silently, because a sibling inherits `duplicate_of_cluster`
and that reason is deliberately not retryable. Under-merging costs one budget slot.
"""
from __future__ import annotations

import pytest

from hireshire.funnel.cluster import group, normalise_title
from test_cluster import make_job


def same(a: str, b: str) -> bool:
    return normalise_title(a) == normalise_title(b)


# --- 1. one requisition, posted repeatedly: MUST cluster -------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        # Location is the only difference. Identical description text at each.
        # healthcare|wd1|private
        ("Acera Senior Account Executive - Baltimore, MD",
         "Acera Senior Account Executive - San Jose, CA"),
        # veterinaryemergencygroupst -- one role staffed across eight clinics.
        ("Emergency Credentialed Veterinary Technician - Leesburg, VA",
         "Emergency Credentialed Veterinary Technician - Henderson, NV"),
        ("Emergency Credentialed Veterinary Technician - Princeton, NJ",
         "Emergency Credentialed Veterinary Technician - Virginia Beach, VA"),
        # urpt -- a travelling role advertised per metro. Descriptions differ here
        # (each names its own city), which is exactly why the oracle is not the only
        # input: the title difference is still purely locational.
        ("Physical Therapist National Traveler - Kansas City, MO",
         "Physical Therapist National Traveler - Waldo, MO"),
        # Requisition id is the only difference. nttlimited|wd3, tbc|wd12, hempel|wd3.
        ("Senior Engineer - MS, Network", "Senior Engineer - MS, Network-2"),
        ("Senior Engineer - MS, Network-2", "Senior Engineer - MS, Network-4"),
        ("Senior Software Engineer", "Senior Software Engineer-1"),
        ("Showroom Assistant", "Showroom Assistant-2"),
        # Case only -- the same posting re-entered by a different recruiter.
        # bpinternational|wd3, jci|wd5, lytx|wd1.
        ("Staff Software Engineer", "Staff software engineer"),
        ("Service Engineer", "Service engineer"),
        ("Senior DevOps Engineer", "Senior Devops Engineer"),
        # Whitespace only. browserstack|wd3 emits a doubled space.
        ("Account Manager  - Strategic Sales", "Account Manager - Strategic Sales"),
        # scoutmotors emits the same title twice, once with U+00A0 throughout.
        ("Direct Procurement Specialist – Metal Commodity",
         "Direct Procurement Specialist – Metal Commodity"),
        # Stacked: a parenthetical over a city. universalproperty|wd1, gevernova|wd5.
        ("Field Adjuster - Detroit, MI (Local Only)",
         "Field Adjuster - Indianapolis/South Bend, IN (Local Only)"),
        ("Wind Hub Technician (Criterion, Maryland)", "Wind Hub Technician- Traverse, OK"),
    ],
)
def test_repeat_postings_of_one_requisition_cluster(a, b):
    assert same(a, b), f"{a!r} and {b!r} are one requisition and must cluster"


# --- 2. different jobs that merely share a stem: MUST NOT cluster ----------------
# Every pair here was confirmed to carry different description text at the employer.

@pytest.mark.parametrize(
    "a,b",
    [
        # Product line. SpaceX qualifies almost every title this way, and the
        # programmes are unrelated engineering organisations. `Automation & Controls
        # Engineer` alone spans seven distinct postings under one normalised title.
        ("Automation & Controls Engineer (Starlink)",
         "Automation & Controls Engineer (Starship)"),
        ("Automation & Controls Engineer (Facilities)",
         "Automation & Controls Engineer (Raptor Manufacturing Systems)"),
        ("Antenna Engineer (Starlink)", "Antenna Engineer (Starship)"),
        ("Avionics Test Engineer (Starshield)", "Avionics Test Engineer (Starship)"),
        ("Civil Engineer, Land Development (Starlink)",
         "Civil Engineer, Land Development (Starship Launch Pad)"),
        # andurilindustries -- three distinct hardware disciplines, four descriptions.
        ("Electrical Engineer", "Electrical Engineer (Actuators)"),
        ("Electrical Engineer (Actuators)", "Electrical Engineer (Motor Controls)"),
        # Shift. Same work, different hours, different pay -- and the user may be able
        # to take one and not the other. spacex, xai, industrialelectricmanufacturing.
        ("Construction Superintendent", "Construction Superintendent (Night Shift)"),
        ("Data Center Operations Technician",
         "Data Center Operations Technician (Night Shift)"),
        ("Production Controller", "Production Controller (Second Shift)"),
        ("CNC Programmer (Starship Components) - Level 4/5",
         "CNC Programmer (Starship Components) - Level 4/5 (2nd Shift)"),
        # Employment type. greenthumbindustries, hfecorp|wd503, fullsail|wd1, xai.
        ("Personal Care Specialist (Full Time)", "Personal Care Specialist (Part Time)"),
        ("Personal Care Specialist", "Personal Care Specialist (Part Time)"),
        ("Story Land- Facilities Maintenance (Full Time)",
         "Story Land- Facilities Maintenance (Seasonal)"),
        ("Adjunct Faculty - Information Technology (Part-Time)",
         "Adjunct Faculty - Information Technology (Remote)"),
        ("Front Desk Ambassador", "Front Desk Ambassador (Part-Time)"),
        # veterinaryemergencygroupst -- relief and overnight are different postings.
        ("Emergency Veterinary Assistant (Part Time) - Redmond, WA",
         "Emergency Veterinary Assistant (Overnight) - Redmond, WA"),
        # Level in a parenthetical. This is the `Analyst 3` failure the bare-number
        # guard in _TRAILING_REQ was written to prevent, wearing brackets.
        # nttlimited|wd3 posts L1/L2/L3 of one ladder.
        ("Security Managed Services Engineer (L1)",
         "Security Managed Services Engineer (L3)"),
        ("Server Load Balancer Engineer (L1)", "Server Load Balancer Engineer (L2)"),
        # Specialisation. cambiumlearning|wd1, urpt (15 distinct descriptions).
        ("Senior Software Engineer", "Senior Software Engineer (AI Applications)"),
        ("Physical Therapist - National Traveler",
         "Physical Therapist - National Traveler (Journey by Upstream)"),
    ],
)
def test_distinct_jobs_sharing_a_stem_stay_separate(a, b):
    assert not same(a, b), f"{a!r} and {b!r} are different jobs and must not merge"


# --- 3. both behaviours inside one real cluster ----------------------------------

def test_a_real_employer_cluster_splits_on_qualifier_but_not_on_location():
    """Eight postings scraped from veterinaryemergencygroupst in one sweep.

    The plain ones are one requisition staffed across four clinics and belong in a
    single cluster. `(Part Time)`, `(Relief)` and `(Overnight)` are separate
    postings and must each keep their own -- so the correct outcome is four
    clusters, not one, and not eight.
    """
    titles = [
        "Emergency Credentialed Veterinary Technician - Henderson, NV",
        "Emergency Credentialed Veterinary Technician - Leesburg, VA",
        "Emergency Credentialed Veterinary Technician - Princeton, NJ",
        "Emergency Credentialed Veterinary Technician - Virginia Beach, VA",
        "Emergency Credentialed Veterinary Technician (Part Time) - Leesburg, VA",
        "Emergency Credentialed Veterinary Technician (Relief) - Boulder, CO",
        "Emergency Credentialed Veterinary Technician (Relief) - Henderson, NV",
        "Emergency Credentialed Veterinary Technician (Overnight) - Redmond, WA",
    ]
    jobs = [make_job(str(i), t, board="veterinaryemergencygroupst")
            for i, t in enumerate(titles)]
    clusters = group(jobs)

    sizes = sorted(len(v) for v in clusters.values())
    assert sizes == [1, 1, 2, 4], (
        "expected the four plain postings to merge and each qualifier to stand "
        f"alone, got {sizes}"
    )
