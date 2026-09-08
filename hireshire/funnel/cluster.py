"""Group repeated requisitions so one employer cannot consume the whole LLM budget.

The problem this solves is measured, not hypothetical: in one real sweep a single
Townsquare Media requisition occupied 31 of 100 budget slots, and 12 copies of one
EquipmentShare posting sat just below the cut — together crowding out genuinely
good matches that ranked in the low hundreds.

*Who decides which duplicate to drop?* Nobody — nothing is dropped. Members of a
cluster are grouped, one representative is scored by the LLM, and the score is
copied back to every sibling. Every posting keeps its own row, location and link in
the all-jobs export.

**The key is the description, not the title.** This reverses an earlier design and
the reversal was forced by data. Keying on `(board_token, normalised_title)` meant
stripping "cosmetic" trailing qualifiers, and an audit of 192,700 real postings
showed that strip was wrong two times out of three: of 2,578 clusters formed by
removing a trailing parenthetical, 1,739 grouped postings whose descriptions
differed. SpaceX qualifies nearly every title by programme, so one cluster held
seven unrelated jobs:

    Automation & Controls Engineer (Facilities)
    Automation & Controls Engineer (Raptor Manufacturing Systems)
    Automation & Controls Engineer (Starlink)
    Automation & Controls Engineer (Starship Launch Hardware)      ... and three more

Six of those inherit `duplicate_of_cluster`, which is deliberately not retryable, so
they were retired permanently and silently on a verdict about a different job. The
same strip collapsed shifts, employment types and ladder levels — `(L1)` with `(L3)`.

A title is marketing copy and employers rewrite it freely; the description is the
job. So two postings at one employer are the same requisition when their
descriptions match, and the title is not consulted at all.

**Exact equality is too strict, because employers localise.** Pay bands and addresses
are interpolated per market, so copies of one requisition are near-identical rather
than identical. Hence `max_word_diff`. Validated against Veterinary Emergency Group,
whose postings contain both cases at once:

    Emergency Credentialed Veterinary Technician - Leesburg, VA
    Emergency Credentialed Veterinary Technician - Henderson, NV     0 words  merge
    Emergency Credentialed Veterinary Technician - Georgetown, DC    4 words  merge
    Emergency Credentialed Veterinary Technician (Overnight)        13 words  separate
    Emergency Credentialed Veterinary Technician (Part Time)        99 words  separate
    Emergency Credentialed Veterinary Technician (Relief)          138 words  separate

The default of 10 is a judgement call — the diff histogram rises smoothly and has no
knee — but it is bounded on both sides by that table: `(Overnight)` at 13 means 15
would already be too loose.

Two costs are accepted deliberately, and neither should be "fixed" without data:

- **Templated employers over-merge.** sweetgreen ships byte-identical text for
  `Assistant Coach` and `Assistant Restaurant Manager`. With the title ignored,
  nothing separates them. Re-introducing a title check to catch this would rebuild
  the machinery this module just removed.
- **Localised employers fragment.** Carvana's `Customer Delivery Driver` differs by
  17–38 words between metros, so each is now its own cluster. Measured against two
  real sweeps: 10,124 title clusters became 11,131, and 8,876 became 9,778 — roughly
  +900 to +1,010 LLM calls each. Those extra postings fall out on the cap, which *is*
  retryable, so they resurface in a quieter sweep rather than being lost.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Optional, Protocol

# Matches U+00A0 as well as ordinary whitespace: descriptions scraped out of HTML
# routinely carry non-breaking spaces, and two copies of one requisition that differ
# only in the kind of space must still land together. Written as an escape rather
# than a literal so it stays visible to anyone editing this line.
_WHITESPACE = re.compile(r"[\s ]+")

# Postings whose descriptions differ by more than this many words are different jobs.
DEFAULT_MAX_WORD_DIFF = 10


def normalise_description(text: Optional[str]) -> tuple[str, ...]:
    """Reduce a description to the token tuple comparisons run on.

    Case-folded and whitespace-collapsed, nothing more. Punctuation is deliberately
    kept: it costs nothing, and stripping it would silently merge postings that
    differ only in a list of figures.

    Returns an empty tuple when there is no description. Note that both `None` and
    `""` occur in practice — the `strip_html` validator on `Job.content_text` turns
    markup-only HTML into the empty string — so callers must test the result, never
    the input.
    """
    if not text:
        return ()
    return tuple(_WHITESPACE.sub(" ", text).strip().casefold().split())


def word_diff(a: Sequence[str], b: Sequence[str]) -> int:
    """How many words separate two descriptions, as a multiset symmetric difference.

    **A substitution counts as 2** — one word removed, one added. Every threshold in
    this module is calibrated in these units, so changing the metric silently halves
    or doubles `max_word_diff`. The Veterinary Emergency Group table in the module
    docstring is the reference: those numbers are in this metric.

    Order-insensitive, which is what makes it cheap. Two descriptions built from the
    same words in a different order score 0; no real posting does that, and the
    alternative is a quadratic alignment over thousand-word documents.
    """
    ca, cb = Counter(a), Counter(b)
    return sum(((ca - cb) + (cb - ca)).values())


class _Clusterable(Protocol):
    """The bits of a Job that clustering reads."""

    board_token: str
    job_id: str
    content_text: Optional[str]


class _Anchor:
    """One cluster, represented by the first job assigned to it."""

    __slots__ = ("tokens", "counts", "vocab", "members")

    def __init__(self, job, tokens: tuple[str, ...]) -> None:
        self.tokens = tokens
        self.counts = Counter(tokens)
        self.vocab = frozenset(tokens)
        self.members = [job]

    def matches(self, counts: Counter, vocab: frozenset, limit: int) -> bool:
        # Distinct-token symmetric difference is a lower bound on the multiset one
        # (a word in both texts contributes 0 here and >= 0 there), so rejecting on it
        # is exact — it can never discard a real match. It runs at C speed on
        # frozensets, which matters: the full Counter difference over two
        # thousand-word descriptions is the expensive part of a sweep's clustering,
        # and most candidate pairs die on this line instead.
        if len(self.vocab ^ vocab) > limit:
            return False
        return sum(((self.counts - counts) + (counts - self.counts)).values()) <= limit


def _rank_key(job, scores: dict[str, float]):
    """Best rerank score first, then freshest, then job_id.

    The tie-breaks are not decoration. 2,279 rows in one real sweep tied on
    `updated_at`, and without the final key input order — i.e. scrape order — would
    decide which posting becomes the representative.
    """
    updated = getattr(job, "updated_at", None)
    stamp = updated.timestamp() if updated is not None else 0.0
    return (-scores.get(job.job_id, float("-inf")), -stamp, job.job_id)


def group(
    jobs: Iterable,
    scores: Optional[dict[str, float]] = None,
    *,
    max_word_diff: int = DEFAULT_MAX_WORD_DIFF,
) -> list[list]:
    """Bucket postings into clusters, each with its representative FIRST.

    `scores` maps job_id -> the cross-encoder logit, and decides which member anchors
    a cluster. Because the reranker has already run by the time this is called, the
    anchor can simply be the best-scoring member — so the cluster is judged on its
    strongest copy without a second selection pass, and without the circularity of
    picking a representative after grouping.

    **Every member is within `max_word_diff` of the representative itself**, never
    merely of some other member. Chaining (A matches B, B matches C, so A joins C)
    is deliberately not allowed: it lets a cluster's extremes drift arbitrarily far
    apart while one verdict is copied across the whole span. Anchoring keeps the
    inherited verdict defensible for every single sibling.

    Postings with no description are never clustered — an absent description is an
    absence of evidence, not evidence of uniqueness, and the alternative is grouping
    jobs on nothing at all. They come back as singletons.
    """
    scores = scores or {}

    by_board: dict[str, list] = {}
    for job in jobs:
        by_board.setdefault((getattr(job, "board_token", "") or "").casefold(), []).append(job)

    clusters: list[list] = []

    for board_jobs in by_board.values():
        anchors: list[_Anchor] = []
        # token count -> indices into `anchors`. A symmetric difference of d implies
        # the lengths differ by at most d, so restricting comparisons to this window
        # is an exact prefilter, not a heuristic — it can never miss a match.
        by_length: dict[int, list[int]] = {}

        for job in sorted(board_jobs, key=lambda j: _rank_key(j, scores)):
            tokens = normalise_description(getattr(job, "content_text", None))
            if not tokens:
                clusters.append([job])
                continue

            counts = Counter(tokens)
            vocab = frozenset(tokens)
            n = len(tokens)
            joined = False
            # Ascending index order == descending rerank score, so a posting joins
            # the strongest cluster it fits rather than an arbitrary one.
            for idx in sorted(
                i
                for length in range(n - max_word_diff, n + max_word_diff + 1)
                for i in by_length.get(length, ())
            ):
                if anchors[idx].matches(counts, vocab, max_word_diff):
                    anchors[idx].members.append(job)
                    joined = True
                    break

            if not joined:
                by_length.setdefault(n, []).append(len(anchors))
                anchors.append(_Anchor(job, tokens))

        clusters.extend(a.members for a in anchors)

    return clusters


def pick_representative(members: Sequence, scores: Optional[dict] = None):
    """The member whose verdict the rest of the cluster inherits.

    `group` already put it first — it is the anchor every other member was measured
    against, so nothing else can be chosen without breaking that guarantee. `scores`
    is accepted and ignored, for callers written against the old signature.
    """
    return members[0]
