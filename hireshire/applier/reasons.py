"""The fixed short lines the overview's Needs Attention section prints.

An application that did not go through is recorded with an `error` the user reads
verbatim. When that text was free-form, one cause arrived in half a dozen wordings — the
zip-code question in four, an emailed verification code in six — and the section read
as a wall of near-duplicates. Each cause now has one label, and this module is the only
place they are spelled:

- the engine writes them itself (`worker.EXCLUDED_REASON`, the ambiguous endings, the
  backlog's expiry);
- `apply_one.md` tells each session to use them exactly, and
  `tests/test_apply_worker.py` fails if the prompt and this module drift apart;
- `short_label` maps text **already stored** onto them at render time, so rows written
  before the labels existed — and a model that ignores an instruction — still read
  short. It never rewrites the stored text: the page keeps that as the tooltip, which is
  the only place the detail (which question, which employer) survives.

Stdlib only, so the reporting package can import it without pulling in the worker.
"""
from __future__ import annotations

import re
from typing import Optional

#: The portal needs a person: an account login (`exclude_companies`, a sign-in gate),
#: an emailed or texted verification code, or a CAPTCHA.
HUMAN_VERIFICATION = "Requires human verification"

#: A form question aimed at bots, or a posting that bans AI-written answers. Answering
#: either as a human would be a misrepresentation made in the user's name.
MANUAL_REQUIRED = "Manual application required."

#: Submit clicked with no confirmation, a timeout, or a session that ended without a
#: result. Every one may have come *after* the submit, so the label keeps the warning
#: the no-double-apply rule depends on.
SUBMIT_UNCONFIRMED = "Submit not confirmed — check before reapplying"

POSTING_CLOSED = "Posting closed"
REJECTED = "Rejected by employer"
NOT_A_JOB = "Not a job posting"
COVER_LETTER_OFF = "Cover letter required (generation is off)"

#: Followed by `": <topic>"` when the topic is one of `TOPICS`.
REQUIRED_QUESTION = "Required question"

#: The topics a `Required question:` label may name, in the order `short_label` tries
#: them — earlier wins when a message names several. Work authorization precedes
#: citizenship because an authorization question lists "US Citizen" among its options;
#: education precedes start date because "degree start date" is an education question.
TOPICS: tuple[tuple[str, str], ...] = (
    ("work authorization", r"authori[sz]ation|\bead\b|sponsorship"),
    ("citizenship / clearance", r"citizen|\bitar\b|u\.s\. person|clearance|export"),
    ("GPA", r"gpa"),
    ("zip code", r"\bzip\b|postal|address"),
    ("education", r"high school|degree|education|\bgrad\b|graduat|university"),
    ("start date", r"start date"),
    ("salary", r"salary|compensation"),
    ("work schedule", r"\bshift\b|schedule"),
    ("conflict of interest", r"non-?compete|non-?disclosure|\bnda\b|conflict|relatives|family"),
    ("upload or link", r"upload|\blink\b|\bpdf\b|proposal|portfolio"),
)


def required(topic: str) -> str:
    """The `Required question: <topic>` label."""
    return f"{REQUIRED_QUESTION}: {topic}"


def expired(hours: int) -> str:
    """The line for a job the backlog gave up on after `hours`."""
    return f"Not applied within {hours}h — apply yourself"


#: Stored text → label, first match wins. The order matters: a verification code is
#: "required", and a not-a-job note says "not submitted", so the specific causes are
#: tried before the broad ones.
_RULES: tuple[tuple[str, str], ...] = (
    (MANUAL_REQUIRED, r"manual application required|ai-written|ai-generated"),
    (HUMAN_VERIFICATION, r"verification code|one-time (pass)?code|\botp\b|captcha"
                         r"|sign[- ]?in (is )?required|sign in to apply|account login"),
    (NOT_A_JOB, r"^not a job"),
    (SUBMIT_UNCONFIRMED, r"not confirmed|unconfirmed|timed out|without a result"),
    (POSTING_CLOSED, r"\b404\b|no longer (open|available|accepting)|posting (is )?closed"
                     r"|job (looks|is|likely) closed|redirected to the .*job board"),
    (REJECTED, r"\brejected it\b|too many (recent )?applications"),
    (COVER_LETTER_OFF, r"cover letter.*\boff\b"),
)


def short_label(status: Optional[str], error: Optional[str]) -> Optional[str]:
    """The fixed label for a stored `applied` row, or None when no cause matches.

    None leaves the caller to render the stored text as before; a label never replaces
    a message it cannot account for.
    """
    if status == "excluded":
        # The engine is the only writer of this status, and it has one meaning.
        return HUMAN_VERIFICATION
    text = " ".join((error or "").split())
    if not text:
        return None
    for label, pattern in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    if re.search(r"\brequired\b", text, re.IGNORECASE):
        for topic, pattern in TOPICS:
            if re.search(pattern, text, re.IGNORECASE):
                return required(topic)
        return REQUIRED_QUESTION
    return None
