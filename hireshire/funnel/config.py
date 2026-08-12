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
    # thousands of documents. Precision belongs to the cross-encoder below, and the
    # budget to top_k. Raising this trades away exactly the differently-worded
    # matches the rerank stage exists to catch. Note also that max-over-targets
    # rises monotonically with the number of anchors, so a bigger `targets` list
    # loosens this gate further on its own.
    threshold: float = 0.25


class RerankConfig(BaseModel):
    """Cross-encoder rerank over full job descriptions.

    A cross-encoder reads query and document jointly with cross-attention, which is
    what lets it recognise that a cluster of words in a JD means the same thing as
    differently-worded experience on a resume — the property a bi-encoder loses when
    it squashes each side into an independent vector.

    Affordable here because we rerank a few thousand in-memory candidates rather
    than retrieving from millions: ~22M params, roughly 1-3 ms/pair on CPU.
    """

    enabled: bool = True
    model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Cross-encoders cap the *combined* query+document pair (512 tokens for this
    # model), so the JD is truncated to its head — which is where requirements
    # usually sit. Chunking would multiply cost for a marginal gain.
    max_doc_chars: int = 1200
    batch_size: int = 32


class DetailFetchConfig(BaseModel):
    concurrency: int = 10   # max concurrent detail hydrations in flight
    jitter_s: float = 0.3   # random pre-fetch sleep to avoid bursting a tenant
    timeout_s: float = 20.0  # per-call httpx timeout for the detail client


class FunnelConfig(BaseModel):
    enabled: bool = False
    encoder: EncoderConfig = Field(default_factory=EncoderConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
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
