"""S6-S13: every /query outcome branch returns 200 and writes EXACTLY one
audit row, with the column-level shapes the Go CompleteQuery cutover must
reproduce.

Status-code-only tests would silently un-pin: the reason strings, the
retrieved_json empty-vs-populated distinction, the token/cost NULL
discipline (NULL when no LLM ran, zeros under echo, cost never guessed),
route_json's exact key set, and the exactly-ONE-row property itself. Each is
an explicit assertion here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWERABLE_QUESTION,
    CITATION_KEYS,
    MULTIFORM_QUESTION,
    QUERY_RESPONSE_KEYS,
    REFUSAL_TEXT,
    RETRIEVED_ITEM_KEYS,
    ROUTE_JSON_KEYS,
    EdgeClient,
    Stack,
    chat_messages_for_turn,
    latest_query_log_row,
    query_log_count,
    seed_answerable_corpus,
    seed_multiform_corpus,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]


def _one_new_row(payload: dict[str, Any], client: EdgeClient) -> dict[str, Any]:
    """The turn's single query_log row, with the invariants every branch
    shares: mode, verbatim question, attribution, and id parity."""
    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["mode"] == "qa"
    assert row["user_id"] == str(client.user_id)
    assert row["session_id"] == payload["session_id"]
    assert row["turn_id"] == payload["turn_id"]
    assert set(row["route_json"].keys()) == ROUTE_JSON_KEYS
    return row


def _assert_two_chat_messages(payload: dict[str, Any], *, status: str, cited: bool) -> None:
    """The turn's two chat rows, with the assistant row's status matching the
    outcome and its citations_json populated exactly when the wire carried
    citations -- the chat trail must agree with the audit trail."""
    messages = chat_messages_for_turn(payload["turn_id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["audit_id"] == payload["audit_id"]
    assert messages[1]["status"] == status
    if cited:
        assert messages[1]["citations_json"]
    else:
        assert messages[1]["citations_json"] == []


def test_s6_answer_golden_row(base_stack: Stack, edge_login: Callable[..., EdgeClient]) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200
    payload = response.json()

    assert set(payload.keys()) == QUERY_RESPONSE_KEYS
    assert payload["status"] == "answer"
    assert payload["refused"] is False
    assert payload["reason"] == "retrieval"
    assert payload["model_name"] == "echo"
    assert payload["audit_id"] > 0
    assert payload["session_id"] and payload["turn_id"]
    assert payload["citations"], "a grounded answer must carry citations"
    for citation in payload["citations"]:
        assert set(citation.keys()) == CITATION_KEYS
        # INV-1: every citation is grounded in the seeded passages.
        assert (citation["short_name"], citation["page"]) in {("PSG_020503", 3), ("PSG_020503", 4)}

    row = _one_new_row(payload, client)
    assert row["status"] == "answer"
    assert row["refused"] is False
    assert row["query_text"] == ANSWERABLE_QUESTION
    assert row["answer_text"] == payload["answer"]
    assert row["retrieved_json"], "the audited evidence trail must be populated"
    for item in row["retrieved_json"]:
        assert set(item.keys()) == RETRIEVED_ITEM_KEYS
    assert row["citations_json"]
    assert row["route_json"]["route"] == "psg_scoped_rag"
    assert row["route_json"]["reason"] == "retrieval"
    assert row["route_json"]["context_applied"] is False
    assert row["route_json"]["response_mode"] == "answer"
    assert row["model_name"] == "echo"
    # Echo reports REAL zero usage (an LLM ran); cost stays NULL because echo
    # is unpriced -- never a guessed number.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["cost_usd"] is None

    _assert_two_chat_messages(payload, status="answer", cited=True)


def test_s7_summary_variant(base_stack: Stack, edge_login: Callable[..., EdgeClient]) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    response = client.http.post(
        "/query", json={"question": "Summarize the recommended study design."}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "summary"
    assert payload["refused"] is False

    row = _one_new_row(payload, client)
    assert row["status"] == "summary"
    assert row["route_json"]["response_mode"] == "summary"
    assert row["retrieved_json"]


def test_s8_no_product_refusal_on_empty_corpus(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    client = edge_login(base_stack)

    response = client.http.post("/query", json={"question": ABSENT_DRUG_QUESTION})
    assert response.status_code == 200, "refusals are 200s with refused=true, never 4xx/5xx"
    payload = response.json()
    assert set(payload.keys()) == QUERY_RESPONSE_KEYS
    assert payload["status"] == "refused"
    assert payload["reason"] == "no_product"
    assert payload["refused"] is True
    assert payload["answer"] == REFUSAL_TEXT
    assert payload["citations"] == []
    assert payload["related"] == []

    row = _one_new_row(payload, client)
    assert row["status"] == "refused"
    assert row["refused"] is True
    assert row["answer_text"] == REFUSAL_TEXT
    assert row["retrieved_json"] == []
    assert row["citations_json"] == []
    assert row["route_json"]["reason"] == "no_product"
    # No LLM ran: token/cost columns are NULL, not zero (INV-2 discipline).
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cost_usd"] is None

    _assert_two_chat_messages(payload, status="refused", cited=False)


def test_s9_low_top_score_refusal_audits_the_weak_evidence(
    low_score_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """Same seed as S6, threshold 1.0: retrieval RUNS and its sub-threshold
    passages are audited (populated retrieved_json distinguishes this refusal
    family from S8's). The question must not be verbatim-identical to a seeded
    chunk -- identical text scores exactly 1.0 and would answer."""
    seed_answerable_corpus()
    client = edge_login(low_score_stack)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "refused"
    assert payload["reason"] == "low_top_score"
    assert payload["refused"] is True
    assert payload["citations"] == []
    assert payload["related"], "sub-threshold evidence still yields related product pointers"

    row = _one_new_row(payload, client)
    assert row["status"] == "refused"
    assert row["route_json"]["reason"] == "low_top_score"
    assert row["retrieved_json"], "the weak evidence is the point of this row"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cost_usd"] is None


def test_s10_vague_input_clarify(base_stack: Stack, edge_login: Callable[..., EdgeClient]) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    response = client.http.post("/query", json={"question": "albuterol sulfate"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "clarify"
    assert payload["reason"] == "vague_input"
    assert payload["refused"] is False
    assert payload["citations"] == []
    assert payload["clarify"], "a clarify must offer options"
    for option in payload["clarify"]:
        assert set(option.keys()) == {"label", "query", "filters"}

    row = _one_new_row(payload, client)
    assert row["status"] == "clarify"
    assert row["route_json"]["reason"] == "vague_input"
    assert row["retrieved_json"] == []  # pre-retrieval clarify
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None


def test_s11_multi_form_clarify_pins_every_combo(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    seed_multiform_corpus()
    client = edge_login(base_stack)

    response = client.http.post("/query", json={"question": MULTIFORM_QUESTION})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "clarify"
    assert payload["reason"] == "multi_form"
    assert payload["citations"] == []
    combos = {
        (o["filters"]["dosage_form"], o["filters"]["route"])
        for o in payload["clarify"]
        if o["filters"]
    }
    assert combos == {("Gel", "Transdermal"), ("Tablet", "Vaginal")}
    assert all(o["filters"].get("normalized_name") == "estradiol" for o in payload["clarify"])

    row = _one_new_row(payload, client)
    assert row["status"] == "clarify"
    assert row["route_json"]["reason"] == "multi_form"
    assert row["retrieved_json"] == []  # the guard fires BEFORE retrieval


def test_s12_scope_warning(base_stack: Stack, edge_login: Callable[..., EdgeClient]) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    response = client.http.post(
        "/query", json={"question": "What submission strategy should we use to file the ANDA?"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "scope_warning"
    assert payload["refused"] is True
    assert payload["citations"] == []

    row = _one_new_row(payload, client)
    assert row["status"] == "scope_warning"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "scope_warning"
    assert row["route_json"]["response_mode"] == "scope_warning"
    assert row["retrieved_json"] == []
    assert row["input_tokens"] is None


def test_s13_meta_answer(base_stack: Stack, edge_login: Callable[..., EdgeClient]) -> None:
    """Closed-set meta phrase over an EMPTY corpus (a populated corpus would
    trip the resolver's single-product fallback and veto the meta gate)."""
    client = edge_login(base_stack)

    response = client.http.post("/query", json={"question": "What can I ask about?"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "meta"
    assert payload["refused"] is False
    assert payload["citations"] == []
    assert payload["answer"]

    row = _one_new_row(payload, client)
    assert row["status"] == "meta"
    assert row["refused"] is False
    assert row["citations_json"] == []
    assert row["retrieved_json"] == []
    assert row["route_json"]["response_mode"] == "meta"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cost_usd"] is None
