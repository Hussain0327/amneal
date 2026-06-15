"""B3: LLM SDK clients are constructed with a bounded timeout + retry budget.

The OpenAI/Anthropic SDK defaults (600s read timeout, 2 retries) can pin a
sync-route worker for ~10-20 min during a provider stall. The shared client
factory must pass explicit values through, and the embedder — which owns its own
retry loop — must construct with max_retries=0 so SDK retries don't stack on top
of it (6 manual attempts x 2 SDK retries = 12 calls per embed).
"""

from __future__ import annotations

import config.settings as cs
import pytest

from regwatch.common import llm_clients


def test_shared_openai_client_passes_bounded_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    openai = pytest.importorskip("openai")
    captured: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    llm_clients.shared_openai_client.cache_clear()
    try:
        llm_clients.shared_openai_client("key-a", timeout=42.0, max_retries=1)
    finally:
        llm_clients.shared_openai_client.cache_clear()
    assert captured == {"api_key": "key-a", "timeout": 42.0, "max_retries": 1}


def test_shared_anthropic_client_passes_bounded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anthropic = pytest.importorskip("anthropic")
    captured: dict[str, object] = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    llm_clients.shared_anthropic_client.cache_clear()
    try:
        llm_clients.shared_anthropic_client("key-b", timeout=15.0, max_retries=2)
    finally:
        llm_clients.shared_anthropic_client.cache_clear()
    assert captured == {"api_key": "key-b", "timeout": 15.0, "max_retries": 2}


def test_embedder_constructs_client_without_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedder's own retry loop owns retries — the SDK client must not also
    retry, or attempts stack."""
    monkeypatch.setenv("OPENAI_API_KEY", "key-c")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()

    captured: dict[str, object] = {}

    def _fake(api_key: str | None, *, timeout: float, max_retries: int) -> object:
        captured.update(api_key=api_key, timeout=timeout, max_retries=max_retries)
        return object()

    monkeypatch.setattr(llm_clients, "shared_openai_client", _fake)
    from regwatch.process.embedder import OpenAIEmbeddingProvider

    OpenAIEmbeddingProvider()._client_or_create()
    assert captured["api_key"] == "key-c"
    assert captured["max_retries"] == 0
