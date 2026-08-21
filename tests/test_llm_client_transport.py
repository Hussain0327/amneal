"""The OpenAI SDK client uses a bounded timeout and retry budget."""

from __future__ import annotations

import pytest

from regwatch.common import llm_clients


def test_shared_openai_client_passes_bounded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    captured: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    llm_clients.shared_openai_api_client.cache_clear()
    try:
        llm_clients.shared_openai_api_client(
            "https://api.openai.com/v1",
            "sk-test",
            timeout=42.0,
            max_retries=1,
        )
    finally:
        llm_clients.shared_openai_api_client.cache_clear()
    assert captured == {
        "api_key": "sk-test",
        "base_url": "https://api.openai.com/v1",
        "timeout": 42.0,
        "max_retries": 1,
    }
