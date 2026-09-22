"""The reranker on a GPU: out of memory is a device accident, never a verdict.

No torch model is loaded. `_get_model` is replaced by fakes whose `predict` raises
the way CUDA and MPS do when memory runs out.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hireshire.funnel import rerank as rerank_mod
from hireshire.funnel.config import RerankConfig


def _jobs(n: int) -> list:
    # The reranker reads only these two fields of a Job.
    return [SimpleNamespace(title=f"Engineer {i}", content_text="Build things.")
            for i in range(n)]


class OomAbove:
    """Raises a CUDA-style OOM for any batch larger than `limit`."""

    def __init__(self, limit: int | None, device: str = "cuda:0"):
        self.limit = limit
        self.device = device
        self.batch_sizes: list[int] = []

    def predict(self, pairs, batch_size):
        self.batch_sizes.append(batch_size)
        if self.limit is None or batch_size > self.limit:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return [1.5] * len(pairs)


@pytest.fixture
def models(monkeypatch):
    loaded: dict[str | None, OomAbove] = {}

    def fake_get_model(name, max_length, device=None):
        return loaded[device]

    monkeypatch.setattr(rerank_mod, "_get_model", fake_get_model)
    return loaded


def _reranker(batch_size=8) -> rerank_mod.Reranker:
    return rerank_mod.Reranker(RerankConfig(batch_size=batch_size), profile="Engineer")


def test_oom_halves_the_batch_and_the_scores_still_come_back(models):
    models[None] = OomAbove(limit=2)
    r = _reranker(batch_size=8)
    assert r._score(_jobs(5)) == [1.5] * 5
    assert models[None].batch_sizes == [8, 4, 2]


def test_the_reduced_batch_persists_into_the_next_call(models):
    """Otherwise every batch of the sweep hits the same OOM and halves again."""
    models[None] = OomAbove(limit=2)
    r = _reranker(batch_size=8)
    r._score(_jobs(3))
    r._score(_jobs(3))
    assert models[None].batch_sizes == [8, 4, 2, 2]


def test_oom_at_batch_one_moves_to_a_cpu_copy_once(models):
    models[None] = OomAbove(limit=None)            # the GPU model never fits
    models["cpu"] = OomAbove(limit=99, device="cpu")
    r = _reranker(batch_size=4)
    assert r._score(_jobs(2)) == [1.5, 1.5]
    assert r._score(_jobs(2)) == [1.5, 1.5]
    assert models[None].batch_sizes == [4, 2, 1]   # the GPU is not retried
    assert models["cpu"].batch_sizes == [4, 4]     # full batch size back on the CPU


def test_an_oom_on_the_cpu_copy_is_raised(models):
    models[None] = OomAbove(limit=None)
    models["cpu"] = OomAbove(limit=None, device="cpu")
    with pytest.raises(RuntimeError, match="out of memory"):
        _reranker(batch_size=1)._score(_jobs(1))


def test_other_errors_are_not_swallowed(models):
    class Broken:
        device = "cuda:0"

        def predict(self, pairs, batch_size):
            raise ValueError("tokenizer exploded")

    models[None] = Broken()
    with pytest.raises(ValueError):
        _reranker()._score(_jobs(1))


def test_mps_style_oom_is_recognised():
    assert rerank_mod._is_oom(RuntimeError("MPS backend out of memory (MPS allocated: 5 GB)"))
    assert not rerank_mod._is_oom(RuntimeError("shape mismatch"))


def test_a_failed_default_load_falls_back_to_cpu(monkeypatch):
    """A GPU that is present but unusable must not cost the sweep."""
    import sys
    import types

    built: list[str | None] = []

    class FakeCrossEncoder:
        def __init__(self, name, max_length, device=None):
            built.append(device)
            if device is None:
                raise RuntimeError("CUDA driver initialization failed")
            self.device = device

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setattr(rerank_mod, "_MODEL_CACHE", {})

    model = rerank_mod._get_model("some/model", 512)
    assert built == [None, "cpu"]
    assert model.device == "cpu"
    # Cached under the default slot, so the next caller does not retry the GPU.
    assert rerank_mod._get_model("some/model", 512) is model
