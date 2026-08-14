"""Group repeated requisitions so one employer cannot consume the whole LLM budget.

The problem this solves is measured, not hypothetical: in one real sweep a single
Townsquare Media requisition occupied 31 of 100 budget slots, and 12 copies of one
EquipmentShare posting sat just below the cut — together crowding out genuinely
good matches that ranked in the low hundreds.

The design answers two questions the naive framing ("dedupe") gets wrong:

*Who decides which duplicate to drop?* Nobody — nothing is dropped. Members of a
cluster are grouped, one representative is scored by the LLM, and the score is
copied back to every sibling. Every posting keeps its own row, location and link in
the all-jobs export.

*What if the user wants several roles at one company?* Different titles at one
company are different clusters and all stay independently eligible. Only literal
re-posts of the same requisition ever group.

Descriptions are deliberately never compared. Two postings can share a description
almost verbatim and still be different jobs (different level, team or location), and
similarity thresholds are exactly the kind of invisible judgement that is impossible
to audit later. The key is therefore conservative to the point of being boring:
same company, same title once obviously-cosmetic differences are normalised away.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

# Trailing "(anything)" — location or discipline suffixes that vary between
# otherwise identical postings: "Territory Account Manager (Pump, Power & HVAC)".
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")

# Trailing "- City, ST" / "— City, ST". Requires the two-letter state so a real
# qualifier ("- Growth") is never mistaken for a location.
_TRAILING_CITY_STATE = re.compile(r"\s*[-–—]\s*[^-–—,]+,\s*[A-Za-z]{2}\.?\s*$")

# Trailing requisition/station identifiers: "- 101.5", "#12345", "- REQ-4471".
# An explicit separator (dash/pipe) or a "#" is REQUIRED before the digits. Without
# that guard a bare trailing number is stripped too, and "Analyst 3" would collapse
# into "Analyst" — merging two different levels of the same job. A trailing word is
# always left alone, so "Account Executive - Growth" keeps its qualifier.
_TRAILING_REQ = re.compile(
    r"\s*(?:[-–—|]\s*(?:req[-\s]?)?#?|#)\d[\d.\-/]*\s*$", re.IGNORECASE
)

# Matches U+00A0 as well as ordinary whitespace: titles scraped out of HTML
# routinely carry non-breaking spaces, and two copies of one requisition that differ
# only in the kind of space must still land on the same key. Written as an escape
# rather than a literal so it stays visible to anyone editing this line.
_PUNCT_RUN = re.compile(r"[\s\u00a0]+")


def normalise_title(title: str) -> str:
    """Reduce a job title to its clusterable core.

    Strips only differences that are cosmetic across copies of one requisition:
    trailing parentheticals, a trailing "City, ST", and trailing requisition or
    station numbers. Everything else survives, including leading modifiers — so
    "Senior Multi-Media Account Executive" never clusters with "Multi-Media Account
    Executive", which are genuinely different jobs at different levels.

    Applied repeatedly, because real titles stack these: "Account Manager (Remote)
    - 101.5".
    """
    text = _PUNCT_RUN.sub(" ", (title or "")).strip()

    # Each pass can expose another suffix underneath. Bounded so a pathological
    # title cannot spin here.
    for _ in range(4):
        before = text
        text = _TRAILING_PAREN.sub("", text)
        text = _TRAILING_CITY_STATE.sub("", text)
        text = _TRAILING_REQ.sub("", text)
        text = text.strip(" -–—|,")
        if text == before:
            break

    # Never normalise a title out of existence: a posting genuinely called "101.5"
    # should cluster on its own key rather than joining every other stripped title.
    if not text.strip():
        return _PUNCT_RUN.sub(" ", (title or "")).strip().casefold()

    return text.casefold()


class _Clusterable(Protocol):
    """The bits of a Job cluster keys are built from."""

    board_token: str
    title: str


def cluster_key(job: _Clusterable) -> tuple[str, str]:
    """Group key: the employer plus the normalised title.

    Company is part of the key because "Account Manager" at two different employers
    is two different jobs — only repeated postings *within* one board token are ever
    the same requisition.
    """
    return ((job.board_token or "").casefold(), normalise_title(job.title))


def group(jobs: Iterable[_Clusterable]) -> dict[tuple[str, str], list]:
    """Bucket jobs by cluster key, preserving input order within each bucket."""
    clusters: dict[tuple[str, str], list] = {}
    for job in jobs:
        clusters.setdefault(cluster_key(job), []).append(job)
    return clusters


def pick_representative(members: Sequence, scores: dict[str, object]) -> object:
    """Choose which member of a cluster spends the LLM call.

    Best rerank score wins, so the cluster is judged on its strongest copy rather
    than whichever happened to be scraped first. Ties break toward the most recently
    updated posting, which is the one most likely to still be open.

    `scores` maps job_id -> RerankScores; its `sort_key` keeps the two rerank stages
    on their own scales (see funnel/rerank.py).
    """
    def rank(job):
        score = scores.get(job.job_id)
        key = score.sort_key if score is not None else (0, float("-inf"))
        return (key, job.updated_at)

    return max(members, key=rank)
