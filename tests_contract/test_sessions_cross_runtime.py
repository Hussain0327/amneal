"""S17 / S18: the session contract across the runtimes.

S17 is the strongest cross-runtime pin in the matrix: Python writes the
chat rows during /query turns, and GET /sessions/{id} -- Go-NATIVE since the
step-4 cutover -- serves them back verbatim. One assertion closes the
shared-schema loop in both directions.

S18 proves the rate limit is a real pre-work 429 (nothing audited) on both
query routes.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWERABLE_QUESTION,
    EdgeClient,
    Stack,
    pg_conn,
    query_log_count,
    seed_answerable_corpus,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]


def test_s17_go_serves_the_session_python_wrote(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    turn1 = client.http.post("/query", json={"question": ANSWERABLE_QUESTION}).json()
    assert turn1["status"] == "answer"
    session_id = turn1["session_id"]

    turn2 = client.http.post(
        "/query", json={"question": "What about dissolution?", "session_id": session_id}
    ).json()
    assert turn2["session_id"] == session_id, "follow-ups reuse the caller's session"
    assert turn2["turn_id"] != turn1["turn_id"]

    assert query_log_count() == 2
    with pg_conn() as conn:
        rows = conn.execute(
            "SELECT role, audit_id, citations_json FROM public.chat_message "
            "WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        ).fetchall()
    assert [r[0] for r in rows] == ["user", "assistant", "user", "assistant"]
    # Distinct audit rows per turn: an id reused across turns would collapse
    # the dict below and pass the old set-equality check vacuously.
    assert turn1["audit_id"] != turn2["audit_id"]
    persisted_by_turn = {turn1["audit_id"]: rows[1], turn2["audit_id"]: rows[3]}

    # The cross-runtime read: Go serves the rows Python persisted.
    detail = client.http.get(f"/sessions/{session_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["session"]["id"] == session_id
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    for message in messages:
        if message["role"] != "assistant":
            continue
        audit_id = message["audit_id"]
        db_row = persisted_by_turn[audit_id]
        assert db_row[1] == audit_id
        # Citations come back byte-equivalent to what Python wrote (Go relays
        # the stored JSON verbatim, no re-shaping).
        assert message["citations"] == db_row[2]
        assert message["citations"], "answer turns carry citations end-to-end"


def test_s18_rate_limit_is_a_real_pre_work_429_on_both_routes(
    rate_limited_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """RATE_LIMIT_PER_MINUTE=1 flavor with its own dedicated user: the second
    call 429s before any work, so only turn 1 is audited. (The limiter is
    in-process per-user state in the app; a dedicated user isolates this test
    from every other scenario.)"""
    client = edge_login(rate_limited_stack)

    first = client.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})
    assert first.status_code == 200
    assert query_log_count() == 1

    second = client.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})
    assert second.status_code == 429
    assert second.json() == {"detail": "rate limit exceeded"}

    stream = client.http.post(
        "/query/stream",
        json={"question": ABSENT_DRUG_QUESTION},
        headers={"Accept": "text/event-stream"},
    )
    assert stream.status_code == 429
    assert "text/event-stream" not in stream.headers.get("content-type", "")
    assert stream.json() == {"detail": "rate limit exceeded"}

    assert query_log_count() == 1, "rate-limited requests never reach ask(), so nothing is audited"
