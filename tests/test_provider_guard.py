"""Lifespan fail-fast guard — echo providers must not face a real corpus.

Echo embeddings are hash noise: retrieval silently degrades while citations
still validate. The API must refuse to boot in that state unless the operator
opted in via REGWATCH_ALLOW_TEST_PROVIDERS=1 (which conftest sets globally).
"""

from __future__ import annotations

import config.settings as cs
import pytest
from fastapi.testclient import TestClient

from regwatch.api.main import app
from regwatch.store import vector_store


def _seed_one_chunk() -> None:
    vector_store.add_chunks(
        ids=["chunk-1"],
        embeddings=[[1.0] + [0.0] * 383],
        documents=["dissolution testing for albuterol"],
        metadatas=[{"doc_id": 1, "version_id": 1, "page": 1, "source_url": "file://x"}],
    )


def _reload_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cs.get_settings.cache_clear()


def test_echo_with_seeded_corpus_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_settings(monkeypatch, REGWATCH_ALLOW_TEST_PROVIDERS="0")
    _seed_one_chunk()
    with pytest.raises(RuntimeError, match="REGWATCH_ALLOW_TEST_PROVIDERS"), TestClient(app):
        pass


def test_echo_with_seeded_corpus_boots_with_override() -> None:
    # conftest sets REGWATCH_ALLOW_TEST_PROVIDERS=1 for the whole suite.
    _seed_one_chunk()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_real_providers_with_seeded_corpus_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Names only — the guard must not instantiate (and so download) any model.
    _reload_settings(
        monkeypatch,
        REGWATCH_ALLOW_TEST_PROVIDERS="0",
        EMBEDDING_PROVIDER="local-bge-small",
        LLM_PROVIDER="openai",
    )
    _seed_one_chunk()
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["components"]["embedding"] == {"provider": "local-bge-small"}
        assert body["components"]["llm"]["key_present"] is False
        assert "allow_test_providers" not in body


def test_echo_with_empty_corpus_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fresh checkout / pre-seed Docker boot: no corpus yet, echo is allowed
    # so the stack can become healthy and the ingest service can run.
    _reload_settings(monkeypatch, REGWATCH_ALLOW_TEST_PROVIDERS="0")
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["components"]["chroma"]["corpus_count"] == 0
        assert body["warnings"]


def test_local_bge_without_sentence_transformers_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slim image + local-bge-small must fail at boot, not 500 per request."""
    from regwatch.process import embedder

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="local-bge-small")
    monkeypatch.setattr(embedder, "_module_available", lambda module: False)
    with pytest.raises(RuntimeError, match="local-embeddings"), TestClient(app):
        pass


def test_local_bge_with_sentence_transformers_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe checks importability only — it must not load the model.
    from regwatch.process import embedder

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="local-bge-small")
    monkeypatch.setattr(embedder, "_module_available", lambda module: True)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_openai_embeddings_without_sdk_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.process import embedder

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="openai")
    monkeypatch.setattr(embedder, "_module_available", lambda module: False)
    with pytest.raises(RuntimeError, match="extra llm"), TestClient(app):
        pass
