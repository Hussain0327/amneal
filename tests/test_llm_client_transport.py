"""B3: the LLM SDK client is constructed with a bounded timeout + retry budget.

The OpenAI-compatible SDK's defaults (600s read timeout, 2 retries) can pin a
sync-route worker for ~10-20 min during a provider stall. The shared Databricks
client factory must pass explicit values through.
"""

from __future__ import annotations

import pytest

from regwatch.common import llm_clients


def test_shared_databricks_client_passes_bounded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    captured: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    llm_clients.shared_databricks_openai_client.cache_clear()
    try:
        llm_clients.shared_databricks_openai_client(
            "https://dbx.example/serving-endpoints",
            "token-a",
            timeout=42.0,
            max_retries=1,
        )
    finally:
        llm_clients.shared_databricks_openai_client.cache_clear()
    assert captured == {
        "api_key": "token-a",
        "base_url": "https://dbx.example/serving-endpoints",
        "timeout": 42.0,
        "max_retries": 1,
    }
