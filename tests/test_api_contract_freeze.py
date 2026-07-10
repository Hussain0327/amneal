"""Wire-shape freeze for the routes that gained response_models.

A response_model VALIDATES and STRIPS: exactly the phantom-field bug class
where a field the frontend reads silently vanishes from the wire. These tests
pin, per route, the EXACT key sets the handlers emitted before the models
existed, the passthrough-verbatim payloads (stored session citations), and the
conditional key-presence contracts (/health, /ready) that
response_model_exclude_none must reproduce -- they fail if a model drops,
renames, or null-fills anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from regwatch.api.main import app
from regwatch.store.db import session_scope
from regwatch.store.models import Alert
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources
from tests.conftest import create_user, login_client

ALERT_WIRE_KEYS = {
    "product_id",
    "active_ingredient",
    "listing_appl_no",
    "listing_psg_type",
    "psg_document_id",
    "psg_version_id",
    "captured_at",
    "diff_summary",
    "confidence",
    "rationale",
    "source_url",
    "change_kind",
}

LAST_RUN_WIRE_KEYS = {
    "started_at",
    "finished_at",
    "listings",
    "matched",
    "added",
    "revised",
    "unchanged",
    "errors",
    "alerts",
}

PRODUCT_WIRE_KEYS = {
    "id",
    "active_ingredient",
    "normalized_name",
    "stripped_name",
    "dosage_form",
    "route",
    "rld_name",
    "rld_application_number",
    "company_status",
    "source",
    "source_url",
}

MESSAGE_WIRE_KEYS = {
    "id",
    "turn_id",
    "role",
    "content",
    "status",
    "citations",
    "audit_id",
    "reason",
    "interpretation",
    "clarify",
    "related",
    "created_at",
}

SPINE_WIRE_KEYS = {
    "application_number",
    "application_type",
    "ingredient",
    "normalized_name",
    "product_numbers",
    "setid",
    "spl_candidates",
    "warnings",
}


def test_watch_latest_alert_and_last_run_keys_exact(auth_client: TestClient) -> None:
    from regwatch.watch.runs import record_watch_run

    with session_scope() as s:
        s.add(
            Alert(
                product_id=1,
                active_ingredient="Albuterol Sulfate",
                listing_appl_no="020503",
                listing_psg_type="final",
                psg_document_id=1,
                psg_version_id=1,
                captured_at="2026-06-01T00:00:00",
                diff_summary=None,
                confidence=1.0,
                rationale="canonical",
                source_url="http://example/020503.pdf",
                created_at=datetime.now(UTC),
            )
        )
    record_watch_run(
        started_at=datetime(2026, 7, 1, 7, 17, 0),
        finished_at=datetime(2026, 7, 1, 7, 21, 0),
        listings=1,
        matched=1,
        added=1,
        revised=0,
        unchanged=0,
        errors=0,
        alerts=1,
        digest_date="2026-07-01",
    )
    body = auth_client.get("/watch/latest").json()
    assert set(body) == {"count", "total", "limit", "offset", "alerts", "last_run"}
    alert = body["alerts"][0]
    assert set(alert) == ALERT_WIRE_KEYS
    # A null diff_summary stays a PRESENT key (the model must not exclude_none
    # here) and change_kind stays the closed structural set.
    assert alert["diff_summary"] is None
    assert alert["change_kind"] in {"new", "revised"}
    assert set(body["last_run"]) == LAST_RUN_WIRE_KEYS


def test_products_create_delete_wire_keys_and_added_count(auth_client: TestClient) -> None:
    payload = {"active_ingredient": "Romidepsin", "source": "manual"}
    r = auth_client.post("/products", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"added", "products"}
    # `added` is the upsert's inserted-row COUNT (int), not a bool -- coercing
    # it would flip the wire from 1 to true.
    assert body["added"] == 1 and not isinstance(body["added"], bool)
    product = body["products"][0]
    assert set(product) == PRODUCT_WIRE_KEYS

    # Re-adding the same identity merges instead of inserting: added == 0.
    again = auth_client.post("/products", json=payload)
    assert again.status_code == 201
    assert again.json()["added"] == 0 and not isinstance(again.json()["added"], bool)

    listing = auth_client.get("/products").json()
    assert set(listing) == {"count", "products"}
    assert set(listing["products"][0]) == PRODUCT_WIRE_KEYS

    removed = auth_client.delete(f"/products/{product['id']}")
    assert removed.status_code == 200
    assert set(removed.json()) == {"removed", "products"}
    assert removed.json()["removed"] is True


def test_settings_wire_keys_exact(auth_client: TestClient) -> None:
    body = auth_client.get("/settings").json()
    assert set(body) == {
        "embedding_provider",
        "llm_provider",
        "llm_model",
        "retrieval_top_k",
        "refusal_score_threshold",
        "company_name",
    }
    # Default settings ship retrieval_top_k=None: the key must stay PRESENT
    # as null (required-nullable), not vanish.
    assert "retrieval_top_k" in body


def test_sessions_wire_keys_and_stored_payload_passthrough() -> None:
    """Stored citations/clarify/related must round-trip VERBATIM through the
    response model -- older sessions carry legacy keys (e.g. ``source_url`` in
    clarify filters) that no current wire type declares, and a nested model
    would silently strip them."""
    from regwatch.common.conversation import ensure_session, record_message

    user_id = create_user()
    client = login_client()
    try:
        session_id = ensure_session(user_id=str(user_id))
        legacy_citation = {
            "short_name": "PSG_020503",
            "page": 4,
            "chunk_id": "020503-4",
            "doc_id": 1,
            "version_id": 10,
            "source_url": "http://example/PSG_020503.pdf",
            "snippet": "fasting single-dose crossover",
            # A key no current wire type declares: it must survive verbatim.
            "legacy_extra": "kept",
        }
        legacy_clarify = [
            {"label": "Tablet", "query": "albuterol tablet", "filters": {"source_url": "legacy"}}
        ]
        record_message(
            session_id=session_id,
            turn_id="t-legacy",
            role="assistant",
            content="A fasting study is recommended [PSG_020503, p.4].",
            status="answer",
            citations=[legacy_citation],
            clarify=legacy_clarify,
        )

        listing = client.get("/sessions")
        assert listing.status_code == 200, listing.text
        summary = listing.json()["sessions"][0]
        assert set(summary) == {"id", "title", "created_at", "updated_at", "message_count"}

        got = client.get(f"/sessions/{session_id}")
        assert got.status_code == 200, got.text
        detail = got.json()
        assert set(detail) == {"session", "messages"}
        assert set(detail["session"]) == {"id", "title", "created_at", "updated_at"}
        message = detail["messages"][-1]
        assert set(message) == MESSAGE_WIRE_KEYS
        assert message["citations"] == [legacy_citation]
        assert message["clarify"] == legacy_clarify
    finally:
        client.__exit__(None, None, None)


def test_health_failure_component_keys_stay_conditional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The db component is {ok, dialect} XOR {ok, error}: the model's optional
    fields must not surface as null-filled keys on the failure branch (the
    success branch is pinned exactly in test_api.py::test_health)."""
    from regwatch.api import main as api_main

    def _down() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(api_main, "get_engine", _down)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["components"]["db"] == {"ok": False, "error": "unreachable"}


