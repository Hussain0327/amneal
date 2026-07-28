"""query_log.latency_ms: the turn clock the provider-cutover gates read.

Two writers stamp this column -- Python's _persist_turn on the relay/stream
path and Go's auditParams on the native path -- and both must measure the same
interval or the percentile mixes two definitions. These tests pin the Python
half and the NULL-not-zero rule the percentile depends on.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as gq


def test_latency_ms_is_none_without_a_start_stamp() -> None:
    """NULL, never 0.

    A percentile over a column where "unknown" and "instantaneous" both read 0
    understates every gate that consumes it -- which is the one thing this
    column exists to prevent.
    """
    assert gq._latency_ms(None) is None


def test_latency_ms_measures_elapsed_milliseconds() -> None:
    from time import perf_counter

    t0 = perf_counter() - 0.25
    measured = gq._latency_ms(t0)
    assert measured is not None
    # Generous bounds: this asserts the unit is milliseconds (not seconds or
    # microseconds), not the machine's scheduling precision.
    assert 200 <= measured <= 5_000


def test_latency_ms_clamps_to_int4() -> None:
    """The column is integer; an absurd start stamp must not overflow it."""
    assert gq._latency_ms(-1e12) == 2**31 - 1


def _capture_log_query(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_log_query(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 7

    monkeypatch.setattr(gq, "log_query", fake_log_query)
    monkeypatch.setattr(gq, "_apply_session_patch", lambda *a, **k: None)
    return calls


def _payloads() -> tuple[Any, Any, Any]:
    """A minimal strict (non-skip) turn triple straight from the core."""
    outcome, audit, patch = gq._pipeline_error(
        "what dissolution method applies?",
        session_id="s1",
        turn_id="t1",
        user_id=None,
        filters=None,
    )
    return outcome, audit, patch


def test_persist_turn_stamps_latency_when_given_a_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from time import perf_counter

    calls = _capture_log_query(monkeypatch)
    outcome, audit, patch = _payloads()

    gq._persist_turn(outcome, audit, patch, perf_counter() - 0.05)

    assert len(calls) == 1
    assert calls[0]["latency_ms"] is not None
    assert calls[0]["latency_ms"] >= 50


def test_persist_turn_writes_null_latency_without_a_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal callers that do not own a turn clock must write NULL, not 0."""
    calls = _capture_log_query(monkeypatch)
    outcome, audit, patch = _payloads()

    gq._persist_turn(outcome, audit, patch)

    assert len(calls) == 1
    assert calls[0]["latency_ms"] is None


def test_persist_turn_never_drops_a_core_supplied_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """latency_ms is ADDED to the core's kwargs, never substituted for them.

    The core owns every other audit column; a merge bug here would silently
    blank an INV-6 field.
    """
    calls = _capture_log_query(monkeypatch)
    outcome, audit, patch = _payloads()
    expected = set(audit.log_kwargs())

    gq._persist_turn(outcome, audit, patch, 0.0)

    assert expected <= set(calls[0])
    assert set(calls[0]) - expected == {"latency_ms"}
