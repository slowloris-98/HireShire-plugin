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

## Two rules that look like bugs and are not

**"Preferred" is treated exactly like "required".** Employers use the words
interchangeably and a "preferred: 5+ years" posting filters the same candidates out
in practice. This is a deliberate departure from the spike's prompt, which excluded
preference-phrased lines and lost most of them entirely.

**A range is only its lower bound.** "5-10 years" is read as 5, and the upper bound
is parsed solely so it cannot be mistaken for a second, separate requirement. Nothing
here ever rejects a candidate for having too MUCH experience: an over-qualified
applicant is a judgement call for the LLM, not a deterministic drop.
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
_REJECT_AFTER = re.compile(
    r"^\s*(?:ago\b|old\b|of age\b|of service\b|of combined\b|"
    r",?\s*we(?:'ve| have| are)?\b|in business\b|"
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

    `min_years` is the LOWEST reading in the document, which is what the gate
    compares against — see `parse_requirement` for why. `stated` keeps every
    surviving reading so a diagnostic can show the whole picture.
    """

    min_years: float
    stated: tuple[float, ...]


def _value(token: str) -> int:
    return int(token) if token.isdigit() else _WORD_NUMBERS[token.lower()]


def parse_requirement(text: str | None) -> ExperienceRequirement | None:
    """Every stated minimum in `text`, or None when it states none.

    None is the common case — roughly a quarter of real postings say nothing about
    years — and it means KEEP. Absent data must never drop a job.

    When a posting states several requirements ("8+ years leading teams… 12+ years
    full-stack… 3+ years infrastructure"), `min_years` is the lowest of them. That is
    the conservative reading: it only drops a candidate who misses even the easiest
    bar the posting names, and it was the only aggregation of the three measured that
    never read higher than the LLM label.

    Pass the FULL description. The reranker's `max_doc_chars` truncation is a cost
    dial for the cross-encoder and must not be applied here — the median posting does
    not reach its first requirements heading until character 1,094.
    """
    if not text:
        return None

    found: list[float] = []
    for match in _REQUIREMENT.finditer(text):
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 60]

        if _REJECT_BEFORE.search(before) or _REJECT_AFTER.match(after):
            continue
        # Experience language usually follows ("5 years of experience"), but a few
        # postings lead with it ("experience: 5 years"), so check a short window on
        # both sides.
        if not _EXPERIENCE.search(after) and not _EXPERIENCE.search(before[-25:]):
            continue

        value = _value(match.group(1))
        if _MIN_PLAUSIBLE <= value <= _MAX_PLAUSIBLE:
            found.append(float(value))

    if not found:
        return None
    return ExperienceRequirement(min_years=min(found), stated=tuple(found))


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
