"""FastAPI surface tests — every endpoint reachable, schema correct."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from regwatch.api.main import app


def _client() -> TestClient:
    # `with TestClient(app)` triggers lifespan → init_db on the per-test DB.
    return TestClient(app)


def _open() -> TestClient:
    c = TestClient(app)
    c.__enter__()
    return c


def test_health() -> None:
    r = _open().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    components = body["components"]
    assert components["db"] == {"ok": True}
    assert components["chroma"]["ok"] is True
    assert components["chroma"]["corpus_count"] == 0
    assert components["llm"] == {"provider": "echo", "key_present": True}
    assert components["embedding"] == {"provider": "echo"}
    assert body["allow_test_providers"] is True  # conftest opt-in
    assert any("empty" in w for w in body["warnings"])
    assert any("echo" in w for w in body["warnings"])


def test_health_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirror the /settings discipline: key PRESENCE only, never a value.
    import config.settings as cs

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-do-not-leak")
    cs.get_settings.cache_clear()
    r = _open().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["components"]["llm"] == {"provider": "openai", "key_present": True}
    assert "sk-test-secret-do-not-leak" not in r.text
    assert "openai_api_key" not in r.text


def test_health_unhealthy_when_chroma_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.api import main as api_main

    def _down() -> int:
        raise RuntimeError("chroma down")

    monkeypatch.setattr(api_main, "collection_size", _down)
    r = _open().get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["chroma"]["ok"] is False


def test_health_unhealthy_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.api import main as api_main

    def _down() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(api_main, "get_engine", _down)
    r = _open().get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["db"]["ok"] is False


def test_settings_no_secrets() -> None:
    r = _open().get("/settings")
    assert r.status_code == 200
    body = r.json()
    assert "embedding_provider" in body
    assert "openai_api_key" not in body
    assert "anthropic_api_key" not in body


def test_query_refuses_on_empty_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty Chroma → refusal; no LLM should be called.
    def _bad_llm(*a: object, **k: object) -> None:
        raise AssertionError("LLM must not be called when retrieval is empty")

    from regwatch.generate import grounded_qa as qa_mod

    monkeypatch.setattr(qa_mod, "get_llm_provider", _bad_llm)
    r = _open().post("/query", json={"question": "Does this exist?"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert body["citations"] == []
    assert body["session_id"]
    assert body["turn_id"]

    r2 = _open().post(
        "/query",
        json={"question": "What about dissolution?", "session_id": body["session_id"]},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["session_id"] == body["session_id"]
    assert body2["turn_id"] != body["turn_id"]


def test_query_rejects_zero_k() -> None:
    r = _open().post("/query", json={"question": "Does this exist?", "k": 0})
    assert r.status_code == 422


def test_sources_search_accepts_explicit_source_without_network() -> None:
    r = _open().post(
        "/sources/search",
        json={"query_text": "show PSG rows", "sources": ["psg"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["routed_sources"] == ["psg"]
    assert body["records"] == []


def test_create_product_rejects_bad_source() -> None:
    r = _open().post(
        "/products",
        json={"active_ingredient": "Foo", "source": "model_memory"},
    )
    assert r.status_code == 422


def test_create_and_list_product() -> None:
    c = _open()
    c.post(
        "/products",
        json={
            "active_ingredient": "Romidepsin",
            "dosage_form": "Injection",
            "route": "Intravenous",
            "rld_name": "Istodax",
            "rld_application_number": "208574",
            "company_status": "approved",
            "source": "anda_letter",
            "source_url": "file://internal/approval.pdf",
        },
    )
    listing = c.get("/products").json()
    assert listing["count"] >= 1
    assert any(p["active_ingredient"] == "Romidepsin" for p in listing["products"])


def test_watch_latest_returns_empty_when_no_alerts() -> None:
    r = _open().get("/watch/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["alerts"] == []


def test_watch_latest_rejects_invalid_since() -> None:
    r = _open().get("/watch/latest", params={"since": "not-a-date"})
    assert r.status_code == 422


def test_assemble_refuses_when_no_matching_psg() -> None:
    r = _open().post(
        "/assemble",
        json={"active_ingredient": "Imaginary Drug XYZ"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert "No PSG" in body["markdown"]
