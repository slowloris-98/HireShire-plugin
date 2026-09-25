"""The fixed labels Needs Attention prints, and the mapping from stored text onto them.

Every message below was copied from a real install's `applied` table. They are the
wordings the labels exist to collapse: one cause, many phrasings.
"""
import pytest

from hireshire.applier import reasons
from hireshire.applier.reasons import short_label

HV = reasons.HUMAN_VERIFICATION

_REAL = [
    # An emailed code — six phrasings of one cause on one install.
    ("Email verification code required to submit — enter the code sent to your email "
     "and submit manually.", HV),
    ("Email verification code required to submit; form filled but not submitted — "
     "finish manually.", HV),
    ("Email verification code sent to your inbox; enter it on the form to finish "
     "submitting.", HV),
    ("Waymo emailed a verification code; enter it on the page to finish. Not confirmed, "
     "so don't reapply.", HV),
    ("Sign-in required before the form appears.", HV),
    ("Submit clicked but not confirmed — check before applying again.",
     reasons.SUBMIT_UNCONFIRMED),
    ("Timed out after 900s — check whether it was submitted before applying again.",
     reasons.SUBMIT_UNCONFIRMED),
    ("Session ended without a result — check whether it was submitted before applying "
     "again.", reasons.SUBMIT_UNCONFIRMED),
    ("Posting returns 404 (page not found) — job likely closed; check Roku careers for "
     "a new listing.", reasons.POSTING_CLOSED),
    ('Posting closed: redirected to the Stack AV job board ("no longer open"). Nothing '
     "to apply to.", reasons.POSTING_CLOSED),
    ("Posting redirected to the job board (error=true); the job looks closed. Check the "
     "listing.", reasons.POSTING_CLOSED),
    ("Sierra rejected it: too many recent applications. Try again in a few months.",
     reasons.REJECTED),
    ("Not a job: Rutgers-student workshop registration (Oct 15). Not submitted; apply "
     "by hand if you want it.", reasons.NOT_A_JOB),
    ("Required cover letter, but cover letter generation is off. Apply manually or turn "
     "it on.", reasons.COVER_LETTER_OFF),
    ("Apply by hand: needs transcripts, 2 writing samples and essays; AI-written answers "
     "get you permanently banned.", reasons.MANUAL_REQUIRED),
    ("Manual application required.", reasons.MANUAL_REQUIRED),
    # The zip-code question, four phrasings.
    ("Required question: zip code of primary residence (not on resume or details).",
     reasons.required("zip code")),
    ("Required question: current Zip Code (not on resume or in applicant details).",
     reasons.required("zip code")),
    ("Required question 'Zip / postal code' cannot be answered honestly — the resume "
     "only gives 'San Jose, CA' with no zip code, so the form was left unsubmitted.",
     reasons.required("zip code")),
    ('Required question "Please provide the zip code of your current, permanent '
     'address." cannot be answered honestly.', reasons.required("zip code")),
    ('Required ITAR question: are you a "U.S. Person" (citizen/green card)? Not in '
     "resume or details.", reasons.required("citizenship / clearance")),
    ('Blocked by the required "CLEARANCE ELIGIBILITY" question (can you get a U.S. '
     "security clearance?)", reasons.required("citizenship / clearance")),
    # Lists "US Citizen" among its options, and is still an authorization question.
    ("Required question: specific work authorization status (US Citizen/Green Card/"
     "H1B/F1-OPT etc.) is unknown.", reasons.required("work authorization")),
    ("Required question: cumulative GPA (not on resume); also needs street "
     "address/zip.", reasons.required("GPA")),
    ("Required question: education start date month/year (not on resume).",
     reasons.required("education")),
    ('Required question: start date accepts only a date (no "Flexible" option).',
     reasons.required("start date")),
    ("Required salary question accepts only dollar ranges (no negotiable option) — "
     "choose a range and apply manually.", reasons.required("salary")),
    ("Required conflict question: do relatives/household work at asset managers or "
     "hold public office? Answer manually.", reasons.required("conflict of interest")),
    ("Required question: link to an agentic system you built (GitHub/demo) — no "
     "portfolio/GitHub URL given.", reasons.required("upload or link")),
    ("Required question: do you enter sports/esports contests watched by sportsbooks or "
     "fantasy sites? Not on resume.", reasons.REQUIRED_QUESTION),
]


@pytest.mark.parametrize("error, label", _REAL)
def test_real_messages_collapse_onto_one_label_per_cause(error, label):
    assert short_label("error", error) == label


def test_an_excluded_employer_reads_as_human_verification_whatever_was_stored():
    """The engine is the only writer of `excluded`, so the status alone decides —
    which is what shortens the long sentence older installs stored."""
    old = ("Requires human verification — this employer's portal needs an account "
           "login, so apply to it yourself.")
    assert short_label("excluded", old) == HV
    assert short_label("excluded", "") == HV


def test_text_no_label_accounts_for_is_left_alone():
    """None means the page renders the stored text as before. A label must never stand
    in for a message it cannot account for."""
    paragraph = ("Application was not submitted: the first submit was rejected by the "
                 "form's check that a phone country be selected, and after fixing it the "
                 "retry was blocked by a permission denial.")
    assert short_label("error", paragraph) is None
    assert short_label("error", "") is None
    assert short_label("error", None) is None
    assert short_label("expired", reasons.expired(72)) is None


def test_every_label_fits_on_one_line():
    labels = [reasons.HUMAN_VERIFICATION, reasons.MANUAL_REQUIRED,
              reasons.SUBMIT_UNCONFIRMED, reasons.POSTING_CLOSED, reasons.REJECTED,
              reasons.NOT_A_JOB, reasons.COVER_LETTER_OFF, reasons.expired(72)]
    labels += [reasons.required(topic) for topic, _ in reasons.TOPICS]
    assert all(len(label) <= 70 for label in labels)
