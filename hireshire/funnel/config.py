from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


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
    # thousands of documents. Precision belongs to the cross-encoder below, and the
    # budget to top_k. Raising this trades away exactly the differently-worded
    # matches the rerank stage exists to catch. Note also that max-over-targets
    # rises monotonically with the number of anchors, so a bigger `targets` list
    # loosens this gate further on its own.
    #
    # Bounded to the cosine range so the matcher's 0-100 relevance threshold cannot be
    # written here by mistake: 85 would validate as a float and silently reject every
    # job in the sweep.
    threshold: float = Field(0.25, ge=0.0, le=1.0)


class RerankRefineConfig(BaseModel):
    """Second rerank pass over the best `depth` documents from the wide pass.

    A cascade, because cross-encoder cost is per-pair: the cheap 17M model can
    afford to read every one of ~7,000 descriptions, and the 68M model — which is
    3x slower but materially more accurate (MTEB 0.5915 vs 0.5576) — only has to
    read the few hundred that could still win the LLM budget.
    """

    enabled: bool = True
    model: str = "cross-encoder/ettin-reranker-68m-v1"
    # How many of the wide pass's best get re-scored. Must be >= funnel.top_k,
    # validated on FunnelConfig — see the note there.
    depth: int = 500
    batch_size: int = 8


class RerankConfig(BaseModel):
    """Cross-encoder rerank over full job descriptions.

    A cross-encoder reads query and document jointly with cross-attention, which is
    what lets it recognise that a cluster of words in a JD means the same thing as
    differently-worded experience on a resume — the property a bi-encoder loses when
    it squashes each side into an independent vector.

    Runs as a two-stage cascade: `model` scores every candidate, then `refine`
    re-scores the top `refine.depth`. The two stages emit DIFFERENT LOGIT SCALES and
    their scores must never be compared — see `Reranker.rank` and `RerankScores`.

    The Ettin models are ModernBERT-based with an 8,192-token window, which is what
    makes `max_doc_chars` a cost dial rather than a capability limit: the longest
    job description measured in a real sweep was 25,386 chars / 2,693 tokens, so
    nothing in a corpus of this kind can overflow the window. The previous
    ms-marco-MiniLM model capped at 512 tokens and truncated 41% of descriptions
    before their requirements section, which made its ranking near-random
    (correlation with the eventual LLM score: +0.16).
    """

    enabled: bool = True
    model: str = "cross-encoder/ettin-reranker-17m-v1"
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
    batch_size: int = 16
    refine: RerankRefineConfig = Field(default_factory=RerankRefineConfig)


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

    # How many jobs get an LLM score per run. This is the budget, and it is what
    # makes scoring feasible at scale: candidates are ranked by the cross-encoder
    # and only the top `top_k` are scored, so LLM cost is bounded by construction
    # rather than by wherever a similarity threshold happens to land.
    #
    # On a Claude subscription the binding limit is prompts per 5-hour window, not
    # dollars, so a few hundred is the realistic ceiling. On a paid API key this is
    # a straight cost dial. 0 disables the cap (score everything that passes).
    top_k: int = 100

    @model_validator(mode="after")
    def _refine_depth_covers_the_budget(self) -> "FunnelConfig":
        """`refine.depth` must be >= `top_k`, or the budget cannot be filled from
        refined scores alone.

        The two rerank stages emit incomparable logit scales, and the selection
        relies on every budget winner having been scored by the *same* (refined)
        model. If depth < top_k the tail of the budget would be filled by ordering
        wide-pass scores against refined ones — which is exactly the silent
        mis-ranking this cascade exists to avoid. Fail loudly at config load
        instead."""
        if not (self.rerank.enabled and self.rerank.refine.enabled):
            return self
        if self.top_k and self.rerank.refine.depth < self.top_k:
            raise ValueError(
                f"funnel.rerank.refine.depth ({self.rerank.refine.depth}) must be >= "
                f"funnel.top_k ({self.top_k}); otherwise the LLM budget would be "
                f"filled by comparing two rerank models' incomparable scores."
            )
        return self
