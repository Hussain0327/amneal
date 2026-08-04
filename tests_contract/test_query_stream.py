"""S19-S21a + S23: /query/stream through the Go edge.

R3 scoping (docs/POLYGLOT_TARGET_2026-07-10.md): during the pass-through era
these tests assert only the KEEP-FOREVER frame grammar -- event vocabulary,
exactly one terminal result frame (last), refusals stream zero tokens,
blocking/stream parity, one audit row after EOF. Deliberately NOT asserted
(they change when Go takes the terminal frame): status-frame text, keep-alive
presence/cadence (S22: the parser tolerates comment frames anywhere),
Cache-Control / X-Accel-Buffering headers, frame timing, token-frame counts
beyond >=1 / ==0, and who writes the audit row relative to the frames.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tests_contract.conftest import (
    ABSENT_DRUG_QUESTION,
    ANSWERABLE_QUESTION,
    DEAD_PROVIDER_TIMEOUT,
    GUIDED_ROUTE_JSON_KEYS,
    NO_PRODUCT_GUIDANCE_TEXT,
    QUERY_RESPONSE_KEYS,
    REFUSAL_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    EdgeClient,
    SseBody,
    Stack,
    latest_query_log_row,
    parse_sse,
    query_log_count,
    seed_answerable_corpus,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]

_ACCEPT_SSE = {"Accept": "text/event-stream"}


def _stream_to_eof(client: EdgeClient, body: dict[str, Any]) -> tuple[int, str, SseBody]:
    """POST the stream route and read to EOF; the relay has no response
    timeout, so the client's own bounded timeout is what protects the suite."""
    with client.http.stream("POST", "/query/stream", json=body, headers=_ACCEPT_SSE) as response:
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        text = "".join(response.iter_text())
    return status, content_type, parse_sse(text)


