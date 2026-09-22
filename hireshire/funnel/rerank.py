from __future__ import annotations

import asyncio
import logging
import threading

from hireshire.funnel.config import RerankConfig
from hireshire.models.job import Job

logger = logging.getLogger(__name__)

# Process-wide cache of loaded cross-encoders, keyed by (model name, max_length,
# device). Loading one is expensive (weights + a torch graph), so every Reranker in
# the process shares one instance per configuration. max_length is part of the key
# because sentence-transformers bakes it into the model wrapper at construction,
# so two configs that differ only in length are genuinely different objects. The
# device is part of it so the OOM fallback's CPU copy never replaces — or mutates —
# the shared GPU model under another caller. Mirrors funnel/relevance.py.
_MODEL_CACHE: dict[tuple[str, int, str | None], object] = {}
_MODEL_LOCK = threading.Lock()


def describe_device(model) -> str:
    """`cuda:0 (NVIDIA GeForce RTX 3060 Laptop GPU)`, `mps`, `cpu` — for the log."""
    device = str(getattr(model, "device", "unknown"))
    if device.startswith("cuda"):
        try:
            import torch

            return f"{device} ({torch.cuda.get_device_name(torch.device(device))})"
        except Exception:
            pass
    return device


def _get_model(name: str, max_length: int, device: str | None = None):
    """Load (or reuse) a cross-encoder.

    `device=None` lets sentence-transformers pick — cuda, then mps, then cpu — so a
    GPU build of torch is all it takes to use the GPU. Precision is left at fp32 on
    every device: `RerankConfig.min_score` is a raw logit, and a half-precision model
    would shift it.
    """
    key = (name, max_length, device)
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            # Lazy import so torch is only required when reranking actually runs.
            # Importing this module must stay free.
            from sentence_transformers import CrossEncoder

            try:
                model = CrossEncoder(name, max_length=max_length, device=device)
            except Exception:
                if device is not None:
                    raise
                # A GPU that is present but unusable must not cost the sweep.
                logger.warning(
                    "Could not load cross-encoder %s on the default device; using CPU",
                    name, exc_info=True,
                )
                model = CrossEncoder(name, max_length=max_length, device="cpu")
            logger.info(
                "Loading cross-encoder %s (max_length=%d) on %s",
                name, max_length, describe_device(model),
            )
            _MODEL_CACHE[key] = model
        return model


def _is_oom(exc: BaseException) -> bool:
    """CUDA raises torch.OutOfMemoryError; MPS raises a plain RuntimeError."""
    try:
        import torch

        if isinstance(exc, torch.OutOfMemoryError):
            return True
    except (ImportError, AttributeError):
        pass
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()


def _free_gpu_memory() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


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
        # Shrinks on GPU out-of-memory and stays shrunk, so every later batch does not
        # hit the same OOM and halve again.
        self._batch_size = max(1, cfg.batch_size)
        self._on_cpu_fallback = False

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
        """Blocking predict (CPU- or GPU-bound) — call under asyncio.to_thread."""
        if not jobs:
            return []
        if self._model is None:
            self._model = _get_model(self._cfg.model, self._cfg.max_length)
        # Out of memory is a device accident, not a verdict: the same logits come out
        # at any batch size and on either device, so back off rather than fail the
        # batch and retire its jobs.
        while True:
            try:
                return self._predict(self._model, jobs, self._batch_size)
            except Exception as exc:
                if not _is_oom(exc) or self._on_cpu_fallback:
                    raise
                _free_gpu_memory()
                if self._batch_size > 1:
                    self._batch_size = max(1, self._batch_size // 2)
                    logger.warning(
                        "Reranker ran out of GPU memory; batch size now %d",
                        self._batch_size,
                    )
                    continue
                logger.warning(
                    "Reranker ran out of GPU memory at batch size 1; "
                    "using the CPU for the rest of this run"
                )
                self._model = _get_model(
                    self._cfg.model, self._cfg.max_length, device="cpu"
                )
                self._on_cpu_fallback = True
                self._batch_size = max(1, self._cfg.batch_size)

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
