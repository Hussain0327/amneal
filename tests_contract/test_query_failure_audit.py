"""S14-S16 + S24-S27: the R1 headline -- failure still leaves a DEFINED audit
trail, now across the Go/Python CompleteQuery boundary (step-5 PR B).

S14 kills the provider (real openai SDK against a reserved closed port),
S15 fails only the strict answer-path audit write (conditional Postgres
trigger), S16 takes the audit store fully down on a skip-tolerant branch
(unconditional trigger). In every case the wire response is a 200 with a
defined payload -- never a naked 500 -- and the query_log state is exactly
what the contract defines.

PR B adds the three paths the cutover introduces or finally closes:
S24 forces an UNEXPECTED raise inside retrieve() -- the INV-6 gap that used
to escape ask() as an unaudited 500 (the July 2026 refactor backlog, item 17,
in git history); compute_turn's audited-error boundary yields one pipeline_error
row. S25 makes the Go control plane unable to reach the Python core at all
(ragclient dial refused) -- Go synthesizes one upstream_error row. S26 is the
answer path (allow_skip=False) under a TOTAL audit outage: the strict write
AND its fixed-copy fallback both fail, degrading to the audit_id=-1 sentinel
with the answer withheld -- the path the single-txn draft left undefined. S27 (PR C)
saturates the ask() worker pool (fault seam): both the native and relay paths
must shed the SAME defined 503 busy contract, never degrade into Go-deadline
timeouts and synthesized upstream_error rows.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWERABLE_QUESTION,
    DEAD_PROVIDER_TIMEOUT,
    REFUSAL_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    EdgeClient,
    Harness,
    Stack,
    audit_boom_trigger,
    chat_messages_for_turn,
    latest_query_log_row,
    pg_conn,
    query_log_count,
    seed_answerable_corpus,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]


def test_s14_provider_error_degrades_to_audited_refusal(
    dead_provider_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """Resolution and retrieval succeed; synthesis dies on connection-refused.
    The turn comes back 200 status=error WITH exactly one audit row carrying
    the retrieved evidence -- the literal behavior POLYGLOT_TARGET R1 says
    gates the step-5 cutover. (The openai SDK retries the dead port twice,
    ~4s total; the SDK has no env knob to disable retries, so the call just
    carries a wider timeout.)"""
    seed_answerable_corpus()
    client = edge_login(dead_provider_stack, timeout=DEAD_PROVIDER_TIMEOUT)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200, "a provider outage must never surface as a 500"
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["refused"] is True
    assert payload["reason"] == "provider_error"
    assert payload["answer"] == SERVICE_UNAVAILABLE_TEXT
    assert payload["citations"] == []
    # No provider/exception text may leak into the wire body.
    assert "Connection" not in payload["answer"]
    assert "sk-test-dead" not in response.text

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["status"] == "error"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "provider_error"
    assert row["retrieved_json"], "retrieval succeeded and its evidence is audited"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None

    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "error"
    assert messages[1]["audit_id"] == payload["audit_id"]


def test_s15_answer_audit_write_failure_withholds_the_answer(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """No-audit-no-answer: when the strict answer-path write fails, the paid
    answer is withheld and the failure-fallback error row commits instead
    (allow_skip=False + failure_fallback in the rag contract). The trigger is
    conditional on refused=false so ONLY the answer write fails."""
    seed_answerable_corpus()
    client = edge_login(base_stack)

    with audit_boom_trigger(when="NEW.refused = false"):
        response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["refused"] is True
    assert payload["answer"] == SERVICE_UNAVAILABLE_TEXT
    assert payload["citations"] == []
    # The validated answer and its citation markers must never leak unaudited.
    assert "ECHO:" not in response.text
    assert "PSG_020503" not in payload["answer"]
    assert payload["audit_id"] > 0, "the fallback row is a REAL committed row"

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["status"] == "error"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "audit_error"
    assert row["answer_text"] == SERVICE_UNAVAILABLE_TEXT
    assert row["model_name"] == "echo"
    # The synthesis DID run before the write failed; echo's real zero usage is
    # carried onto the fallback row.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


def test_s16_audit_fully_down_degrades_to_sentinel_not_500(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """With the audit store fully down, a skip-tolerant branch still serves
    its defined payload with the audit_id=-1 sentinel and writes NOTHING --
    the defined failure, logged and Sentry-captured, never a naked 500.
    (Ancestor: tests/test_meta_routing.py:344.)"""
    client = edge_login(base_stack)

    with audit_boom_trigger():
        response = client.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "refused"
    assert payload["reason"] == "empty_corpus"
    assert payload["answer"] == REFUSAL_TEXT
    assert payload["audit_id"] == -1, "the sentinel that never collides with a real id"
    assert query_log_count() == 0, "the defined failure: no row, not a half-row"

    # Empirically observed chat side effect (asserted as observed, not
    # over-specified): the user message written before compute survives, and
    # the best-effort assistant message commits carrying the -1 sentinel.
    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["audit_id"] == -1


def test_s24_pipeline_error_is_audited_not_a_naked_500(
    fault_retrieve_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The INV-6 gap PR B closes: an UNEXPECTED raise inside retrieve() (after
    the product resolves) used to escape ask() as an unaudited 500. compute_turn
    now catches it and returns a defined status="error"/pipeline_error turn, so
    the control plane writes exactly one audit row -- with EMPTY retrieval (the
    crash preceded any evidence) and NULL tokens (no LLM call)."""
    seed_answerable_corpus()  # resolution must succeed so retrieve() is reached
    client = edge_login(fault_retrieve_stack)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200, "a pipeline crash must never surface as a 500"
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["refused"] is True
    assert payload["reason"] == "pipeline_error"
    assert payload["answer"] == SERVICE_UNAVAILABLE_TEXT
    assert payload["citations"] == []
    # No exception/internal text may leak into the wire body.
    assert "RuntimeError" not in response.text
    assert "REGWATCH_FAULT_INJECT" not in response.text

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["status"] == "error"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "pipeline_error"
    assert row["retrieved_json"] == [], "the crash preceded retrieval; no evidence audited"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None

    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "error"
    assert messages[1]["audit_id"] == payload["audit_id"]


