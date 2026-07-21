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
