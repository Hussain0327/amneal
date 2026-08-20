"""Offline contract tests for the OpenAI embeddings provider.

Migration target: text-embedding-3-large at 1024 dims via Matryoshka
truncation (the `dimensions` request param). Unlike Qwen3, OpenAI has no
asymmetric query instruction -- prepending Qwen's "Instruct: ...\\nQuery:"
prefix to a query would poison retrieval geometry, not help it. OpenAI's raw
response vectors are also not guaranteed unit norm the way
Qwen3EmbeddingProvider's endpoint contractually is, so this provider must
normalize a valid non-unit-norm response instead of rejecting it, while
still refusing a zero or non-finite vector it cannot safely normalize. No
network calls, no database, no OPENAI_API_KEY required.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from regwatch.process.embedder import OpenAIEmbeddingProvider

DIM = 4


def _unit_vector(dim: int, coordinate: int = 0) -> list[float]:
    vector = [0.0] * dim
    vector[coordinate % dim] = 1.0
    return vector


class _FakeEmbeddingsApi:
    """Fake `client.embeddings` -- records every request, replays canned vectors."""

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        reverse_index: bool = False,
    ) -> None:
        self.vectors = vectors
        self.reverse_index = reverse_index
        self.calls: list[dict[str, Any]] = []

    def create(
        self, *, model: str, input: list[str], dimensions: int, **extra: Any
    ) -> SimpleNamespace:
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions, **extra})
        count = len(input)
        order = list(reversed(range(count))) if self.reverse_index else list(range(count))
        data = [
            SimpleNamespace(
                index=index,
                embedding=list(
                    self.vectors[index]
                    if self.vectors is not None
                    else _unit_vector(dimensions, index)
                ),
            )
            for index in order
        ]
        return SimpleNamespace(data=data)


def _fake_client(api: _FakeEmbeddingsApi) -> SimpleNamespace:
    return SimpleNamespace(embeddings=api)


def _provider(
    api: _FakeEmbeddingsApi,
    *,
    dim: int = DIM,
    batch_size: int = 256,
) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        client=_fake_client(api),
        model="text-embedding-3-large",
        dim=dim,
        batch_size=batch_size,
    )


def test_query_is_sent_raw_with_no_instruction_prefix() -> None:
    """Qwen's asymmetric "Instruct: ...\\nQuery:" prefix has no OpenAI
    analogue; carrying it over would corrupt, not help, retrieval geometry.
    """
    api = _FakeEmbeddingsApi()

    _provider(api).embed_query("washout period?")

    assert api.calls[-1]["input"] == ["washout period?"]


def test_dimensions_param_is_sent_on_every_request() -> None:
    api = _FakeEmbeddingsApi()

    _provider(api, dim=DIM).embed_documents(["a", "b"])

    assert api.calls[-1]["dimensions"] == DIM


def test_request_never_exceeds_the_configured_batch_cap() -> None:
    api = _FakeEmbeddingsApi()
    texts = [f"chunk {i}" for i in range(5)]

    _provider(api, batch_size=2).embed_documents(texts)

    assert all(len(call["input"]) <= 2 for call in api.calls)
    # Every input embedded exactly once across however many requests it took.
    assert sum(len(call["input"]) for call in api.calls) == len(texts)


def test_non_unit_norm_response_is_normalized_not_rejected() -> None:
    """OpenAI's raw response vectors are not unit norm; the provider must
    normalize a valid response rather than raise the way
    Qwen3EmbeddingProvider's strict unit-norm check would."""
    api = _FakeEmbeddingsApi(vectors=[[3.0, 4.0, 0.0, 0.0]])

    vector = _provider(api, dim=DIM).embed_documents(["text"])[0]

    norm = math.sqrt(math.fsum(value * value for value in vector))
    assert math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6)
    assert math.isclose(vector[0], 0.6, rel_tol=1e-6)
    assert math.isclose(vector[1], 0.8, rel_tol=1e-6)


def test_zero_vector_raises_rather_than_dividing_by_zero() -> None:
    api = _FakeEmbeddingsApi(vectors=[[0.0, 0.0, 0.0, 0.0]])

    with pytest.raises(RuntimeError):
        _provider(api, dim=DIM).embed_documents(["text"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_vector_raises(bad: float) -> None:
    api = _FakeEmbeddingsApi(vectors=[[bad, 0.0, 0.0, 0.0]])

    with pytest.raises(RuntimeError):
        _provider(api, dim=DIM).embed_documents(["text"])


def test_response_items_are_reassembled_by_index_not_arrival_order() -> None:
    """The endpoint may return items out of request order; consuming them
    positionally would silently swap which vector belongs to which chunk."""
    api = _FakeEmbeddingsApi(reverse_index=True)

    vectors = _provider(api, dim=DIM).embed_documents(["first", "second", "third"])

    assert vectors[0] == _unit_vector(DIM, 0)
    assert vectors[1] == _unit_vector(DIM, 1)
    assert vectors[2] == _unit_vector(DIM, 2)
