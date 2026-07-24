"""S3-S5 + S28: pre-work statuses at the edge for both query routes.

401/404/422 are REAL pre-work HTTP statuses on the stream route too (never an
opened event-stream), and none of them write an audit row -- an unauthorized
or malformed probe must not pollute the compliance log. S5 additionally pins
that a hijack probe leaves the victim's owner column untouched (GAP-4), and
S28 pins the native Go 422 contract at pydantic item granularity (GAP-1).
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
    pg_conn,
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

    # GAP-4: a flip-owner-then-404 bug would pass every count assert above --
    # re-read the owner column to prove the hijack never touched it.
    with pg_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM public.chat_session WHERE id = %s", (session_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == str(user_a.user_id), "the hijack must not flip the owner column"


def test_s28_native_query_422_matches_pydantic_shape_and_is_unaudited(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """GAP-1: the native Go /query reproduces pydantic's 422 contract at item
    granularity -- {"detail": [{"type", "loc", "msg"}]} -- for the k bounds,
    a missing question, and a malformed (trailing-JSON) body. Validation is
    pre-work: no audit row and no chat rows may exist afterwards."""
    client = edge_login(base_stack)

    cases = [
        (
            {"question": "q?", "k": 0},
            {
                "type": "greater_than_equal",
                "loc": ["body", "k"],
                "msg": "Input should be greater than or equal to 1",
            },
        ),
        (
            {"question": "q?", "k": 51},
            {
                "type": "less_than_equal",
                "loc": ["body", "k"],
                "msg": "Input should be less than or equal to 50",
            },
        ),
        (
            {"k": 3},
            {"type": "missing", "loc": ["body", "question"], "msg": "Field required"},
        ),
    ]
    for body, item in cases:
        response = client.http.post("/query", json=body)
        assert response.status_code == 422, body
        assert response.headers.get("content-type", "").startswith("application/json")
        assert response.json() == {"detail": [item]}, body

    # Trailing content after a complete JSON document must be rejected too
    # (Go's decoder would otherwise silently accept the first document).
    trailing = client.http.post(
        "/query",
        content=b'{"question": "q?", "k": 3}{"trailing": true}',
        headers={"Content-Type": "application/json"},
    )
    assert trailing.status_code == 422
    assert trailing.json() == {
        "detail": [{"type": "json_invalid", "loc": ["body"], "msg": "Input should be a valid JSON"}]
    }

    assert query_log_count() == 0, "422 is pre-work; nothing may be audited"
    with pg_conn() as conn:
        chat_rows = conn.execute("SELECT count(*) FROM public.chat_message").fetchone()
    assert chat_rows is not None and chat_rows[0] == 0, "422 is pre-work; no chat rows either"
