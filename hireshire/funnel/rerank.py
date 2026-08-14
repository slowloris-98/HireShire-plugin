from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

from hireshire.funnel.config import RerankConfig
from hireshire.models.job import Job

logger = logging.getLogger(__name__)

# Process-wide cache of loaded cross-encoders, keyed by (model name, max_length).
# Loading one is expensive (weights + a torch graph), so every Reranker in the
# process shares one instance per configuration. max_length is part of the key
# because sentence-transformers bakes it into the model wrapper at construction,
# so two configs that differ only in length are genuinely different objects.
# Mirrors funnel/relevance.py.
_MODEL_CACHE: dict[tuple[str, int], object] = {}
_MODEL_LOCK = threading.Lock()


def _get_model(name: str, max_length: int):
    key = (name, max_length)
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            # Lazy import so torch is only required when reranking actually runs.
            # Importing this module must stay free.
            from sentence_transformers import CrossEncoder

            logger.info("Loading cross-encoder %s (max_length=%d)", name, max_length)
            model = CrossEncoder(name, max_length=max_length)
            _MODEL_CACHE[key] = model
        return model


@dataclass(frozen=True)
class RerankScores:
    """One job's rerank outcome, keeping the two stages' scores apart.

    `wide` and `refined` come from DIFFERENT MODELS and are therefore on different
    logit scales. Comparing or averaging them is meaningless, and sorting a mixed
    list of them silently produces a wrong ranking — the same class of invisible
    failure that made the previous single-stage reranker useless.

    `sort_key` is the only sanctioned way to order jobs by these numbers. It ranks
    every refined job above every unrefined one, then breaks ties *within* a single
    model's scale. That is sound rather than arbitrary because the refined set is by
    construction the wide pass's own top `depth`, and `depth >= top_k` is enforced
    in FunnelConfig — so everything still in contention for the budget has been
    scored by the same model.
    """

    wide: float
    refined: float | None = None

    @property
    def is_refined(self) -> bool:
        return self.refined is not None

    @property
    def stage(self) -> str:
        return "refined" if self.is_refined else "wide"

    @property
    def best(self) -> float:
        """The score from whichever stage last judged this job."""
        return self.refined if self.refined is not None else self.wide

    @property
    def sort_key(self) -> tuple[int, float]:
        return (1 if self.is_refined else 0, self.best)


class Reranker:
    """Scores (candidate profile, job description) pairs with a cross-encoder cascade.

    The query is the expanded "ideal candidate" profile generated at setup — not
    the raw resume. That profile spells out transferable skills in the vocabulary
    employers use ("component-based UI development" alongside "React"), which is
    what closes the gap when a genuinely good job is worded nothing like the
    resume.

    Scores are ORDINAL ONLY. These models are trained on retrieval relevance, not
    resume fit, so an absolute value means little — which is fine, because the gate
    downstream is top-K, never a fixed cutoff.
    """

    def __init__(self, cfg: RerankConfig, profile: str):
        self._cfg = cfg
        self._profile = (profile or "").strip()
        self._model = None
        self._refine_model = None

    @property
    def usable(self) -> bool:
        """Reranking needs both a switch and a query. Without a profile there is
        nothing to compare against, so the caller should fall back to scoring
        everything that passed the cheap gates."""
        return self._cfg.enabled and bool(self._profile)

    def _doc(self, job: Job) -> str:
        # Title first: it survives truncation and carries real signal.
        body = (job.content_text or "")[: self._cfg.max_doc_chars]
        return f"{job.title}\n\n{body}".strip()

    def _predict(self, model, jobs: list[Job], batch_size: int) -> list[float]:
        pairs = [(self._profile, self._doc(j)) for j in jobs]
        scores = model.predict(pairs, batch_size=batch_size)
        return [float(s) for s in scores]

    def _score(self, jobs: list[Job]) -> list[RerankScores]:
        """Blocking (CPU-bound) predict — call under asyncio.to_thread."""
        if not jobs:
            return []

        if self._model is None:
            self._model = _get_model(self._cfg.model, self._cfg.max_length)
        wide = self._predict(self._model, jobs, self._cfg.batch_size)
        out = [RerankScores(wide=w) for w in wide]

        refine = self._cfg.refine
        if not refine.enabled or not refine.depth:
            return out

        # Re-score only the wide pass's best `depth`. Indices, not objects, so the
        # refined scores land back on the right jobs in the caller's input order.
        order = sorted(range(len(jobs)), key=lambda i: wide[i], reverse=True)
        chosen = order[: refine.depth]
        if not chosen:
            return out

        if self._refine_model is None:
            self._refine_model = _get_model(refine.model, self._cfg.max_length)
        logger.info(
            "Rerank: refining the top %d of %d with %s",
            len(chosen), len(jobs), refine.model,
        )
        refined = self._predict(
            self._refine_model, [jobs[i] for i in chosen], refine.batch_size
        )
        for i, score in zip(chosen, refined):
            out[i] = RerankScores(wide=wide[i], refined=score)
        return out

    async def rank(self, jobs: list[Job]) -> list[RerankScores]:
        """Return one RerankScores per job, in input order.

        With reranking unusable every job scores 0.0 on the wide stage and none are
        refined, which leaves the caller's ordering untouched and lets top-K fall
        back to arbitrary-but-bounded selection rather than silently dropping
        everything.
        """
        if not jobs:
            return []
        if not self.usable:
            return [RerankScores(wide=0.0)] * len(jobs)
        return await asyncio.to_thread(self._score, jobs)
