from __future__ import annotations

import asyncio
import logging
import threading

from hireshire.funnel.config import EncoderConfig

logger = logging.getLogger(__name__)

# Process-wide cache of loaded encoder models, keyed by model name. Loading a
# sentence-transformers model is expensive (weights + a torch graph), so every
# Funnel in the process shares one instance per model.
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = threading.Lock()


def _get_model(name: str):
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(name)
        if model is None:
            # Lazy import so the (heavy) dependency is only required when the funnel
            # is actually enabled.
            from sentence_transformers import SentenceTransformer

            logger.info("Loading encoder model %s", name)
            model = SentenceTransformer(name)
            _MODEL_CACHE[name] = model
        return model


class EncoderRelevance:
    """Scores job titles against configured target job-type anchors with a
    sentence-transformers encoder. Cosine similarity via normalized embeddings."""

    def __init__(self, cfg: EncoderConfig):
        self._cfg = cfg
        self._model = None
        self._target_emb = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            self._model = _get_model(self._cfg.model)
            self._target_emb = self._model.encode(
                self._cfg.targets,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )

    def _score_titles(self, titles: list[str]) -> list[float]:
        """Blocking (CPU-bound) encode + similarity — call under asyncio.to_thread."""
        if not titles:
            return []
        self._ensure_loaded()
        emb = self._model.encode(titles, normalize_embeddings=True, convert_to_numpy=True)
        # Both sides are L2-normalized, so the dot product is the cosine similarity.
        sims = emb @ self._target_emb.T  # (n_titles, n_targets)
        return sims.max(axis=1).tolist()

    async def score(self, titles: list[str]) -> list[float]:
        """Return the raw max-over-targets cosine per title.

        Exposed separately from `relevant_mask` because the score is worth keeping,
        not just the verdict it produces: it is the only number that explains why a
        title was dropped as `title_low_relevance`, and it used to be computed,
        compared to the threshold and thrown away.

        With no targets configured there is nothing to compare against, so every
        title scores 0.0 and the caller's gate is a no-op."""
        if not self._cfg.targets or not titles:
            return [0.0] * len(titles)
        return await asyncio.to_thread(self._score_titles, titles)

    async def relevant_mask(self, titles: list[str]) -> list[bool]:
        """Return a per-title boolean: True where max cos-sim to any target >= threshold.

        With no targets configured the gate is a no-op (all True) — callers that want
        a code-only fallback should check `cfg.targets` before using the encoder."""
        if not self._cfg.targets:
            return [True] * len(titles)
        if not titles:
            return []
        return [s >= self._cfg.threshold for s in await self.score(titles)]
