"""Offline contract tests for provider-aware embedding-profile dispatch.

Qwen3@1024 and OpenAI text-embedding-3-large@1024 (Matryoshka-truncated)
share a dimension. Today get_embedding_provider_for_profile
(embedder.py:670-725) ignores profile.provider entirely and always builds a
Qwen3EmbeddingProvider (:687-688) -- widening the provider allowlist alone
does nothing, because that call is hard-coded to "qwen3". A profile-provider
dispatch bug of that shape produces silently-wrong vectors with no error
anywhere downstream: same shape, same store constraints. These tests pin the
dispatch on both the concrete provider class AND the model name, in both
directions, so a regression back to "always Qwen3" -- or a new "always
OpenAI" bug -- fails here first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from regwatch.process.embedder import (
    OpenAIEmbeddingProvider,
    Qwen3EmbeddingProvider,
    get_embedding_provider,
    get_embedding_provider_for_profile,
)
from regwatch.store.embedding_profiles import EmbeddingProfile, EmbeddingProfileSpec

DIM = 1024


def _openai_spec(
    *, model: str = "text-embedding-3-large", dimension: int = DIM
) -> EmbeddingProfileSpec:
    return EmbeddingProfileSpec(
        provider="openai",
        model=model,
        revision="text-embedding-3-large",
        dimension=dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version="none",
        preprocessing_version="text-v1",
        chunking_version="page-window-1000-v1",
        serving_runtime_version="openai-api-v1",
    )


def _qwen_spec(*, dimension: int = DIM) -> EmbeddingProfileSpec:
    return EmbeddingProfileSpec(
        provider="databricks",
        model="qwen3-embedding-0-6b",
        revision="0123456789abcdef",
        dimension=dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version="regwatch-regulatory-retrieval-v1",
        preprocessing_version="raw-text-v1",
        chunking_version="page-window-1000-v1",
        serving_runtime_version="vllm-0.19.0",
    )


def _profile(spec: EmbeddingProfileSpec) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id=spec.profile_id,
        fingerprint=spec.fingerprint,
        created_at=datetime.now(UTC),
        **{key: value for key, value in spec.__dict__.items()},
    )


def _openai_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "embedding_provider": "openai",
        "openai_api_key": "sk-test",
        "openai_base_url": "https://api.openai.com/v1",
        "openai_embedding_model": "text-embedding-3-large",
        "openai_embedding_dimension": DIM,
        "openai_embedding_batch_size": 256,
        "openai_timeout_s": 60.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _qwen_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "embedding_provider": "qwen3",
        "qwen_embedding_base_url": "https://workspace.example/serving-endpoints",
        "qwen_embedding_token": "dapi-token",
        "qwen_embedding_model": "qwen3-embedding-0-6b",
        "qwen_embedding_dimension": DIM,
        "qwen_embedding_query_instruction": "Retrieve the supporting PSG evidence",
        "qwen_embedding_query_instruction_version": "regwatch-regulatory-retrieval-v1",
        "qwen_embedding_batch_size": 8,
        "qwen_embedding_request_token_budget": 3000,
        "qwen_embedding_revision": "0123456789abcdef",
        "llm_timeout_s": 60.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_get_embedding_provider_openai_builds_the_openai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _openai_settings)

    provider = get_embedding_provider("openai")

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.model == "text-embedding-3-large"
    assert provider.dim == DIM


def test_profile_dispatch_returns_openai_provider_for_an_openai_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MOST IMPORTANT case: get_embedding_provider_for_profile must build
    an OpenAIEmbeddingProvider for a profile whose provider is "openai", not
    silently fall through to the Qwen3 branch it is hard-coded to today."""
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _openai_settings)
    profile = _profile(_openai_spec())

    provider = get_embedding_provider_for_profile(profile)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert not isinstance(provider, Qwen3EmbeddingProvider)
    assert provider.model == "text-embedding-3-large"
    assert provider.dim == DIM


def test_profile_dispatch_still_returns_qwen3_for_a_qwen_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard on the other direction: widening dispatch to support
    OpenAI must not make it default to OpenAI for the still-live Qwen
    profile, and must not silently swap in the OpenAI model name."""
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _qwen_settings)
    profile = _profile(_qwen_spec())

    provider = get_embedding_provider_for_profile(profile)

    assert isinstance(provider, Qwen3EmbeddingProvider)
    assert not isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.model == "qwen3-embedding-0-6b"
