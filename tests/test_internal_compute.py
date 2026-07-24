"""POST /internal/query/compute saturation shed (step-5 PR C, fix 3).

The public /query family sheds load with a defined 503 when the ask() worker
pool is saturated (_dispatch_ask). The internal compute endpoint queues on the
SAME limiter, so without its own shed the flag-on Go control plane degrades
overload into 240s deadline timeouts and synthesized upstream_error rows.
These pin the shared helper: the internal endpoint 503s with the exact
_dispatch_ask detail WITHOUT invoking compute, and the contract-suite fault
seam ("saturate") is fenced by allow_test_providers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from config.settings import get_settings

from regwatch.api import main as api_main
from tests.conftest import AuthedClient

_TOKEN = "test-internal-rag-token"
_BUSY = {"detail": "server is busy, retry shortly"}


class _SaturatedLimiter:
    """statistics().borrowed_tokens >= total_tokens -- the shed condition."""

    total_tokens = 16

    def statistics(self) -> Any:
        return SimpleNamespace(borrowed_tokens=16)


@pytest.fixture
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    # The guard fail-closes (404) while internal_rag_token is unset; give the
    # cached settings instance a token for the duration of the test.
    monkeypatch.setattr(get_settings(), "internal_rag_token", _TOKEN)
    return _TOKEN


def _post_compute(client: AuthedClient, token: str) -> Any:
    return client.post(
        "/internal/query/compute",
        json={"question": "What study design is recommended?", "session_id": "s1", "turn_id": "t1"},
        headers={"X-Internal-Token": token},
    )


def test_internal_compute_sheds_503_without_running_compute(
    auth_client: AuthedClient, internal_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saturated limiter -> the exact _dispatch_ask 503, and the compute
    pipeline is never entered (the shed precedes to_thread.run_sync)."""

    def boom(req: Any) -> Any:
        raise AssertionError("compute must not run when the pool is saturated")

    monkeypatch.setattr(api_main, "_compute_payload", boom)
    monkeypatch.setattr(api_main, "_ASK_LIMITER", _SaturatedLimiter())

    res = _post_compute(auth_client, internal_token)
    assert res.status_code == 503
    assert res.json() == _BUSY


def test_saturate_fault_seam_is_fenced_by_allow_test_providers(
    auth_client: AuthedClient, internal_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGWATCH_FAULT_INJECT=saturate forces the shed only behind the
    allow_test_providers boot fence (grounded_qa._maybe_inject_fault parity);
    in a prod-shaped process the env var is inert and compute runs."""
    monkeypatch.setenv("REGWATCH_FAULT_INJECT", "saturate")
    monkeypatch.setattr(api_main, "_compute_payload", lambda req: {"ok": True})

    monkeypatch.setattr(get_settings(), "allow_test_providers", True)
    res = _post_compute(auth_client, internal_token)
    assert res.status_code == 503
    assert res.json() == _BUSY

    monkeypatch.setattr(get_settings(), "allow_test_providers", False)
    res = _post_compute(auth_client, internal_token)
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_public_query_shed_uses_the_same_helper_detail(
    auth_client: AuthedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/query's shed rides the SAME helper: identical status + detail, so the
    flag-on (Go-relayed) and flag-off overload contracts cannot drift."""
    monkeypatch.setattr(api_main, "_ASK_LIMITER", _SaturatedLimiter())
    res = auth_client.post("/query", json={"question": "Busy now?"})
    assert res.status_code == 503
    assert res.json() == _BUSY
