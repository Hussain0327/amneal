"""OpenAIEmbeddingProvider — mocked API, no network.

Covers the K6 contract: text-embedding-3-small at 1536 dims, batches of up
to 512 inputs, retry on 429/5xx (and only those) with full-jitter backoff,
and the provider interface staying identical to the local/echo providers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from regwatch.process.embedder import OpenAIEmbeddingProvider, get_embedding_provider

DIM = 1536


class _ApiError(Exception):
    """Stand-in for openai's status errors (carries `status_code`)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"api error {status_code}")
        self.status_code = status_code


class _FakeEmbeddingsApi:
    """Records calls; optionally raises queued errors before succeeding."""

    def __init__(self, dim: int = DIM, errors: list[Exception] | None = None) -> None:
        self.dim = dim
        self.errors = list(errors or [])
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        assert model == "text-embedding-3-small"
        self.calls.append(list(input))
        if self.errors:
            raise self.errors.pop(0)
        # Return data deliberately out of order; the provider must sort by index.
        data = [
            SimpleNamespace(index=i, embedding=[float(i)] * self.dim)
            for i in reversed(range(len(input)))
        ]
        return SimpleNamespace(data=data)


def _fake_client(api: _FakeEmbeddingsApi) -> SimpleNamespace:
    return SimpleNamespace(embeddings=api)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps are pointless against a fake API."""
    monkeypatch.setattr("regwatch.process.embedder.time.sleep", lambda _s: None)


def _record_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[float, float]], list[float]]:
    """Captures every jitter draw range and every sleep the retry loop makes.

    The stub returns each draw's own upper bound so the recorded sleeps are
    deterministic without a seeded RNG.

    Returns:
        (draws, slept): the (low, high) pairs passed to random.uniform, and
        the delays handed to time.sleep, in retry order.
    """
    draws: list[tuple[float, float]] = []
    slept: list[float] = []

    def fake_uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return high

    monkeypatch.setattr("regwatch.process.embedder.random.uniform", fake_uniform)
    monkeypatch.setattr("regwatch.process.embedder.time.sleep", slept.append)
    return draws, slept


def test_provider_interface() -> None:
    p = OpenAIEmbeddingProvider()
    assert p.name == "openai"
    assert p.dim == DIM
    assert p.model == "text-embedding-3-small"


def test_factory_returns_openai_provider() -> None:
    p = get_embedding_provider("openai")
    assert isinstance(p, OpenAIEmbeddingProvider)


def test_empty_input_no_api_call() -> None:
    api = _FakeEmbeddingsApi()
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    assert p.embed([]) == []
    assert api.calls == []


def test_embed_returns_vectors_in_input_order() -> None:
    api = _FakeEmbeddingsApi()
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    vecs = p.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == DIM for v in vecs)
    # The fake returns data reversed; sorting by index must restore order.
    assert [v[0] for v in vecs] == [0.0, 1.0, 2.0]


def test_batches_of_at_most_512() -> None:
    api = _FakeEmbeddingsApi()
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    texts = [f"t{i}" for i in range(1100)]
    vecs = p.embed(texts)
    assert len(vecs) == 1100
    assert [len(c) for c in api.calls] == [512, 512, 76]
    # Batch boundaries preserve input order.
    assert api.calls[0][0] == "t0"
    assert api.calls[2][-1] == "t1099"


def test_retries_on_429_then_succeeds() -> None:
    api = _FakeEmbeddingsApi(errors=[_ApiError(429)])
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    vecs = p.embed(["hello"])
    assert len(vecs) == 1
    assert len(api.calls) == 2  # one failure + one success


def test_retries_on_5xx_then_succeeds() -> None:
    api = _FakeEmbeddingsApi(errors=[_ApiError(500), _ApiError(503)])
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    vecs = p.embed(["hello"])
    assert len(vecs) == 1
    assert len(api.calls) == 3


def test_does_not_retry_4xx() -> None:
    api = _FakeEmbeddingsApi(errors=[_ApiError(400)])
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    with pytest.raises(_ApiError):
        p.embed(["hello"])
    assert len(api.calls) == 1


def test_gives_up_after_max_attempts() -> None:
    api = _FakeEmbeddingsApi(errors=[_ApiError(429)] * 10)
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    with pytest.raises(_ApiError):
        p.embed(["hello"])
    assert len(api.calls) == OpenAIEmbeddingProvider._max_attempts


def test_backoff_is_full_jitter_from_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Full jitter: every wait is drawn from [0, min(cap, base * 2**retry)], so
    # 429'd bulk-embed callers cannot re-collide on a shared delay floor.
    draws, slept = _record_backoff(monkeypatch)
    api = _FakeEmbeddingsApi(errors=[_ApiError(429)] * 5)
    p = OpenAIEmbeddingProvider(client=_fake_client(api))

    assert len(p.embed(["hello"])) == 1
    assert draws == [(0, 1.0), (0, 2.0), (0, 4.0), (0, 8.0), (0, 16.0)]
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_ceiling_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    draws, _slept = _record_backoff(monkeypatch)
    monkeypatch.setattr(OpenAIEmbeddingProvider, "_backoff_cap_s", 3.0)
    api = _FakeEmbeddingsApi(errors=[_ApiError(500)] * 5)
    p = OpenAIEmbeddingProvider(client=_fake_client(api))

    assert len(p.embed(["hello"])) == 1
    assert [high for _low, high in draws] == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_non_retryable_error_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    draws, slept = _record_backoff(monkeypatch)
    api = _FakeEmbeddingsApi(errors=[_ApiError(400)])
    p = OpenAIEmbeddingProvider(client=_fake_client(api))

    with pytest.raises(_ApiError):
        p.embed(["hello"])
    assert draws == []
    assert slept == []


def test_exhausted_retries_sleep_once_per_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    draws, _slept = _record_backoff(monkeypatch)
    api = _FakeEmbeddingsApi(errors=[_ApiError(429)] * 10)
    p = OpenAIEmbeddingProvider(client=_fake_client(api))

    with pytest.raises(_ApiError):
        p.embed(["hello"])
    # The last attempt raises instead of waiting on a retry that never comes.
    assert len(api.calls) == OpenAIEmbeddingProvider._max_attempts
    assert len(draws) == OpenAIEmbeddingProvider._max_attempts - 1


def test_dim_mismatch_raises() -> None:
    api = _FakeEmbeddingsApi(dim=384)
    p = OpenAIEmbeddingProvider(client=_fake_client(api))
    with pytest.raises(RuntimeError, match="expected 1536"):
        p.embed(["hello"])


def test_missing_api_key_fails_fast() -> None:
    # conftest blanks OPENAI_API_KEY; without an injected client the provider
    # must refuse rather than construct an unusable SDK client.
    p = OpenAIEmbeddingProvider()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        p.embed(["hello"])
