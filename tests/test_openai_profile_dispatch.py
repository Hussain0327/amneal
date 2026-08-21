"""Offline contracts for OpenAI-only embedding-profile dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from regwatch.process.embedder import (
    OpenAIEmbeddingProvider,
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
    """An OpenAI profile must always use OpenAI embedding geometry."""
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _openai_settings)
    profile = _profile(_openai_spec())

    provider = get_embedding_provider_for_profile(profile)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.model == "text-embedding-3-large"
    assert provider.dim == DIM


def test_qwen_provider_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _openai_settings)

    with pytest.raises(ValueError, match="unknown embedding provider: qwen3"):
        get_embedding_provider("qwen3")


def test_qwen_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import regwatch.process.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "get_settings", _openai_settings)
    profile = _profile(_qwen_spec())

    with pytest.raises(ValueError, match="unsupported embedding profile provider"):
        get_embedding_provider_for_profile(profile)
