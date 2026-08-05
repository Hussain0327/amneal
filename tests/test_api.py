"""FastAPI surface tests — every endpoint reachable, schema correct.

Protected endpoints go through the `auth_client` fixture (a logged-in
TestClient); only /health and CORS preflights stay anonymous here. The 401
behavior itself is locked down in tests/test_auth.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from config.settings import get_settings
from fastapi.testclient import TestClient

from regwatch.api.main import app
from regwatch.store.db import session_scope
from regwatch.store.models import Alert


def _open() -> TestClient:
    # `with TestClient(app)` triggers lifespan → init_db on the per-test DB.
    c = TestClient(app)
    c.__enter__()
    return c


def _seed_alert(
    appl_no: str,
    captured_at: str,
    n: int,
    *,
    created_at: datetime | None = None,
) -> Alert:
    """A durable alert row for the /watch/latest tests.

    ``captured_at`` values passed in should use the WRITER'S shape: naive-UTC
    isoformat with no +00:00 suffix (PsgVersion.captured_at is a naive DateTime
    column). ``created_at`` is overridable so tests can control feed order.
    """
    if created_at is None:
        created_at = datetime.now(UTC)
    return Alert(
        product_id=n,
        active_ingredient="Albuterol Sulfate",
        listing_appl_no=appl_no,
        listing_psg_type="final",
        psg_document_id=1,
        psg_version_id=n,  # distinct -> no unique-key conflict
        captured_at=captured_at,
        confidence=1.0,
        rationale="canonical",
        source_url=f"http://example/{appl_no}.pdf",
        created_at=created_at,
    )


def test_health() -> None:
    r = _open().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    components = body["components"]
    # B1: /health exposes the active datastore dialect; postgresql is the only
    # possible value since R5, and anything else must read as visibly wrong.
    assert components["db"] == {"ok": True, "dialect": "postgresql"}
    assert components["vector_store"]["ok"] is True
    assert components["vector_store"]["corpus_count"] == 0
    assert components["llm"] == {"provider": "echo", "key_present": True}
    assert components["embedding"] == {"provider": "echo", "profile": "legacy"}
    assert body["allow_test_providers"] is True  # conftest opt-in
    assert any("empty" in w for w in body["warnings"])
    assert any("echo" in w for w in body["warnings"])


def test_health_reports_the_profile_arm_not_the_legacy_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod bug this fixes: /health answered "openai" while queries were in
    fact embedded by the Databricks-hosted profile model.

    Retrieval branches on ACTIVE_EMBEDDING_PROFILE and only the "legacy" arm
    ever reads EMBEDDING_PROVIDER, so with a profile active the raw setting is
    inert -- and reporting it inverts the residency answer an operator came for.
    """
    import config.settings as cs

    from regwatch.api import main as api_main

    profile_id = "ep_" + "a" * 32
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", profile_id)
    # Non-echo LLM so the echo warning below can only come from the embedding
    # side -- otherwise the LLM arm masks what this test is checking.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cs.get_settings.cache_clear()

    class _Profile:
        provider = "qwen3"

    monkeypatch.setattr(
        "regwatch.store.embedding_profiles.get_embedding_profile",
        lambda _pid: _Profile(),
    )
    try:
        body = _open().get("/health").json()
        assert body["components"]["embedding"] == {"provider": "qwen3", "profile": profile_id}
        # The stale setting must not leak back in under any key.
        assert "echo" not in str(body["components"]["embedding"])
        # EMBEDDING_PROVIDER=echo is INERT while a profile is active, so it must
        # not raise the degraded-quality warning about a provider nothing uses.
        assert not any("echo" in w for w in body["warnings"])
    finally:
        cs.get_settings.cache_clear()
        assert api_main is not None


