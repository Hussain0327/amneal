"""The local bge-small model must be loaded once per process, not per instance.

`get_embedding_provider()` returns a fresh `LocalBgeSmallProvider` per call, and
the ingest pipeline calls it once per listing. If the ~130 MB model were stored
as a per-instance attribute it would be reloaded from disk every time. The model
is a shared class variable, so constructing two providers must build the model
exactly once.
"""

from __future__ import annotations

import pytest

from regwatch.process.embedder import LocalBgeSmallProvider


class _FakeST:
    """Stand-in for sentence_transformers.SentenceTransformer.

    Counts construction so we can assert the model is built exactly once, and
    its ``encode`` returns an object exposing ``.tolist()`` (the code calls it).
    """

    instances = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).instances += 1

    def encode(self, texts: list[str], **kwargs: object) -> object:
        import numpy as np

        return np.zeros((len(texts), 384), dtype="float32")


def test_model_loaded_once_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reset shared state: the autouse conftest fixture does not touch this.
    LocalBgeSmallProvider._model = None
    LocalBgeSmallProvider._cache.clear()
    _FakeST.instances = 0

    monkeypatch.setattr("sentence_transformers.SentenceTransformer", _FakeST)

    try:
        # Distinct, cache-busting strings so both providers hit the model path.
        LocalBgeSmallProvider().embed(["first unique embedding input"])
        LocalBgeSmallProvider().embed(["second unique embedding input"])

        assert _FakeST.instances == 1
    finally:
        # Avoid leaking the fake model into other tests.
        LocalBgeSmallProvider._model = None
        LocalBgeSmallProvider._cache.clear()
