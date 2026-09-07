from __future__ import annotations

from pydantic import BaseModel, Field


class EncoderConfig(BaseModel):
    # A sentence-transformers model name (MiniLM by default). The key is configurable
    # so a lighter ONNX backend can be swapped in without code changes.
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Adjacent/synonymous job titles the candidate is qualified for, generated from
    # their resume at setup. Empty = the stage is a no-op that never loads torch.
    targets: list[str] = []
    # A title passes when its max cosine similarity to any target >= threshold.
    #
    # Deliberately low. This is a RECALL NET, not the decision: its only job is to
    # discard the obviously irrelevant cheaply so the reranker isn't handed tens of
    # thousands of documents. Precision belongs to the cross-encoder below.
    #
    # Resist tightening it to save money — it cannot. Both gates run locally and cost
    # nothing per job; only `rerank.min_score` decides what reaches the LLM. A
    # stricter threshold here buys CPU seconds and skipped detail fetches, and pays
    # for them in recall at the *dumbest* checkpoint in the funnel, since this stage
    # sees only the title. Raising it trades away exactly the differently-worded
    # matches the rerank stage exists to catch.
    #
    # It also does not transfer between users. Outside tech, titles are branded and
    # generic — Account Manager, Client Partner, Relationship Manager, Growth
    # Partner — so their similarity scores bunch into a narrow band and a threshold
    # tuned on engineering titles will either pass everything or nothing. Note too
    # that max-over-targets rises monotonically with the number of anchors, so a
    # bigger `targets` list loosens this gate further on its own.
    #
    # Bounded to the cosine range so the matcher's 0-100 relevance threshold cannot be
    # written here by mistake: 85 would validate as a float and silently reject every
    # job in the sweep.
    threshold: float = Field(0.25, ge=0.0, le=1.0)


class RerankConfig(BaseModel):
    """Cross-encoder rerank over full job descriptions.

    A cross-encoder reads query and document jointly with cross-attention, which is
    what lets it recognise that a cluster of words in a JD means the same thing as
    differently-worded experience on a resume — the property a bi-encoder loses when
    it squashes each side into an independent vector.

    ONE model, one scale. This used to be a two-stage cascade: a cheap 17M model read
    every candidate and a 68M model re-read the best few hundred. That existed only
    to make a global top-K affordable, and it cost a permanent hazard — the two
    stages emit different logit scales, so any code that sorted them together was
    silently wrong. With selection now a per-job cutoff (`min_score`) rather than a
    global ranking, the cheap pass has nothing to shortlist *for*, so the accurate
    model reads everything and the hazard is gone with it. Reranking is also spread
    across the sweep now rather than run in one block at the end, which is what pays
    for the extra CPU.

    The Ettin models are ModernBERT-based with an 8,192-token window, which is what
    makes `max_doc_chars` a cost dial rather than a capability limit: the longest
    job description measured in a real sweep was 25,386 chars / 2,693 tokens, so
    nothing in a corpus of this kind can overflow the window. The previous
    ms-marco-MiniLM model capped at 512 tokens and truncated 41% of descriptions
    before their requirements section, which made its ranking near-random
    (correlation with the eventual LLM score: +0.16).
    """

    enabled: bool = True
    # MTEB 0.5915 vs the 17M model's 0.5576, at ~3x the cost per pair.
    model: str = "cross-encoder/ettin-reranker-68m-v1"
    # The cutoff: a job is worth an LLM call when its rerank logit reaches this.
    #
    # THIS NUMBER IS PERSONAL. It is a raw cross-encoder logit against one user's
    # search profile — not a probability, not a percentage, not comparable between
    # two people or two models. Change `model` and it means nothing. Derive it from
    # a real run with `scripts/calibrate_cutoffs.py`, which reports what each
    # candidate cutoff would have passed and what it would have lost.
    #
    # 0.0 is the provisional shipped default: on the binary-relevance objective
    # these models are trained for it is the decision boundary (sigmoid 0.5), which
    # makes it defensible rather than arbitrary while a fresh install has no history
    # to calibrate against. It is deliberately permissive — `FunnelConfig.top_k` is
    # the fuse that bounds cost, not this. Set too high, a user gets zero jobs and no
    # error, which is why the per-stage counts in the matching report exist.
    min_score: float = 0.0
    # Job descriptions tokenise at ~5.06 chars/token, so this is ~2,960 tokens.
    # 15,000 covers 99.8% of real postings in full; only 5.8% exceed 10,000, so the
    # extra headroom is nearly free. Truncating the *head* is what broke the old
    # config — the median description does not reach its first requirements heading
    # until character 1,094.
    max_doc_chars: int = 15000
    # Safety bound on the combined query+document pair, not a working limit: at
    # 15,000 chars and the p10 ratio of 4.69 chars/token the worst pair is ~3,548
    # tokens, well inside this. It exists so one pathological posting cannot cost
    # 8,192 tokens' worth of compute.
    max_length: int = 4096
    # Smaller than the old wide pass's 16: batches are now one employer's postings
    # rather than the whole sweep, and the model is the larger of the two.
    batch_size: int = 8


class DetailFetchConfig(BaseModel):
    concurrency: int = 10   # max concurrent detail hydrations in flight
    jitter_s: float = 0.3   # random pre-fetch sleep to avoid bursting a tenant
    timeout_s: float = 20.0  # per-call httpx timeout for the detail client


class DedupeConfig(BaseModel):
    """Collapse repeated requisitions so one employer cannot eat the whole budget.

    Nothing is discarded: postings that share a company AND a normalised title are
    grouped, one representative is scored, and the score is copied back to every
    sibling. A single Townsquare Media requisition occupied 31 of 100 budget slots
    in a real sweep, and 12 EquipmentShare copies sat just below the cut.

    Descriptions are deliberately never compared — see funnel/cluster.py for why the
    key is this conservative.
    """

    enabled: bool = True


class FunnelConfig(BaseModel):
    enabled: bool = False
    encoder: EncoderConfig = Field(default_factory=EncoderConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    detail_fetch: DetailFetchConfig = Field(default_factory=DetailFetchConfig)

    # The maximum number of LLM calls one run may make. A FUSE, not the selection
    # mechanism — that is `rerank.min_score`.
    #
    # The distinction matters. This used to be the budget: candidates pooled for the
    # whole sweep, were ranked against each other, and the best `top_k` were scored.
    # Selection is now a per-job cutoff, which is what lets a job be judged the
    # moment it is scraped instead of after the sweep ends. But a cutoff has no
    # upper bound — a mis-set one could hand the judge thousands of jobs — so this
    # survives as the ceiling that makes that impossible.
    #
    # The name is kept deliberately. It is already in the `funnel` whitelist in
    # config_writer.py and in the setup skill's key table, and pydantic drops
    # unknown keys silently, so renaming it would quietly reset every existing
    # matcher.yaml to the default.
    #
    # On a Claude subscription the binding limit is prompts per 5-hour window,
    # shared with the user's own chat usage, not dollars. On a paid API key this is
    # a straight cost dial. 0 disables the cap (judge everything above the cutoff).
    top_k: int = 150