def test_ready_success_omits_failure_keys() -> None:
    with TestClient(app) as client:
        r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    # No null-filled `failed`/`detail` on the ready branch.
    assert set(body) == {"status", "checks"}
    assert set(body["checks"]) == {"db", "vector_store", "llm"}


def test_resolve_spine_wire_keys_exact(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from regwatch.sources import dailymed
    from regwatch.sources.dailymed import SetidResolution, SplCandidate

    install_fake_sources(monkeypatch)

    # The shared stub resolves with zero candidates; carry one so the typed
    # spl_candidates path (WhitepaperSplCandidate) is actually exercised.
    def _resolve_with_candidates(
        application_number: str,
        *,
        prefer_titles: object = (),
        prefer_labelers: object = (),
        client: object = None,
    ) -> SetidResolution:
        return SetidResolution(
            setid="abc-def-123",
            title="PROVENTIL HFA [MERCK]",
            published="Oct 08, 2019",
            source_url="https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc-def-123",
            fetched_at=datetime.now(UTC),
            labeler="MERCK",
            candidate_labelers=("MERCK",),
            candidates=(
                SplCandidate(
                    setid="abc-def-123",
                    title="PROVENTIL HFA [MERCK]",
                    labeler="MERCK",
                    published="Oct 08, 2019",
                ),
            ),
        )

    monkeypatch.setattr(dailymed, "resolve_setid", _resolve_with_candidates)
    r = auth_client.post("/resolve", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == SPINE_WIRE_KEYS
    assert body["spl_candidates"], "the stubbed resolution carries one candidate"
    assert set(body["spl_candidates"][0]) == {"setid", "title", "labeler", "published"}
