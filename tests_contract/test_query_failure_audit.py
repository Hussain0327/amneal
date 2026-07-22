"""S14-S16: the R1 headline -- failure still leaves a DEFINED audit trail.

S14 kills the provider (real openai SDK against a reserved closed port),
S15 fails only the strict answer-path audit write (conditional Postgres
trigger), S16 takes the audit store fully down (unconditional trigger). In
every case the wire response is a 200 with a defined payload -- never a
naked 500 -- and the query_log state is exactly what the contract defines.

Deliberately NOT tested in PR A: the known INV-6 gap where an UNEXPECTED
raise in retrieve()/resolver escapes ask() as an unaudited 500
(docs/REFACTOR_BACKLOG_2026-07-09.md item 17). Pinning today's buggy
behavior would make the fix look like a regression; PR B's CompleteQuery
closes the gap and this suite gains the scenario then.
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
    Stack,
    audit_boom_trigger,
    chat_messages_for_turn,
    latest_query_log_row,
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
    assert payload["reason"] == "no_product"
    assert payload["answer"] == REFUSAL_TEXT
    assert payload["audit_id"] == -1, "the sentinel that never collides with a real id"
    assert query_log_count() == 0, "the defined failure: no row, not a half-row"

    # Empirically observed chat side effect (asserted as observed, not
    # over-specified): the user message written before compute survives, and
    # the best-effort assistant message commits carrying the -1 sentinel.
    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["audit_id"] == -1
