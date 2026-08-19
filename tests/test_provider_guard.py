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
        embeddings=[[1.0] + [0.0] * 1535],
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


def test_real_providers_do_not_trip_the_echo_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Names only — the guard must not instantiate any model. Tested at the
    # guard function directly: a full qwen3 boot additionally requires a named
    # profile with complete coverage, which is separate machinery from what
    # this guard decides.
    from regwatch.api import main as api_main

    # Seed under conftest's echo env FIRST: the store's lazy dim assert runs on
    # first write, and qwen3-with-legacy would (correctly) refuse there. What
    # is under test is only the echo guard's decision on real provider NAMES.
    _seed_one_chunk()
    _reload_settings(
        monkeypatch,
        REGWATCH_ALLOW_TEST_PROVIDERS="0",
        EMBEDDING_PROVIDER="qwen3",
        LLM_PROVIDER="databricks",
    )
    api_main._guard_test_providers(cs.get_settings())  # must not raise


def test_echo_with_empty_corpus_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fresh checkout / pre-seed Docker boot: no corpus yet, echo is allowed
    # so the stack can become healthy and the ingest service can run.
    _reload_settings(monkeypatch, REGWATCH_ALLOW_TEST_PROVIDERS="0")
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["components"]["vector_store"]["corpus_count"] == 0
        assert body["warnings"]


def test_unset_embedding_provider_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-08-14 hazard: a worker with no EMBEDDING_PROVIDER must refuse.

    There is deliberately no default any more -- the silent local-bge fallback
    is what let 295 backfill documents pay fetch/parse/OCR before failing.
    """
    from regwatch.store import db as db_module

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="")
    db_module.reset_for_tests()  # Force init_db to re-run the provider assert.
    with pytest.raises(RuntimeError, match="EMBEDDING_PROVIDER is not set"), TestClient(app):
        pass
    db_module.reset_for_tests()


def test_unset_database_url_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DATABASE_URL half of the boot contract, pinned at the API lifespan.

    tests/test_smoke.py pins get_engine()'s refusal directly; this pins that
    the API startup path actually reaches it (lifespan -> init_db ->
    get_engine) instead of booting half-configured and failing on first use.
    Blank normalizes to unset, so this covers both.
    """
    from regwatch.store import db as db_module

    _reload_settings(monkeypatch, DATABASE_URL="")
    db_module.reset_for_tests()  # Drop the memoized engine from prior tests.
    with pytest.raises(RuntimeError, match="only datastore"), TestClient(app):
        pass
    db_module.reset_for_tests()


def test_retired_provider_name_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # local-bge-small and openai were removed 2026-08-17; a machine still
    # configured with one must refuse loudly, never fall back.
    from regwatch.store import db as db_module

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="local-bge-small")
    db_module.reset_for_tests()
    with pytest.raises(ValueError, match="unknown embedding provider"), TestClient(app):
        pass
    db_module.reset_for_tests()


def test_wrong_dim_provider_refuses_the_1536_dim_datastore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # K6: the datastore is Postgres with a vector(1536) chunk table, so a
    # non-Qwen provider with any other dimension must fail the dim assert at
    # boot rather than serve retrieval that can never write or match the
    # corpus. No such provider ships any more, so a stub stands in for a
    # future misconfigured one.
    from regwatch.store import db as db_module
    from regwatch.store import pgvector_store

    class _Stub384:
        name = "stub-384"
        dim = 384

    _reload_settings(monkeypatch)
    monkeypatch.setattr(pgvector_store, "get_embedding_provider", lambda: _Stub384())
    db_module.reset_for_tests()  # Force init_db to re-run the dim assert.
    with pytest.raises(RuntimeError, match="384"), TestClient(app):
        pass
    db_module.reset_for_tests()


def test_qwen3_in_legacy_arm_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Qwen writes named profiles; pointing it at the unversioned legacy column
    # would mix vector spaces, so the boot assert refuses the combination.
    from regwatch.store import db as db_module

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="qwen3")
    db_module.reset_for_tests()
    with pytest.raises(RuntimeError, match="legacy vector space"), TestClient(app):
        pass
    db_module.reset_for_tests()


def test_qwen3_without_endpoint_config_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.process.embedder import assert_embedding_runtime_available

    _reload_settings(
        monkeypatch,
        EMBEDDING_PROVIDER="qwen3",
        QWEN_EMBEDDING_BASE_URL="",
        QWEN_EMBEDDING_TOKEN="",
    )
    with pytest.raises(RuntimeError, match="QWEN_EMBEDDING_BASE_URL"):
        assert_embedding_runtime_available()


def test_unset_provider_refuses_at_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.process.embedder import get_embedding_provider

    _reload_settings(monkeypatch, EMBEDDING_PROVIDER="")
    with pytest.raises(RuntimeError, match="EMBEDDING_PROVIDER is not set"):
        get_embedding_provider()
