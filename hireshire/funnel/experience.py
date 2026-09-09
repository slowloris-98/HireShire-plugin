"""Read a stated years-of-experience minimum out of a job description, with a regex.

## Why not an encoder, and why not an LLM

A bi-encoder pools a description into one vector and a cross-encoder emits one logit;
neither has a span output, so there is nowhere for "5" to come out. Worse, subword
tokenisers shred numbers and pooling washes out magnitude, so "2+ years" and "12+
years" sit close together in embedding space. The property that makes the bi-encoder
a good recall net — it ignores surface detail in favour of topical similarity — is
exactly what makes it blind to the one token that matters here.

An LLM *can* do it. `analysis/extraction_spike.py` measured Haiku at this and the
precision gain was real, but the cost was a wash: ~20 Sonnet-equivalents spent to
save 18 judge calls. A regex keeps the gain and removes the cost.

Measured against that spike's 213 cached Haiku labels, taking the LOWEST stated
requirement agreed exactly 162/213 (76%) and — the number that matters — read *higher*
than the label **zero** times. Reading high is the only error direction that kills a
job wrongly. Most of the remaining disagreements are the label being wrong: Haiku
returned null on postings that plainly state a minimum ("At least five years of
relevant experience as a Superintendent", "15+ years of industry experience").

## The "+" is the requirement marker, and that is the whole trick

This module used to demand experience *language* near the number — "years" counted as
a requirement only when a word like `experience`, `working` or `industry` sat within a
short window. That guard silently threw away real requirements, because employers write
the domain instead of the word:

    7+ years owning financial planning and forecasting processes end-to-end
    10+ years in software engineering, with a focus on data engineering
    5+ years of production support, application support, systems support

None of those contain an experience word near the number, so all three were read as
stating no requirement at all and cost a full LLM call. On one measured sweep this was
7 of 50 paid calls.

The fix is to key on the shape rather than the vocabulary. An **open-ended minimum** —
`5+ years`, `5 plus years`, `at least 5 years`, `minimum of 5 years` — is a requirement
whatever follows it, and company prose almost never uses that form. Widening the word
list instead was tried and rejected: a rule loose enough to admit the lines above also
read "For 20 years, Acme has been building homes" as a 20-year requirement, which is
the one error direction that kills a good job.

## Three tiers, in order, first match wins

1. **Open-ended minimums** (`_OPEN_ENDED`), aggregated with **max**. No proximity check.
2. **Ranges** (`_RANGE`) — lower bound only, and see below.
3. **Bare counts plus the old proximity guard** (`_REQUIREMENT` + `_EXPERIENCE`),
   aggregated with min. This keeps "5 years of experience required" — no "+" anywhere —
   readable. Not dead weight: dropping it costs 7 of the 104 reachable labels
   (91% -> 85% agreement, and 1 missed requirement becomes 10).

Every tier still passes through `_REJECT_BEFORE`, `_REJECT_AFTER` and the plausibility
band, which are what keep company history and benefits tables out.

## Three rules that look like bugs and are not

**The HIGHEST open-ended minimum governs.** A posting stating `8+ years in Forward
Deployed Engineering` and `2+ years directly managing engineers` reads **8**, not 2.
This reverses an earlier decision, and the reversal is deliberate. Taking the lowest is
the reading that best agrees with `analysis/cache/extraction.json`, whose labels encode
exactly that rule — but agreement with those labels is not the objective. A candidate
who cannot clear the highest bar the posting names is not getting the job, and reading
the easiest bar sent 6 more calls per 50 to postings needing 5-8 years. Because the
labels use the lowest, this parser now reads *higher* than a label on occasion by
design; `analysis/yoe_gate_eval.py` section 3 (the safety floor) is the check that
matters, not its section 1 agreement count.

**"Preferred" is treated exactly like "required".** Employers use the words
interchangeably and a "preferred: 5+ years" posting filters the same candidates out
in practice. This is a deliberate departure from the spike's prompt, which excluded
preference-phrased lines and lost most of them entirely.

**A range is only its lower bound.** "5-10 years" is read as 5. Ranges are matched
before the open-ended tier and their spans are then excluded from it, so "3 to 7+
years" reads 3 rather than being torn into a separate 7. Nothing here ever rejects a
candidate for having too MUCH experience: an over-qualified applicant is a judgement
call for the LLM, not a deterministic drop.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Spelled-out counts. Sorted longest-first so the alternation cannot match "seven"
# inside "seventeen" and leave "teen" behind.
_WORD_NUMBERS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
_WORDS_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_NUM = rf"(?:\d{{1,2}}|{_WORDS_ALT})"

# A count, an optional "+"/"plus", an optional range tail, then a years unit.
#
# The range tail is matched but NOT captured on purpose: consuming "-10" as part of
# this match is what stops "5-10 years" being read as two requirements of 5 and 10.
# The lookbehind keeps the match off the tail of a decimal or a longer number.
_REQUIREMENT = re.compile(
    rf"(?<![\d.])({_NUM})\s*(?:\+|plus)?\s*"
    rf"(?:(?:-|–|—|to|or)\s*{_NUM}\s*(?:\+|plus)?\s*)?"
    r"(?:\+\s*)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)

_YEARS = r"(?:years?|yrs?)"

# Tier 1. An OPEN-ENDED minimum: "5+ years", "5 plus years", "5 years or more",
# "at least five years", "minimum of 8 yrs". The marker itself carries the meaning,
# which is why no experience-language check is applied to these — see the module
# docstring. Each alternative captures the count in group 1.
_OPEN_ENDED = (
    re.compile(rf"(?<![\d.])({_NUM})\s*(?:\+|plus)\s*{_YEARS}\b", re.IGNORECASE),
    re.compile(
        rf"(?<![\d.])({_NUM})\s*{_YEARS}\s*(?:\+|or more|or greater|or above)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:at least|minimum(?:\s+of)?|min\.?|no less than)\s*({_NUM})"
        rf"\s*\+?\s*{_YEARS}\b",
        re.IGNORECASE,
    ),
)

# Tier 2. A range. Matched BEFORE tier 1 so its span can be excluded from it —
# otherwise "3 to 7+ years" would read as an open-ended 7 instead of a range from 3.
_RANGE = re.compile(
    rf"(?<![\d.])({_NUM})\s*(?:-|–|—|to|or)\s*{_NUM}\s*(?:\+|plus)?\s*{_YEARS}\b",
    re.IGNORECASE,
)

# A count of years is only a *requirement* when experience language sits beside it.
# Without this, every "3 years" in a benefits table or a company history reads as one.
_EXPERIENCE = re.compile(
    r"\b(?:experience|exp\b|background|working|worked|hands[-\s]?on|"
    r"practice|practising|practicing|industry|professional|track record|"
    r"tenure|career)",
    re.IGNORECASE,
)

# Phrases that make the number something other than a requirement on the candidate.
# Every entry fires on a real posting in the measured corpus — "For 30 years, Per
# Scholas has been on a mission…" is the canonical one, which a naive `\d+ years`
# reads as a 30-year requirement.
#
# These are deliberately NARROW. A bare "for" or "with" would catch the company
# blurb, but it would also catch "looking for 5+ years of experience" and "candidates
# with 5+ years", which are the single most common ways a requirement is phrased.
# Each preposition here is therefore qualified by the word that makes it retrospective
# ("for over", "over the past"). Widen this only against real postings that it misses.
_REJECT_BEFORE = re.compile(
    r"(?:\bfor (?:over|more than|nearly|almost)|"
    r"\bwith (?:over|more than)|"
    r"\bover the (?:past|last)|"
    r"\bwithin the (?:last|past)|"
    r"\b(?:founded|established|in business|our history|serving|celebrating)\b[^.]*|"
    r"\bevery|\bage of|\bunder)\s*$",
    re.IGNORECASE,
)
# The last four entries carry the weight now that tier 1 runs without a proximity
# check. "A Best Places to Work company 10 years in a row" and "a 7+year history of AI
# innovation" both sit in the posting that prompted this module's rewrite, and the
# second one has a "+", so it reaches tier 1 and nothing else would stop it.
_REJECT_AFTER = re.compile(
    r"^\s*(?:ago\b|old\b|of age\b|of service\b|of combined\b|"
    r",?\s*we(?:'ve| have| are)?\b|in business\b|"
    r"in a row\b|history\b|straight\b|running\b|"
    r"of (?:vacation|pto|paid|parental|leave|tenure))",
    re.IGNORECASE,
)

# Readings outside this band are noise, not requirements — a "0 years" line is a
# non-statement and nothing legitimately asks for more than 25.
_MIN_PLAUSIBLE = 1
_MAX_PLAUSIBLE = 25


@dataclass(frozen=True)
class ExperienceRequirement:
    """What a posting asks for, in years.

    `min_years` is the EFFECTIVE requirement — the bar the candidate has to clear —
    and which reading that is depends on the tier that produced it: the highest of
    the open-ended minimums, or the lowest bound of a range, or the lowest bare
    count. See `parse_requirement`. The name predates the tiers and is kept because
    `matcher`, `scorer` and `store` all address it; it does not mean "the smallest
    number in the document".

    `stated` keeps every surviving reading from the tier that won, in document
    order, so a diagnostic can show the whole picture.
    """

    min_years: float
    stated: tuple[float, ...]


def _value(token: str) -> int:
    return int(token) if token.isdigit() else _WORD_NUMBERS[token.lower()]


def parse_requirement(text: str | None) -> ExperienceRequirement | None:
    """Every stated minimum in `text`, or None when it states none.

    None is the common case — roughly a quarter of real postings say nothing about
    years — and it means KEEP. Absent data must never drop a job.

    Three tiers, first non-empty one wins (see the module docstring for why):

      1. open-ended minimums ("8+ years… 12+ years… 3+ years") -> the HIGHEST, 12.
         A candidate who cannot clear the highest bar the posting names is not
         getting the job, so the easiest one is the wrong thing to measure against.
      2. a range ("3-7 years") -> its lower bound, 3. Never an upper bound: nothing
         here drops anyone for being over-qualified.
      3. bare counts with experience language beside them -> the LOWEST, unchanged
         from the behaviour that predates the tiers.

    Pass the FULL description. The reranker's `max_doc_chars` truncation is a cost
    dial for the cross-encoder and must not be applied here — the median posting does
    not reach its first requirements heading until character 1,094.
    """
    if not text:
        return None

    # Ranges first: their spans are excluded from tier 1 so that "3 to 7+ years"
    # cannot also be read as an open-ended 7.
    ranges: list[tuple[int, float]] = []
    range_spans: list[tuple[int, int]] = []
    for match in _RANGE.finditer(text):
        range_spans.append(match.span())
        value = _admit(text, match)
        if value is not None:
            ranges.append((match.start(), value))

    open_ended: list[tuple[int, float]] = []
    for pattern in _OPEN_ENDED:
        for match in pattern.finditer(text):
            if any(lo <= match.start() < hi for lo, hi in range_spans):
                continue
            value = _admit(text, match)
            if value is not None:
                open_ended.append((match.start(), value))

    if open_ended:
        return _requirement(open_ended, max)
    if ranges:
        return _requirement(ranges, min)

    bare: list[tuple[int, float]] = []
    for match in _REQUIREMENT.finditer(text):
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 60]
        # Experience language usually follows ("5 years of experience"), but a few
        # postings lead with it ("experience: 5 years"), so check a short window on
        # both sides. Tier 1 needs no such check; this tier has no "+" to rely on.
        if not _EXPERIENCE.search(after) and not _EXPERIENCE.search(before[-25:]):
            continue
        value = _admit(text, match)
        if value is not None:
            bare.append((match.start(), value))

    if bare:
        return _requirement(bare, min)
    return None


def _admit(text: str, match: re.Match[str]) -> float | None:
    """The count this match states, or None if it is not a requirement at all.

    The guards every tier shares: surrounding prose that makes the number
    retrospective or a benefit, and the plausibility band.
    """
    before = text[max(0, match.start() - 40):match.start()]
    after = text[match.end():match.end() + 60]
    if _REJECT_BEFORE.search(before) or _REJECT_AFTER.match(after):
        return None
    value = _value(match.group(1))
    if not _MIN_PLAUSIBLE <= value <= _MAX_PLAUSIBLE:
        return None
    return float(value)


def _requirement(found: list[tuple[int, float]], governs) -> ExperienceRequirement:
    """Pick the effective requirement out of one tier's readings.

    `stated` is kept in document order rather than the order the patterns happened to
    run in, so a diagnostic reads the way the posting does.
    """
    values = [value for _, value in sorted(found)]
    return ExperienceRequirement(min_years=governs(values), stated=tuple(values))


def meets(
    requirement: ExperienceRequirement | None,
    candidate_years: float,
    tolerance_years: float,
) -> bool:
    """Is the candidate experienced enough for this posting?

    `tolerance_years` is slack below the stated bar, because a posting asking for 5
    does interview someone with 4.5. The comparison is `>=`, so a candidate sitting
    exactly on the tolerance boundary is kept — every tie in this module resolves
    toward keeping the job.

    An unstated requirement always passes.
    """
    if requirement is None:
        return True
    return candidate_years + tolerance_years >= requirement.min_years
