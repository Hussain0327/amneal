"""S6-S13 + S30: every /query outcome branch returns 200 and writes EXACTLY
one audit row, with the column-level shapes the Go CompleteQuery cutover must
reproduce. S30 (GAP-3) proves the INV-5 filters whitelist runs at the edge,
via the audit row's route_json.

Status-code-only tests would silently un-pin: the reason strings, the
retrieved_json empty-vs-populated distinction, the token/cost discipline
(NULL when no model ran, zeros when either echo model ran, cost never
guessed), route_json's exact key set, and the exactly-ONE-row property itself.
Each is an explicit assertion here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWER_ROUTE_JSON_KEYS,
    ANSWERABLE_QUESTION,
    CITATION_KEYS,
    GUIDED_ROUTE_JSON_KEYS,
    LOW_SCORE_GUIDANCE_TEXT,
    MULTIFORM_QUESTION,
    NO_PRODUCT_GUIDANCE_TEXT,
    QUERY_RESPONSE_KEYS,
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

_GUIDED_REASONS = frozenset(
    {
        "ambiguous_product",
        "brand_lookup",
        "did_you_mean",
        "low_top_score",
        "meta",
        "mixed_products",
        "multi_form",
        "no_product",
        "scope_warning",
        "vague_input",
    }
)


def _assert_prompt_identity(route_json: dict[str, Any], *, prompt_id: str, version: str) -> None:
    """Pin prompt identity without importing the implementation manifest."""
    prompt = route_json["prompt"]
    assert set(prompt) == {"prompt_id", "version", "sha256"}
    assert prompt["prompt_id"] == prompt_id
    assert prompt["version"] == version
    assert re.fullmatch(r"[0-9a-f]{64}", prompt["sha256"])


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
    if payload["status"] in {"answer", "summary"}:
        expected_route_keys = ANSWER_ROUTE_JSON_KEYS
    elif payload["reason"] in _GUIDED_REASONS:
        expected_route_keys = GUIDED_ROUTE_JSON_KEYS
    else:
        expected_route_keys = ROUTE_JSON_KEYS
    assert set(row["route_json"].keys()) == expected_route_keys
    if payload["status"] in {"answer", "summary"}:
        _assert_prompt_identity(row["route_json"], prompt_id="regwatch.grounded_qa", version="4")
        # A deliberate pin, never a mirror of the source: changing the prompt
        # identity must be a conscious edit here. "4" adds the helpful,
        # partial-evidence behavior while retaining the structured claim gate.
        assert isinstance(row["route_json"]["partial_evidence"], bool)
        # The gate ledger reached the row, and its verdict agrees with the
        # turn that was served: an answer/summary is only rendered when at
        # least one claim was admitted.
        turn = row["route_json"]["turn"]
        assert turn["verdict"] in {"answer", "partial"}
        assert turn["admitted"] >= 1
    return row


def _assert_echo_guidance(row: dict[str, Any], *, next_step: str, option_ids: list[str]) -> None:
    """A healthy non-answer turn ran exactly the bounded router-model path."""
    _assert_prompt_identity(row["route_json"], prompt_id="regwatch.query_guidance", version="1")
    guidance = row["route_json"]["guidance"]
    assert {key: guidance[key] for key in ("attempted", "applied", "next_step", "option_ids")} == {
        "attempted": True,
        "applied": True,
        "next_step": next_step,
        "option_ids": option_ids,
    }
    selected = guidance["selected_options"]
    assert [option["id"] for option in selected] == option_ids
    assert all(set(option) == {"id", "label", "channel"} for option in selected)
    assert all(option["id"].startswith(f"{option['channel']}:") for option in selected)
    assert row["model_name"] == "echo"
    # Echo reports real zero usage. NULL would mean no model call occurred.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["cost_usd"] is None


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
    assert payload["answer"] == NO_PRODUCT_GUIDANCE_TEXT
    assert payload["citations"] == []
    assert payload["related"] == []

    row = _one_new_row(payload, client)
    assert row["status"] == "refused"
    assert row["refused"] is True
    assert row["answer_text"] == NO_PRODUCT_GUIDANCE_TEXT
    assert row["retrieved_json"] == []
    assert row["citations_json"] == []
    assert row["route_json"]["reason"] == "no_product"
    _assert_echo_guidance(row, next_step="name_product", option_ids=[])

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
    assert payload["answer"] == LOW_SCORE_GUIDANCE_TEXT
    assert payload["citations"] == []
    assert payload["related"], "sub-threshold evidence still yields related product pointers"

    row = _one_new_row(payload, client)
    assert row["status"] == "refused"
    assert row["answer_text"] == LOW_SCORE_GUIDANCE_TEXT
    assert row["route_json"]["reason"] == "low_top_score"
    assert row["retrieved_json"], "the weak evidence is the point of this row"
    _assert_echo_guidance(row, next_step="narrow_source_topic", option_ids=["related:0"])


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
    _assert_echo_guidance(
        row,
        next_step="narrow_source_topic",
        option_ids=["clarify:0", "clarify:1", "clarify:2"],
    )


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
    _assert_echo_guidance(
        row,
        next_step="choose_dosage_form",
        option_ids=["clarify:0", "clarify:1"],
    )


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
    _assert_echo_guidance(
        row,
        next_step="ask_evidence_question",
        option_ids=["related:0", "related:1", "related:2"],
    )


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
    _assert_echo_guidance(row, next_step="view_capabilities", option_ids=[])


def test_s30_filters_whitelist_holds_at_the_edge(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """GAP-3, INV-5 at the wire: a poisoned filters object -- a version_id
    that would defeat current-version scoping, a legacy source_url, an unknown
    key, and a non-scalar value on a whitelisted key -- is silently REDUCED to
    the whitelisted scalars (never a 422: old clarify echoes must keep
    working), and the audit row's route_json proves the whitelist ran BEFORE
    compute (/internal/query/compute TRUSTS its filters)."""
    seed_answerable_corpus()
    client = edge_login(base_stack)

    response = client.http.post(
        "/query",
        json={
            "question": ANSWERABLE_QUESTION,
            "filters": {
                "normalized_name": "albuterol sulfate",  # whitelisted scalar: kept
                "version_id": 17,  # not whitelisted: dropped
                "source_url": "http://x",  # legacy key: dropped
                "page": 3,  # unknown key: dropped
                "dosage_form": ["Gel", "Cream"],  # whitelisted key, non-scalar: dropped
            },
        },
    )
    assert response.status_code == 200, "poisoned filters are dropped, never 422'd"
    payload = response.json()
    assert payload["status"] == "answer", "the surviving filter still scopes to the seed"

    row = _one_new_row(payload, client)
    assert row["route_json"]["filters"] == {"normalized_name": "albuterol sulfate"}
