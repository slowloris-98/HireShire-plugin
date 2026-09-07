from __future__ import annotations

import asyncio
import logging
import threading

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


# The value written to MatchResult.rerank_stage. One model now, so there is one
# stage; the column is kept because historical rows carry "wide" and "refined" and
# the reports still render them.
RERANK_STAGE = "single"


class Reranker:
    """Scores (candidate profile, job description) pairs with a cross-encoder.

    The query is the expanded "ideal candidate" profile generated at setup — not
    the raw resume. That profile spells out transferable skills in the vocabulary
    employers use ("component-based UI development" alongside "React"), which is
    what closes the gap when a genuinely good job is worded nothing like the
    resume.

    Scores are RAW LOGITS from one model against one profile. Ordinal within a run,
    and meaningless across models or users — which is why `RerankConfig.min_score`
    has to be calibrated rather than guessed, and why changing `model` invalidates
    whatever cutoff was calibrated for the old one.
    """

    def __init__(self, cfg: RerankConfig, profile: str):
        self._cfg = cfg
        self._profile = (profile or "").strip()
        self._model = None

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

    def _score(self, jobs: list[Job]) -> list[float]:
        """Blocking (CPU-bound) predict — call under asyncio.to_thread."""
        if not jobs:
            return []
        if self._model is None:
            self._model = _get_model(self._cfg.model, self._cfg.max_length)
        return self._predict(self._model, jobs, self._cfg.batch_size)

    async def rank(self, jobs: list[Job]) -> list[float]:
        """Return one logit per job, in input order.

        With reranking unusable every job scores 0.0. That is NOT a verdict, and
        callers must not apply `min_score` to it — `usable` is the flag to check.
        Treating an unusable reranker's zeros as scores would silently drop the
        whole sweep the moment the profile file went missing, which is the failure
        the setup skill already warns about."""
        if not jobs:
            return []
        if not self.usable:
            return [0.0] * len(jobs)
        return await asyncio.to_thread(self._score, jobs)
