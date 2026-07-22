"""S3-S5: auth enforcement at the edge for both query routes.

401/404 are REAL pre-work HTTP statuses on the stream route too (never an
opened event-stream), and none of them write an audit row -- an unauthorized
probe must not pollute the compliance log.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    CLIENT_TIMEOUT,
    EdgeClient,
    Stack,
    chat_message_count,
    query_log_count,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]

_ACCEPT_SSE = {"Accept": "text/event-stream"}


def test_s3_query_without_cookie_is_401_and_unaudited(base_stack: Stack) -> None:
    response = httpx.post(
        f"{base_stack.edge_url}/query",
        json={"question": "What study design is recommended?"},
        timeout=CLIENT_TIMEOUT,
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert response.headers.get("content-type", "").startswith("application/json")
    assert query_log_count() == 0


def test_s4_query_stream_without_cookie_is_a_real_401_never_a_stream(base_stack: Stack) -> None:
    response = httpx.post(
        f"{base_stack.edge_url}/query/stream",
        json={"question": "What study design is recommended?"},
        headers=_ACCEPT_SSE,
        timeout=CLIENT_TIMEOUT,
    )
    assert response.status_code == 401
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert response.json() == {"detail": "authentication required"}
    assert query_log_count() == 0


def test_s5_foreign_session_is_404_on_both_routes_and_writes_nothing(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """404-not-403 non-disclosure: user B probing user A's session id learns
    nothing and leaves no trace in A's session or the audit log."""
    user_a = edge_login(base_stack)
    # Any turn creates a session -- the empty-corpus refusal needs no seed.
    turn = user_a.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})
    assert turn.status_code == 200
    session_id = turn.json()["session_id"]
    rows_before = query_log_count()
    messages_before = chat_message_count(session_id)

    user_b = edge_login(base_stack)
    stolen = {"question": "What study design is recommended?", "session_id": session_id}

    blocked = user_b.http.post("/query", json=stolen)
    assert blocked.status_code == 404
    assert blocked.json() == {"detail": "session not found"}

    blocked_stream = user_b.http.post("/query/stream", json=stolen, headers=_ACCEPT_SSE)
    assert blocked_stream.status_code == 404
    assert "text/event-stream" not in blocked_stream.headers.get("content-type", "")
    assert blocked_stream.json() == {"detail": "session not found"}

    assert query_log_count() == rows_before
    assert chat_message_count(session_id) == messages_before
