"""Tests for the process-wide canonical-query embedding cache.

The cache memoizes module-level ``embed_query`` results so a repeated
canonical Ask query (deterministic follow-up rewrites re-embed the same
string) skips the embedding HTTP round-trip (issue #221). These tests pin the
contract: a hit skips the provider call, every geometry field participates in
the key, errors are never cached, the bound evicts least-recently-used
entries, callers cannot poison entries by mutating returned vectors, and
concurrent same-key callers may duplicate the embed but never receive a wrong
vector.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import SimpleNamespace

import config.settings as cs
import pytest

from regwatch.process import embedder as embedder_module
from regwatch.process.embedder import embed_query


def _deterministic_vector(query: str, dim: int) -> list[float]:
    """Per-query fingerprint vector so a cross-key cache hit is detectable."""
    return [
        float(len(query)),
        float(sum(ord(ch) for ch in query) % 1009),
        float(dim),
        0.5,
    ]


class _CountingProvider:
    """Qwen3-shaped fake exposing the full identity surface, counting calls."""

    name = "qwen3"

    def __init__(
        self,
        *,
        model: str = "qwen3-embedding-0-6b",
        dim: int = 4,
        query_instruction: str = "Retrieve regulatory evidence.",
        query_instruction_version: str = "v1",
        base_url: str = "https://endpoint.example/v1",
        fail_first: int = 0,
    ) -> None:
        self.model = model
        self.dim = dim
        self.query_instruction = query_instruction
        self.query_instruction_version = query_instruction_version
        self.base_url = base_url
        self.calls = 0
        self._fail_first = fail_first

    def embed_query(self, query: str) -> list[float]:
        self.calls += 1
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("simulated embedding endpoint failure")
        return _deterministic_vector(query, self.dim)


def test_repeat_query_embeds_once() -> None:
    provider = _CountingProvider()
    first = embed_query(provider, "why?")
    second = embed_query(provider, "why?")
    assert provider.calls == 1
    assert second == first == _deterministic_vector("why?", 4)


def test_fresh_provider_instance_with_same_geometry_hits() -> None:
    """retrieve() builds a NEW provider per call; the key must be value-based."""
    warm = _CountingProvider()
    embed_query(warm, "which PSG changed?")
    fresh = _CountingProvider()
    result = embed_query(fresh, "which PSG changed?")
    assert warm.calls == 1
    assert fresh.calls == 0
    assert result == _deterministic_vector("which PSG changed?", 4)


def test_changed_instruction_version_misses() -> None:
    v1 = _CountingProvider(query_instruction_version="v1")
    v2 = _CountingProvider(query_instruction_version="v2")
    embed_query(v1, "q")
    embed_query(v2, "q")
    assert v1.calls == 1
    assert v2.calls == 1


def test_changed_model_dim_instruction_or_base_url_misses() -> None:
    base = _CountingProvider()
    embed_query(base, "q")
    variants = [
        _CountingProvider(model="qwen3-embedding-8b"),
        _CountingProvider(dim=8),
        _CountingProvider(query_instruction="A different task instruction."),
        _CountingProvider(base_url="https://other.example/v1"),
    ]
    for variant in variants:
        embed_query(variant, "q")
        assert variant.calls == 1, "a changed geometry field must miss"
    assert base.calls == 1


def test_eviction_is_bounded_and_lru_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedder_module, "_QUERY_EMBEDDING_CACHE_MAX_SIZE", 2)
    provider = _CountingProvider()
    embed_query(provider, "q1")
    embed_query(provider, "q2")
    assert provider.calls == 2
    embed_query(provider, "q1")  # refresh q1's recency
    assert provider.calls == 2
    embed_query(provider, "q3")  # evicts q2, the least recently used
    assert provider.calls == 3
    embed_query(provider, "q1")  # survived eviction
    assert provider.calls == 3
    embed_query(provider, "q2")  # was evicted, embeds again
    assert provider.calls == 4


def test_error_is_never_cached() -> None:
    provider = _CountingProvider(fail_first=1)
    with pytest.raises(RuntimeError, match="simulated embedding endpoint"):
        embed_query(provider, "q")
    ok = embed_query(provider, "q")  # the retry reaches the provider
    assert provider.calls == 2
    again = embed_query(provider, "q")  # the SUCCESS was cached
    assert provider.calls == 2
    assert again == ok


def test_mutating_returned_vectors_cannot_poison_the_cache() -> None:
    provider = _CountingProvider()
    first = embed_query(provider, "q")
    pristine = list(first)
    first[0] = 12345.0  # mutate the miss-path return
    second = embed_query(provider, "q")
    assert second == pristine
    second[1] = -777.0  # mutate the hit-path return
    third = embed_query(provider, "q")
    assert third == pristine
    assert provider.calls == 1


def test_disabled_flag_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "regwatch.process.embedder.get_settings",
        lambda: SimpleNamespace(query_embedding_cache_enabled=False),
    )
    provider = _CountingProvider()
    embed_query(provider, "q")
    embed_query(provider, "q")
    assert provider.calls == 2


def test_settings_flag_defaults_on_with_regwatch_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = cs.Settings.model_fields["query_embedding_cache_enabled"]
    assert field.default is True
    assert field.validation_alias == "REGWATCH_QUERY_EMBED_CACHE"
    monkeypatch.setenv("REGWATCH_QUERY_EMBED_CACHE", "false")
    assert cs.Settings().query_embedding_cache_enabled is False


def test_partial_identity_and_legacy_providers_bypass_the_cache() -> None:
    """Only providers exposing their full geometry may share cache entries."""

    class _NoModelProvider:
        name = "mystery"
        dim = 4

        def __init__(self) -> None:
            self.calls = 0

        def embed_query(self, query: str) -> list[float]:
            self.calls += 1
            return [1.0, 2.0]

    class _LegacyEmbedOnlyProvider:
        name = "legacy"
        dim = 4

        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            return [[float(len(text))] for text in texts]

    partial = _NoModelProvider()
    embed_query(partial, "q")
    embed_query(partial, "q")
    assert partial.calls == 2

    legacy = _LegacyEmbedOnlyProvider()
    assert embed_query(legacy, "q") == [1.0]
    embed_query(legacy, "q")
    assert legacy.calls == 2


def _run_threads(worker: Callable[[int], None], count: int) -> None:
    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)


def test_concurrent_same_key_callers_never_get_a_wrong_vector() -> None:
    """16 threads, one key: correct vector always; duplicate embeds allowed.

    The provider call deliberately runs OUTSIDE the cache lock so a slow HTTP
    embed never serializes unrelated queries; several cold-start threads may
    therefore each embed the query once (the documented duplicate-embed
    allowance). What is forbidden is a wrong or torn vector.
    """
    thread_count = 16
    barrier = threading.Barrier(thread_count)
    provider = _CountingProvider()
    results: list[list[float] | None] = [None] * thread_count
    errors: list[Exception] = []

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[slot] = embed_query(provider, "same canonical query")
        except Exception as exc:  # capture, thread exceptions are silent
            errors.append(exc)

    _run_threads(worker, thread_count)
    assert not errors
    expected = _deterministic_vector("same canonical query", 4)
    assert all(result == expected for result in results)
    cold_calls = provider.calls
    assert 1 <= cold_calls <= thread_count
    embed_query(provider, "same canonical query")  # warm cache: no new call
    assert provider.calls == cold_calls


def test_concurrent_distinct_keys_each_get_their_own_vector() -> None:
    thread_count = 16
    queries = [f"canonical query number {index}" for index in range(4)]
    barrier = threading.Barrier(thread_count)
    provider = _CountingProvider()
    results: list[list[float] | None] = [None] * thread_count
    errors: list[Exception] = []

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[slot] = embed_query(provider, queries[slot % 4])
        except Exception as exc:  # capture, thread exceptions are silent
            errors.append(exc)

    _run_threads(worker, thread_count)
    assert not errors
    for slot in range(thread_count):
        assert results[slot] == _deterministic_vector(queries[slot % 4], 4)
