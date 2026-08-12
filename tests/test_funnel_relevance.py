"""Encoder relevance-gate logic (cosine + threshold), with a fake model so the test
needs no torch weights. Requires numpy (a transitive dep of the encoder stack)."""
from __future__ import annotations

import asyncio

import pytest

np = pytest.importorskip("numpy")

import hireshire.funnel.relevance as relevance_mod
from hireshire.funnel.config import EncoderConfig
from hireshire.funnel.relevance import EncoderRelevance

# 2-D unit vectors: "engineering" axis vs an unrelated axis.
_VECS = {
    "software engineer": [1.0, 0.0],
    "backend software engineer": [0.98, 0.2],
    "barista": [0.0, 1.0],
    "registered nurse": [0.1, 1.0],
}


class FakeModel:
    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
        rows = []
        for t in texts:
            v = np.array(_VECS.get(t.lower(), [0.0, 1.0]), dtype=float)
            n = np.linalg.norm(v)
            rows.append(v / n if n else v)
        return np.array(rows)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(relevance_mod, "_get_model", lambda name: FakeModel())


def _mask(cfg, titles):
    return asyncio.run(EncoderRelevance(cfg).relevant_mask(titles))


def test_swe_titles_pass_and_unrelated_fail():
    cfg = EncoderConfig(targets=["software engineer"], threshold=0.5)
    mask = _mask(cfg, ["backend software engineer", "barista", "registered nurse"])
    assert mask == [True, False, False]


def test_retargeting_flips_relevance():
    cfg = EncoderConfig(targets=["registered nurse"], threshold=0.5)
    mask = _mask(cfg, ["backend software engineer", "barista"])
    # Now the nurse-adjacent axis passes and the SWE title fails.
    assert mask == [False, True]


def test_no_targets_is_a_noop():
    cfg = EncoderConfig(targets=[])
    assert _mask(cfg, ["anything", "at all"]) == [True, True]


def test_empty_titles():
    cfg = EncoderConfig(targets=["software engineer"], threshold=0.5)
    assert _mask(cfg, []) == []