def test_s19_stream_grammar_and_blocking_parity(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    seed_answerable_corpus()
    client = edge_login(base_stack)

    rows_before = query_log_count()
    status, content_type, sse = _stream_to_eof(client, {"question": ANSWERABLE_QUESTION})
    assert status == 200
    assert "text/event-stream" in content_type

    events = sse.events()
    assert set(events) <= {"status", "token", "result"}, f"unknown event name in {events}"
    assert events.count("status") >= 1
    assert events.count("token") >= 1, "an answer turn replays its rendered answer as tokens"
    result = sse.single_result()  # exactly one result frame, and it is last

    assert set(result.keys()) == QUERY_RESPONSE_KEYS
    assert result["status"] == "answer"
    # INV-1 at the frame level: status frames are progress prose only -- never
    # answer text or citation markers.
    for data in sse.data_for("status"):
        frame = json.loads(data)
        assert set(frame.keys()) == {"text"}
        assert "[PSG_" not in frame["text"]
        assert "ECHO:" not in frame["text"], "answer prose must never ride the status channel"
    for data in sse.data_for("token"):
        assert set(json.loads(data).keys()) == {"delta"}
    # Token frames are a REPLAY of the gated, audited answer, not a live model
    # stream: over the real wire the deltas must reassemble to exactly the bytes
    # the terminal frame carries. If synthesis ever went back to streaming raw
    # model output, the deltas would be the model's draft and this would fail.
    replayed = "".join(json.loads(data)["delta"] for data in sse.data_for("token"))
    assert replayed == result["answer"]

    assert query_log_count() == rows_before + 1, "exactly one audit row per streamed turn"

    # Parity: the terminal frame is the same QueryResponse the blocking route
    # builds (shared serializer today; the pin that keeps Go's future terminal
    # frame honest). Per-turn ids are excluded by construction.
    blocking = client.http.post("/query", json={"question": ANSWERABLE_QUESTION}).json()
    for key in ("answer", "refused", "status", "citations"):
        assert result[key] == blocking[key], f"stream/blocking divergence on {key!r}"


def test_s20_refusal_streams_zero_token_frames(
    base_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The hold keeps trusted AI-selected guidance out of the token channel:
    the no-product guidance appears ONLY inside the terminal result frame."""
    client = edge_login(base_stack)

    status, content_type, sse = _stream_to_eof(client, {"question": ABSENT_DRUG_QUESTION})
    assert status == 200
    assert "text/event-stream" in content_type

    assert sse.events().count("token") == 0
    result = sse.single_result()
    assert result["status"] == "refused"
    assert result["refused"] is True
    assert result["citations"] == []
    assert result["answer"] == NO_PRODUCT_GUIDANCE_TEXT
    # Not in any status frame either -- only the result carries it.
    for data in sse.data_for("status"):
        assert NO_PRODUCT_GUIDANCE_TEXT not in data

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == result["audit_id"]
    assert row["answer_text"] == NO_PRODUCT_GUIDANCE_TEXT
    assert set(row["route_json"]) == GUIDED_ROUTE_JSON_KEYS
    assert row["route_json"]["prompt"]["prompt_id"] == "regwatch.query_guidance"
    assert row["route_json"]["prompt"]["version"] == "1"
    assert row["route_json"]["guidance"] == {
        "attempted": True,
        "applied": True,
        "next_step": "name_product",
        "option_ids": [],
        "selected_options": [],
    }
    # Echo reports real zero usage; NULL would mean the router was bypassed.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["cost_usd"] is None


def test_s21a_provider_death_mid_pipeline_still_ends_with_a_result_frame(
    dead_provider_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The audited-degrade contract holds over SSE: the stream terminates with
    the defined error result frame (never a silent truncation for this CAUGHT
    failure family) and the provider_error row is committed by EOF."""
    seed_answerable_corpus()
    client = edge_login(dead_provider_stack, timeout=DEAD_PROVIDER_TIMEOUT)

    status, content_type, sse = _stream_to_eof(client, {"question": ANSWERABLE_QUESTION})
    assert status == 200
    assert "text/event-stream" in content_type

    result = sse.single_result()
    assert result["status"] == "error"
    assert result["reason"] == "provider_error"
    assert result["refused"] is True
    assert result["answer"] == SERVICE_UNAVAILABLE_TEXT, "the fixed copy, never the exception"

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == result["audit_id"]
    assert row["status"] == "error"
    assert row["route_json"]["reason"] == "provider_error"


def test_s23_synthesis_refusal_streams_zero_tokens_and_lands_as_model_refusal(
    forced_refusal_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """The post-synthesis decline hold, reached over the wire: echo is forced to
    emit a NO_EVIDENCE turn (REGWATCH_ECHO_FORCE_REFUSAL), so this is the only
    scenario where the SYNTHESIZER decides the decline. S20's zero-token refusal
    is decided pre-synthesis and never exercises the hold -- without this test,
    widening the token replay from `status in ("answer","summary")` to every
    turn stays green while a real model refusal would paint as a
    grounded-looking draft for a beat and then vanish (the INV-2 UX failure)."""
    seed_answerable_corpus()
    client = edge_login(forced_refusal_stack)

    status, content_type, sse = _stream_to_eof(client, {"question": ANSWERABLE_QUESTION})
    assert status == 200
    assert "text/event-stream" in content_type

    assert sse.events().count("token") == 0, "the hold: a refusal must never stream as tokens"
    result = sse.single_result()
    assert set(result.keys()) == QUERY_RESPONSE_KEYS
    # The single-product-fallback model_refusal branch: no drug was named in
    # the question (resolution came from the sole-product corpus), so a model
    # refusal stays REFUSED rather than guiding to a clarify.
    assert result["status"] == "refused"
    assert result["reason"] == "model_refusal"
    assert result["refused"] is True
    assert result["citations"] == []
    assert result["answer"] == REFUSAL_TEXT
    # The refusal text appears ONLY in the terminal result frame.
    for data in sse.data_for("status"):
        assert REFUSAL_TEXT not in data

    assert query_log_count() == 1, "exactly one audit row for the refused streamed turn"
    row = latest_query_log_row()
    assert row["id"] == result["audit_id"]
    assert row["status"] == "refused"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "model_refusal"
    assert row["retrieved_json"], "synthesis ran, so the retrieved evidence is audited"

    # Blocking parity: POST /query walks the same branch to the same shape.
    blocking = client.http.post("/query", json={"question": ANSWERABLE_QUESTION}).json()
    for key in ("answer", "refused", "status", "reason", "citations"):
        assert result[key] == blocking[key], f"stream/blocking divergence on {key!r}"
    assert query_log_count() == 2