def test_health_embedding_component_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable profile must degrade to a reported value, never a 500.

    Note the probe still 503s here, but NOT because of this component: an
    unknown profile also breaks the corpus count, and vector_store is one of the
    two components that legitimately flip the status. The point of this test is
    that the embedding lookup contributes a truthful degraded value rather than
    propagating an exception out of the handler.
    """
    import config.settings as cs

    profile_id = "ep_" + "b" * 32
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", profile_id)
    cs.get_settings.cache_clear()

    def _boom(_pid: str) -> None:
        raise KeyError("unknown embedding profile")

    monkeypatch.setattr("regwatch.store.embedding_profiles.get_embedding_profile", _boom)
    try:
        r = _open().get("/health")
        assert r.status_code in (200, 503)
        assert r.json()["components"]["embedding"] == {
            "provider": "unresolved",
            "profile": profile_id,
        }
    finally:
        cs.get_settings.cache_clear()


def test_health_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # No-secrets discipline (same as Go's /settings allowlist struct): key
    # PRESENCE only, never a value.
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


def test_health_unhealthy_when_vector_store_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.api import main as api_main

    def _down() -> int:
        raise RuntimeError("vector store down")

    monkeypatch.setattr(api_main, "collection_size", _down)
    r = _open().get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["vector_store"]["ok"] is False


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


def test_cors_allowlist() -> None:
    c = _open()
    allowed_origin = get_settings().cors_allow_origins[0]
    blocked_origin = "https://not-allowed.example"

    allowed_preflight = c.options(
        "/query",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert allowed_preflight.status_code == 200
    assert allowed_preflight.headers["access-control-allow-origin"] == allowed_origin
    # The browser only sends the session cookie cross-origin when this is set.
    assert allowed_preflight.headers["access-control-allow-credentials"] == "true"

    blocked_preflight = c.options(
        "/query",
        headers={
            "Origin": blocked_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert blocked_preflight.status_code == 400
    assert "access-control-allow-origin" not in blocked_preflight.headers

    allowed_simple = c.get("/health", headers={"Origin": allowed_origin})
    assert allowed_simple.headers["access-control-allow-origin"] == allowed_origin

    blocked_simple = c.get("/health", headers={"Origin": blocked_origin})
    assert "access-control-allow-origin" not in blocked_simple.headers


def test_query_refuses_on_empty_corpus(
    monkeypatch: pytest.MonkeyPatch, auth_client: TestClient
) -> None:
    # Empty corpus → no grounded answer, but each valid query still gets one
    # constrained guidance turn.
    calls = {"n": 0}

    def _guidance_llm(*a: object, **k: object) -> object:
        calls["n"] += 1

        class _LLM:
            name = "stub"

            def complete(self, *args: object, **kwargs: object) -> object:
                from regwatch.generate.llm import LLMResponse

                return LLMResponse(text="{}", model="stub")

        return _LLM()

    from regwatch.generate import grounded_qa as qa_mod

    monkeypatch.setattr(qa_mod, "get_llm_provider", _guidance_llm)
    r = auth_client.post("/query", json={"question": "Does this exist?"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert body["citations"] == []
    assert body["session_id"]
    assert body["turn_id"]
    assert calls["n"] == 1

    r2 = auth_client.post(
        "/query",
        json={"question": "What about dissolution?", "session_id": body["session_id"]},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["session_id"] == body["session_id"]
    assert body2["turn_id"] != body["turn_id"]
    assert calls["n"] == 2


def test_query_rejects_zero_k(auth_client: TestClient) -> None:
    r = auth_client.post("/query", json={"question": "Does this exist?", "k": 0})
    assert r.status_code == 422


def test_sources_search_accepts_explicit_source_without_network(
    auth_client: TestClient,
) -> None:
    r = auth_client.post(
        "/sources/search",
        json={"query_text": "show PSG rows", "sources": ["psg"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["routed_sources"] == ["psg"]
    assert body["records"] == []


def test_watch_latest_returns_empty_when_no_alerts(auth_client: TestClient) -> None:
    r = auth_client.get("/watch/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["alerts"] == []
    # Truthful "never ran" (INV-4): an empty DB must report null, not a
    # fabricated quiet-day run.
    assert body["last_run"] is None


def test_watch_latest_carries_last_run_from_ledger(auth_client: TestClient) -> None:
    """`last_run` is the newest durable watch_run row in the agreed wire shape,
    so the UI can distinguish "quiet day" from "cron dead for a week"."""
    from regwatch.watch.runs import record_watch_run

    record_watch_run(
        started_at=datetime(2026, 7, 1, 7, 17, 0),
        finished_at=datetime(2026, 7, 1, 7, 21, 0),
        listings=1795,
        matched=2,
        added=1,
        revised=0,
        unchanged=1,
        errors=0,
        alerts=1,
        digest_date="2026-07-01",
    )
    body = auth_client.get("/watch/latest").json()
    assert body["last_run"] == {
        "started_at": "2026-07-01T07:17:00",
        "finished_at": "2026-07-01T07:21:00",
        "listings": 1795,
        "matched": 2,
        "added": 1,
        "revised": 0,
        "unchanged": 1,
        "errors": 0,
        "alerts": 1,
    }


def test_watch_latest_rejects_invalid_since(auth_client: TestClient) -> None:
    r = auth_client.get("/watch/latest", params={"since": "not-a-date"})
    assert r.status_code == 422


def test_watch_latest_since_filters_by_captured_at(auth_client: TestClient) -> None:
    """`since` keeps only alerts captured at/after it, INCLUSIVE at the exact
    boundary. Stored captured_at strings are the writer's naive-UTC isoformat
    (no +00:00), so the SQL compare must normalize `since` to that shape."""
    with session_scope() as s:
        s.add(_seed_alert("100001", "2026-06-01T00:00:00", 1))
        s.add(_seed_alert("100002", "2026-06-20T00:00:00", 2))

    both = auth_client.get("/watch/latest").json()
    assert {a["listing_appl_no"] for a in both["alerts"]} >= {"100001", "100002"}

    r = auth_client.get("/watch/latest", params={"since": "2026-06-10T00:00:00+00:00"})
    assert r.status_code == 200
    appl_nos = {a["listing_appl_no"] for a in r.json()["alerts"]}
    assert "100002" in appl_nos  # captured after `since`
    assert "100001" not in appl_nos  # captured before `since`, excluded

    # Boundary: a tz-aware `since` equal to the stored instant keeps the row.
    # Pre-fix, '2026-06-20T00:00:00' >= '2026-06-20T00:00:00+00:00' was False
    # lexicographically and the cursor client silently lost the boundary alert.
    boundary = auth_client.get(
        "/watch/latest", params={"since": "2026-06-20T00:00:00+00:00"}
    ).json()
    assert {a["listing_appl_no"] for a in boundary["alerts"]} == {"100002"}
    assert boundary["total"] == 1


def test_watch_latest_since_is_applied_before_the_row_cap(auth_client: TestClient) -> None:
    """Regression guard for the acknowledged prior bug shape: cap the newest N
    by created_at THEN filter captured_at in Python. The ONLY since-matching
    alert is seeded with the oldest created_at (outside every post-limit
    window), so it survives only if the filter runs in SQL before the cap."""
    base = datetime(2026, 6, 1, tzinfo=UTC)
    with session_scope() as s:
        # Recent capture, oldest insert: a post-limit filter evicts it first.
        s.add(_seed_alert("200000", "2026-06-20T00:00:00", 10, created_at=base))
        for i in range(1, 6):  # 5 old-captured alerts inserted AFTER it
            s.add(
                _seed_alert(
                    f"20000{i}",
                    "2026-05-01T00:00:00",
                    10 + i,
                    created_at=base + timedelta(minutes=i),
                )
            )

    r = auth_client.get(
        "/watch/latest",
        params={"since": "2026-06-10T00:00:00+00:00", "limit": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert [a["listing_appl_no"] for a in body["alerts"]] == ["200000"]
    assert body["count"] == 1
    assert body["total"] == 1


def test_watch_latest_paginates_with_true_total(auth_client: TestClient) -> None:
    """limit/offset page the durable feed and `total` is the FULL matching
    count -- len(page) alone reads as "that's everything" and rows past the
    cap were previously unreachable through the API forever."""
    base = datetime(2026, 6, 1, tzinfo=UTC)
    with session_scope() as s:
        for i in range(5):
            s.add(
                _seed_alert(
                    f"30000{i}",
                    "2026-06-01T00:00:00",
                    20 + i,
                    created_at=base + timedelta(minutes=i),
                )
            )

    seen: list[str] = []
    for offset in (0, 2, 4):
        body = auth_client.get("/watch/latest", params={"limit": 2, "offset": offset}).json()
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == offset
        assert body["count"] == len(body["alerts"])
        seen += [a["listing_appl_no"] for a in body["alerts"]]
    # Newest-first across pages: no overlap, nothing lost.
    assert seen == ["300004", "300003", "300002", "300001", "300000"]

    assert auth_client.get("/watch/latest", params={"limit": 0}).status_code == 422
    assert auth_client.get("/watch/latest", params={"limit": 501}).status_code == 422
    assert auth_client.get("/watch/latest", params={"offset": -1}).status_code == 422


def test_watch_latest_alerts_carry_change_kind(auth_client: TestClient) -> None:
    """Backend half of the New/Revised chip: every alert on the wire carries a
    STRUCTURAL change_kind (here "new": no psg_version history at all), so the
    UI never infers kind from degraded diff prose. The "revised" derivation is
    unit-tested against real version rows in tests/test_alerts.py."""
    with session_scope() as s:
        s.add(_seed_alert("400001", "2026-06-01T00:00:00", 40))
    body = auth_client.get("/watch/latest").json()
    assert body["alerts"][0]["change_kind"] == "new"


def test_assemble_refuses_when_no_matching_psg(auth_client: TestClient) -> None:
    r = auth_client.post(
        "/assemble",
        json={"active_ingredient": "Imaginary Drug XYZ"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert "No PSG" in body["markdown"]


def test_assemble_rejects_out_of_bounds_inputs(auth_client: TestClient) -> None:
    # active_ingredient min_length=2 -> a 1-char value is a 422, never reaches
    # build_dossier (these free-text fields flow to the QA prompt + audit row).
    too_short = auth_client.post("/assemble", json={"active_ingredient": "a"})
    assert too_short.status_code == 422
    # rld max_length=200 -> over-long is a 422 as well.
    too_long = auth_client.post(
        "/assemble",
        json={"active_ingredient": "Albuterol Sulfate", "rld": "9" * 201},
    )
    assert too_long.status_code == 422