def test_s25_dead_internal_core_synthesizes_upstream_error(
    dead_internal_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The Go control plane cannot reach the Python RAG core at all (ragclient
    dial refused on the reserved closed port). Rather than 502, Go SYNTHESIZES a
    defined upstream_error turn and audits it: one row, empty retrieval, NULL
    tokens, the SERVICE_UNAVAILABLE answer -- INV-6 holds even when the core is
    down."""
    client = edge_login(dead_internal_stack)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200, "a dead core must never surface as a 502/500"
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["refused"] is True
    assert payload["reason"] == "upstream_error"
    assert payload["answer"] == SERVICE_UNAVAILABLE_TEXT
    assert payload["citations"] == []
    assert payload["audit_id"] > 0, "the skip-tolerant synthesized row is a REAL committed row"

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["status"] == "error"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "upstream_error"
    assert row["retrieved_json"] == []
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None

    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "error"
    assert messages[1]["audit_id"] == payload["audit_id"]


def test_s26_answer_path_total_audit_outage_withholds_and_sentinels(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The answer path (allow_skip=False) under a TOTAL audit outage: the strict
    write fails AND its fixed-copy fallback write ALSO fails (unconditional
    trigger). The paid answer is withheld, the failure degrades to the
    audit_id=-1 sentinel writing NOTHING -- never a naked 500, never an
    unaudited answer. This is the path the single-transaction draft left
    undefined; audit-first isolated writes make the chat rows survive anyway."""
    seed_answerable_corpus()
    client = edge_login(base_stack)

    with audit_boom_trigger():  # UNCONDITIONAL: both the strict AND fallback writes fail
        response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["refused"] is True
    assert payload["reason"] == "audit_error"
    assert payload["answer"] == SERVICE_UNAVAILABLE_TEXT
    assert payload["citations"] == []
    # The validated answer and its citations must never leak when unaudited.
    assert "ECHO:" not in response.text
    assert "PSG_020503" not in payload["answer"]
    assert payload["audit_id"] == -1, "both writes failed -> the sentinel, answer withheld"
    assert query_log_count() == 0, "the defined failure: no row, not a half-row"

    # The audit-first isolated writes keep the chat rows even though query_log is
    # fully down: T1 (user) and T3 (assistant, -1) both commit on their own conns.
    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "error"
    assert messages[1]["audit_id"] == -1


def _all_chat_roles() -> list[str]:
    with pg_conn() as conn:
        rows = conn.execute("SELECT role FROM public.chat_message ORDER BY created_at").fetchall()
    return [r[0] for r in rows]


def _chat_session_count() -> int:
    with pg_conn() as conn:
        row = conn.execute("SELECT count(*) FROM public.chat_session").fetchone()
        assert row is not None
        return int(row[0])


def test_s27_saturated_ask_pool_sheds_the_same_503_on_both_paths(
    harness: Harness, edge_login: Callable[..., EdgeClient]
) -> None:
    """ask()-pool saturation (forced via the prod-fenced "saturate" fault) is a
    DEFINED shed, not a slow failure: the native path's compute call comes back
    503 and Go relays FastAPI's exact busy body -- byte-identical to what the
    flag-off relay serves for the same condition -- and a shed turn leaves
    ZERO rows on BOTH paths. Go commits the T1 user message before the shed is
    known, then compensates (deleting the message and the fresh session shell),
    so the end state converges on Python's pre-write shed -- the formerly
    accepted orphaned-T1 divergence is CLOSED, and this pins it closed."""
    busy = b'{"detail":"server is busy, retry shortly"}'

    native = harness.stack("saturate")
    client = edge_login(native)
    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 503
    assert response.content == busy, "must be byte-identical to FastAPI's busy body"
    assert response.headers["content-type"] == "application/json"
    assert query_log_count() == 0, "a shed turn never ran; nothing to audit"
    assert _all_chat_roles() == [], "the T1 compensation leaves zero chat rows"
    assert _chat_session_count() == 0, "the fresh session shell is cleaned up too"

    # Relay comparison: the flag-off path sheds the identical bytes, and
    # Python's pre-write shed likewise leaves zero rows of every kind.
    relay = harness.stack("saturate", native=False)
    relay_client = edge_login(relay)
    relay_response = relay_client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert relay_response.status_code == 503
    assert relay_response.content == busy
    assert query_log_count() == 0
    assert _all_chat_roles() == []
    assert _chat_session_count() == 0
