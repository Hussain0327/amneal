"""/ready readiness probe + /metrics Prometheus exposition (#7a).

/ready returns 200 only when DB + vector store are reachable AND the LLM client
is constructable (no paid call) - else 503 naming the failed check. /metrics is
hand-rolled Prometheus text derived from the query_log audit table. Both are
open (no session cookie), like /health.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import config.settings as cs
import pytest
from fastapi.testclient import TestClient

from regwatch.api import main
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests.conftest import create_user, session_client

if TYPE_CHECKING:
    from fastapi import Request


def _anon() -> TestClient:
    c = TestClient(main.app)
    c.__enter__()
    return c


# ---------- /ready ----------


def test_ready_open_and_200_with_echo_provider() -> None:
    # echo always constructs and the per-test SQLite + Chroma are reachable, so
    # /ready is 200 - and reachable without authentication (like /health).
    r = _anon().get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"db": True, "vector_store": True, "llm": True}


def test_ready_503_when_llm_not_constructable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real provider with no endpoint config cannot construct
    # get_llm_provider() -> the LLM check fails and /ready 503s naming "llm".
    # No paid call is made (construction raises before any request).
    monkeypatch.setenv("LLM_PROVIDER", "databricks")
    monkeypatch.setenv("DATABRICKS_LLM_BASE_URL", "")
    monkeypatch.setenv("DATABRICKS_LLM_TOKEN", "")
    monkeypatch.setenv("DATABRICKS_LLM_MODEL", "")
    cs.get_settings.cache_clear()
    r = _anon().get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["failed"] == "llm"
    assert body["checks"]["llm"] is False
    # The non-secret detail names the provider, never echoes a key/value.
    assert "databricks" in body["detail"]


def test_ready_503_names_db_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # DB takes precedence in the failed-check ordering (db, vector_store, llm).
    monkeypatch.setattr(main, "_db_component", lambda: {"ok": False, "error": "unreachable"})
    r = _anon().get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["failed"] == "db"
    assert body["detail"] == "db is unreachable"


def test_ready_does_not_make_paid_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard against a regression where /ready pings the provider: complete() must
    # never be invoked. With echo configured, getting a provider is fine, but if
    # anything called .complete() this would blow up.
    from regwatch.generate import llm as llm_mod

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("/ready must not make a paid LLM call")

    monkeypatch.setattr(llm_mod.EchoLLMProvider, "complete", _boom)
    assert _anon().get("/ready").status_code == 200


# ---------- /metrics ----------


def test_metrics_open_and_prometheus_content_type() -> None:
    r = _anon().get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert "# HELP regwatch_queries_total" in body
    assert "# TYPE regwatch_queries_total counter" in body
    assert "regwatch_queries_refused_total" in body


def test_metrics_counts_query_log_rows() -> None:
    client = session_client(create_user())
    # An out-of-corpus question on the echo provider audits a refusal (qa mode).
    assert client.post("/query", json={"question": "Out of corpus?"}).status_code == 200

    body = _anon().get("/metrics").text
    # One qa row, and it refused (echo provider has no real corpus).
    assert 'regwatch_queries_total{mode="qa"} 1' in body
    assert "regwatch_queries_refused_total 1" in body


def test_metrics_groups_by_mode_without_n_plus_one() -> None:
    # Two qa rows + one assemble row -> the mode label aggregates correctly. This
    # asserts the single grouped query path (no per-row N+1) produces the right
    # per-mode totals.
    client = session_client(create_user())
    assert client.post("/query", json={"question": "First out of corpus?"}).status_code == 200
    assert client.post("/query", json={"question": "Second out of corpus?"}).status_code == 200
    assert client.post("/assemble", json={"active_ingredient": "Imaginary XYZ"}).status_code == 200

    body = _anon().get("/metrics").text
    assert 'regwatch_queries_total{mode="qa"} 2' in body
    assert 'regwatch_queries_total{mode="assemble"} 1' in body


def test_metrics_empty_query_log_exposes_zero() -> None:
    # A fresh process with no audited queries still exposes the named series so a
    # scraper doesn't see a vanished metric.
    body = _anon().get("/metrics").text
    assert "regwatch_queries_total 0" in body
    assert "regwatch_queries_refused_total 0" in body
    assert 'regwatch_route_shadow_calls_total{outcome="success"} 0' in body
    assert "regwatch_route_shadow_failures_total 0" in body


def test_metrics_counts_nested_route_shadow_outcomes_and_compilations() -> None:
    with session_scope() as session:
        session.add_all(
            [
                QueryLog(
                    mode="qa",
                    query_text="Across inhalation PSGs, define ISM",
                    answer_text="Existing no-product response",
                    refused=True,
                    status="refused",
                    route_json={
                        "route_call": {
                            "outcome": "success",
                            "compile_status": "success",
                        }
                    },
                    model_name="route-model",
                ),
                QueryLog(
                    mode="qa",
                    query_text="Ambiguous question",
                    answer_text="Existing clarification",
                    refused=False,
                    status="clarify",
                    route_json={
                        "route_call": {
                            "outcome": "invalid",
                            "compile_status": "not_attempted",
                        }
                    },
                    model_name="route-model",
                ),
            ]
        )

    body = _anon().get("/metrics").text

    assert 'regwatch_route_shadow_calls_total{outcome="success"} 1' in body
    assert 'regwatch_route_shadow_calls_total{outcome="invalid"} 1' in body
    assert "regwatch_route_shadow_failures_total 1" in body
    assert 'regwatch_route_shadow_compilations_total{status="success"} 1' in body
    assert 'regwatch_route_shadow_compilations_total{status="not_attempted"} 1' in body


def test_metrics_degrades_to_zero_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A DB error during the counter query must NOT 500 the scrape - it degrades
    # to the static help/type lines with zeroed counters.
    monkeypatch.setattr(main, "_query_log_counters", lambda: {})
    r = _anon().get("/metrics")
    assert r.status_code == 200
    assert "regwatch_queries_refused_total 0" in r.text


# ---------- /metrics opt-in bearer gate (METRICS_TOKEN) ----------


def _arm_metrics_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("METRICS_TOKEN", token)
    cs.get_settings.cache_clear()


def test_metrics_open_when_token_unset() -> None:
    # Default (METRICS_TOKEN unset): /metrics stays open with no Authorization
    # header - the opt-in gate must not change today's behavior.
    assert _anon().get("/metrics").status_code == 200


def test_metrics_401_when_token_set_and_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm_metrics_token(monkeypatch, "s3cr3t-scrape-token")
    assert _anon().get("/metrics").status_code == 401


def test_metrics_401_when_token_set_and_header_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm_metrics_token(monkeypatch, "s3cr3t-scrape-token")
    r = _anon().get("/metrics", headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401


def test_metrics_200_when_token_set_and_header_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm_metrics_token(monkeypatch, "s3cr3t-scrape-token")
    r = _anon().get("/metrics", headers={"Authorization": "Bearer s3cr3t-scrape-token"})
    assert r.status_code == 200
    # Still the real Prometheus body, not just a 200.
    assert "# HELP regwatch_queries_total" in r.text


def test_metrics_401_when_non_bearer_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    # A correctly-valued token presented under a non-Bearer scheme is rejected;
    # the gate is specifically `Authorization: Bearer <token>`.
    _arm_metrics_token(monkeypatch, "s3cr3t-scrape-token")
    r = _anon().get("/metrics", headers={"Authorization": "Basic s3cr3t-scrape-token"})
    assert r.status_code == 401


def test_metrics_authorized_handles_non_ascii_bearer_without_raising() -> None:
    # A non-ASCII bearer value must be rejected as a mismatch, never raise inside
    # compare_digest (which rejects non-ASCII str) and 500 the scrape - we compare
    # on utf-8 bytes for exactly this. Driven at the helper because the test
    # client's header transport itself cannot carry a non-latin-1 header value.
    s = cs.Settings(metrics_token="s3cr3t-scrape-token")  # type: ignore[call-arg]
    req = cast("Request", SimpleNamespace(headers={"authorization": "Bearer ééé"}))
    assert main._metrics_authorized(req, s) is False


def test_health_and_ready_stay_open_when_metrics_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HARD CONSTRAINT: METRICS_TOKEN must gate ONLY /metrics. /health is the Fly
    # healthcheck and /ready the readiness probe; gating either would mark
    # machines unhealthy. Both must stay reachable with no Authorization header.
    _arm_metrics_token(monkeypatch, "s3cr3t-scrape-token")
    c = _anon()
    assert c.get("/health").status_code == 200
    assert c.get("/ready").status_code == 200
