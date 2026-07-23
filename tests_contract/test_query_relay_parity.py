"""Relay-path parity for the step-5 cutover (GO_NATIVE_QUERY off).

Every other module runs against the NATIVE Go /query (the deliverable). This
one boots a base stack with the flag OFF -- the Phase-0 default and the instant
rollback path -- and proves /query still serves a grounded answer through the
reverse-relay to Python, so the flag flip is behavior-neutral in both directions.
Deliberately small: the relay code is unchanged by PR B, so a happy-path + an
auth check are enough to catch a wiring regression.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tests_contract.conftest import (
    ANSWERABLE_QUESTION,
    CITATION_KEYS,
    CLIENT_TIMEOUT,
    QUERY_RESPONSE_KEYS,
    EdgeClient,
    Stack,
    latest_query_log_row,
    query_log_count,
    seed_answerable_corpus,
)

pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]


def test_relay_answerable_grounded_answer(
    base_relay_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    """Flag OFF: /query relays to Python and returns the same grounded,
    audited answer shape the native path does."""
    seed_answerable_corpus()
    client = edge_login(base_relay_stack)

    response = client.http.post("/query", json={"question": ANSWERABLE_QUESTION})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == QUERY_RESPONSE_KEYS
    assert payload["status"] == "answer"
    assert payload["refused"] is False
    assert payload["audit_id"] > 0
    assert payload["citations"], "a grounded answer must carry citations"
    for citation in payload["citations"]:
        assert set(citation.keys()) == CITATION_KEYS

    assert query_log_count() == 1
    row = latest_query_log_row()
    assert row["id"] == payload["audit_id"]
    assert row["status"] == "answer"
    assert row["refused"] is False


def test_relay_query_without_cookie_is_401(base_relay_stack: Stack) -> None:
    """Flag OFF: the relayed /query still enforces auth (Python require_user)."""
    response = httpx.post(
        f"{base_relay_stack.edge_url}/query",
        json={"question": ANSWERABLE_QUESTION},
        timeout=CLIENT_TIMEOUT,
    )
    assert response.status_code == 401
    assert query_log_count() == 0
