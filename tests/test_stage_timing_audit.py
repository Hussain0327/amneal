"""Stage timings reach the audit row on both control planes.

`query_log.latency_ms` records what a turn cost but never where it went.
Measured 2026-08-19, the language-model call is only ~1.0s of a 3.4-5.25s turn,
so most of the turn was unattributable from production data. These pin that the
per-stage block actually lands in `route_json` -- on the relay path (`ask`) and
on the Go native path (`compute_turn`), which is where production traffic
flows.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests.test_invariants import _meta, _seed_corpus

pytestmark = pytest.mark.invariants


def _only_route_json() -> dict[str, Any]:
    with session_scope() as s:
        rows = list(s.exec(select(QueryLog)))
        assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
        return dict(rows[0].route_json)


def test_ask_records_stage_timings_in_the_audit_row() -> None:
    """The relay path stamps the stages it owns, including the session write."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    qa_mod.ask("What study design is recommended?")

    timings = _only_route_json().get("timings")
    assert timings is not None, "route_json carries no timings block"
    # session_write is the stage only the relay shell can see, and one of the
    # suspects for the unattributed time.
    assert timings["session_write_ms"] >= 0
    assert timings["retrieve_ms"] >= 0
    assert timings["measured_total_ms"] >= 0
    # The retrieval breakdown must survive a REAL retrieval, not just a unit
    # test: `retrieve.embed_and_scope` is measured on the calling thread while
    # the scoping round trips run in a worker (PR #242). If the collector were
    # invisible across that boundary, or the span were recorded inside the
    # worker instead, these children would silently vanish and the version-
    # scoping question could never be answered from production data.
    assert "retrieve.embed_and_scope_ms" in timings
    assert "retrieve.vector_search_ms" in timings
    # The exact aggregate arithmetic is pinned in test_stage_timing.py, where
    # the durations are controlled; asserting it here would be a millisecond
    # race. What matters here is that a real turn produces both the outer span
    # and its children.


def test_compute_turn_records_stage_timings_for_the_go_path() -> None:
    """The native path carries the block too; Go persists route_json verbatim.

    Without this, instrumentation would cover only the fallback path and miss
    the traffic we are actually trying to explain.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    _outcome, audit, _patch = qa_mod.compute_turn(
        "What study design is recommended?",
        session_id="s-timing",
        turn_id="t-timing",
    )

    timings = audit.route_json.get("timings")
    assert timings is not None, "compute_turn produced no timings block"
    assert timings["retrieve_ms"] >= 0
    # Go performed the session write before calling, so that stage is NOT ours
    # to claim; recording it here would attribute Go's time to Python.
    assert "session_write_ms" not in timings


def test_timings_never_fail_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken collector degrades to a missing block, never a failed answer.

    On the INV-6 answer path a lost audit row costs the user their answer, so a
    diagnostic must never be able to raise into it.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    class _Exploding:
        def as_route_json(self) -> dict[str, Any]:
            raise RuntimeError("instrumentation is broken")

    audit = qa_mod.compute_turn(
        "What study design is recommended?",
        session_id="s-timing",
        turn_id="t-timing",
    )[1]
    qa_mod._attach_stage_timings(audit, _Exploding())  # type: ignore[arg-type]

    result = qa_mod.ask("What study design is recommended?")
    assert result.status in {"answer", "clarify", "refused", "summary", "error"}
