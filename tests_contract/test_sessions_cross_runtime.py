"""S17 / S18 / S29: the session contract across the runtimes.

S17 is the strongest cross-runtime pin in the matrix: Python writes the
chat rows during /query turns, and GET /sessions/{id} -- Go-NATIVE since the
step-4 cutover -- serves them back verbatim. One assertion closes the
shared-schema loop in both directions.

S18 proves the rate limit is a real pre-work 429 (nothing audited) on both
query routes, and (GAP-5) that a second user gets its own fresh budget.

S29 (GAP-2) proves a NULL-owner legacy session is adopted by the first
authenticated /query caller through the edge.

S32 proves chat_session.origin holds across the split: both writers stamp it
(Go's UpsertChatSession on /query, Python's ensure_session on /query/stream),
the Threads list serves only origin='thread', and an assistant conversation
stays readable by id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWERABLE_QUESTION,
    EdgeClient,
    Stack,
    latest_query_log_row,
    parse_sse,
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

    # GAP-5: the queryLimiter keys on "user:{id}" -- a SECOND freshly-logged-in
    # user must get a fresh budget, not share the exhausted one, and its turn
    # is audited under its own id.
    other = edge_login(rate_limited_stack)
    fresh = other.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})
    assert fresh.status_code == 200, "a second user is a fresh budget, never a shared bucket"
    assert query_log_count() == 2, "the fresh-budget turn was audited"
    assert latest_query_log_row()["user_id"] == str(other.user_id)


def test_s29_null_owner_legacy_session_adopted_via_query(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """GAP-2: a pre-auth demo row (user_id NULL) is ADOPTED by the first
    authenticated /query caller: 200 with the sid echoed, the owner column
    flips to the caller (authorizeSession's conditional-UPDATE + re-read
    path), the turn is attributed to the adopter, and the session becomes
    visible on the Go-native GET /sessions/{id}."""
    legacy_sid = str(uuid.uuid4())
    with pg_conn() as conn:
        # The legacy demo-row shape: pre-auth sessions carried no owner.
        conn.execute(
            "INSERT INTO public.chat_session "
            "(id, user_id, title, active_filters_json, created_at, updated_at) "
            "VALUES (%s, NULL, 'Legacy demo', '{}', now(), now())",
            (legacy_sid,),
        )
        conn.commit()

    client = edge_login(base_stack)
    # Empty corpus on purpose: refusal turns adopt too (adoption is pre-work).
    response = client.http.post(
        "/query", json={"question": ABSENT_DRUG_QUESTION, "session_id": legacy_sid}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == legacy_sid, "the adopted session id is echoed back"

    with pg_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM public.chat_session WHERE id = %s", (legacy_sid,)
        ).fetchone()
    assert row is not None
    assert row[0] == str(client.user_id), "the NULL owner column flipped to the caller"

    # INV-6 attribution on the adopted turn (the _one_new_row invariants).
    assert query_log_count() == 1
    log_row = latest_query_log_row()
    assert log_row["id"] == payload["audit_id"]
    assert log_row["user_id"] == str(client.user_id)
    assert log_row["session_id"] == legacy_sid

    # Adoption makes the legacy session readable cross-runtime.
    detail = client.http.get(f"/sessions/{legacy_sid}")
    assert detail.status_code == 200
    assert detail.json()["session"]["id"] == legacy_sid


def _session_origins() -> dict[str, str]:
    """Every chat_session row's origin, straight from the shared Postgres."""
    with pg_conn() as conn:
        rows = conn.execute("SELECT id, origin FROM public.chat_session").fetchall()
    return {r[0]: r[1] for r in rows}


def test_s32_assistant_origin_is_written_by_both_runtimes_and_hidden_from_the_list(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """S32: chat_session.origin, across the runtime split.

    The Research Studio's Assistant panel holds its own conversation, and
    /query has exactly one place to put a conversation -- so without this
    column that conversation lands in the work rail's Threads list beside the
    analyst's real work. The fix is one column written on create and filtered
    on read, and it has to hold on BOTH writers, because the panel's two code
    paths do not share one:

      POST /query        -> Go-native persistUserTurn -> UpsertChatSession
      POST /query/stream -> relayed to Python -> ask() -> ensure_session

    The panel calls the stream and falls back to the blocking route, so a fix
    that landed on only one of these would hide the clutter until the first
    stream failure and then quietly stop. Same reason S17 exists: two parallel
    implementations of one write are held identical HERE or not at all.
    """
    seed_answerable_corpus()
    client = edge_login(base_stack)

    # An ordinary turn: the default origin, and the analyst's real work.
    thread_sid = client.http.post("/query", json={"question": ANSWERABLE_QUESTION}).json()[
        "session_id"
    ]

    # The Go-native writer, asked the way the panel asks.
    go_sid = client.http.post(
        "/query", json={"question": ANSWERABLE_QUESTION, "origin": "assistant"}
    ).json()["session_id"]

    # The PYTHON writer, over the panel's primary path. Read to EOF so the turn
    # is fully persisted before the assertions below read the table.
    with client.http.stream(
        "POST",
        "/query/stream",
        json={"question": ANSWERABLE_QUESTION, "origin": "assistant"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    py_sid = parse_sse(body).single_result()["session_id"]

    assert len({thread_sid, go_sid, py_sid}) == 3, "three turns, three distinct sessions"

    # Both runtimes wrote the column, and neither stamped the ordinary turn.
    origins = _session_origins()
    assert origins[thread_sid] == "thread", "an unmarked turn is still a thread"
    assert origins[go_sid] == "assistant", "the Go native writer stamps origin"
    assert origins[py_sid] == "assistant", "the Python stream writer stamps origin"

    # The read side: the Threads list serves the analyst's work and nothing else.
    listed = {s["id"] for s in client.http.get("/sessions").json()["sessions"]}
    assert thread_sid in listed
    assert go_sid not in listed, "an assistant conversation never joins the Threads list"
    assert py_sid not in listed

    # The DELIBERATE asymmetry: hidden from the list is not unreachable. Both
    # stay readable by id, so the panel can reload its own conversation and a
    # stray row can still be deleted rather than accumulating forever.
    for sid in (go_sid, py_sid):
        detail = client.http.get(f"/sessions/{sid}")
        assert detail.status_code == 200, "an assistant conversation is still readable by id"
        assert detail.json()["session"]["id"] == sid

    # Create-only, on the writer that owns the ON CONFLICT path: a later turn
    # on the same session that does NOT ask for "assistant" must not promote
    # that conversation into the Threads list.
    follow_up = client.http.post(
        "/query", json={"question": "What about dissolution?", "session_id": go_sid}
    )
    assert follow_up.status_code == 200
    assert _session_origins()[go_sid] == "assistant", "origin is set on create, never on conflict"
    assert go_sid not in {s["id"] for s in client.http.get("/sessions").json()["sessions"]}
