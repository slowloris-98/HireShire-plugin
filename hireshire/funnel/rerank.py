from __future__ import annotations

import asyncio
import logging
import threading

from hireshire.funnel.config import RerankConfig
from hireshire.models.job import Job

logger = logging.getLogger(__name__)

# Process-wide cache of loaded cross-encoders, keyed by model name. Loading one is
# expensive (weights + a torch graph), so every Reranker in the process shares one
# instance per model. Mirrors funnel/relevance.py.
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = threading.Lock()


def _get_model(name: str):
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(name)
        if model is None:
            # Lazy import so torch is only required when reranking actually runs.
            # Importing this module must stay free.
            from sentence_transformers import CrossEncoder

            logger.info("Loading cross-encoder %s", name)
            model = CrossEncoder(name)
            _MODEL_CACHE[name] = model
        return model


class Reranker:
    """Scores (candidate profile, job description) pairs with a cross-encoder.

    The query is the expanded "ideal candidate" profile generated at setup — not
    the raw resume. That profile spells out transferable skills in the vocabulary
    employers use ("component-based UI development" alongside "React"), which is
    what closes the gap when a genuinely good job is worded nothing like the
    resume.

    Scores are ORDINAL ONLY. This model is trained on web-search relevance, not
    resume fit, so an absolute value means little — which is fine, because the
    gate downstream is top-K, never a fixed cutoff.
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

    def _score(self, jobs: list[Job]) -> list[float]:
        """Blocking (CPU-bound) predict — call under asyncio.to_thread."""
        if not jobs:
            return []
        if self._model is None:
            self._model = _get_model(self._cfg.model)
        pairs = [(self._profile, self._doc(j)) for j in jobs]
        scores = self._model.predict(pairs, batch_size=self._cfg.batch_size)
        return [float(s) for s in scores]

    async def rank(self, jobs: list[Job]) -> list[float]:
        """Return one score per job, in input order.

        With reranking unusable every job scores 0.0, which leaves the caller's
        ordering untouched and lets top-K fall back to arbitrary-but-bounded
        selection rather than silently dropping everything.
        """
        if not jobs:
            return []
        if not self.usable:
            return [0.0] * len(jobs)
        return await asyncio.to_thread(self._score, jobs)
