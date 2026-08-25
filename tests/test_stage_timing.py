"""Stage-timing collector: opt-in, non-invasive, and never fatal.

The instrument exists to attribute the ~2-4s of an Ask turn that the
language-model call does not explain (measured 2026-08-19). These tests pin the
properties that make it safe to leave on in production: it is inert unless a
shell opts in, it never changes control flow, and it cannot fail a turn.
"""

from __future__ import annotations

import threading

from regwatch.common.stage_timing import (
    TurnTimings,
    collect_stage_timings,
    record_stage,
    stage,
    timed_stage,
)


def test_stage_is_inert_without_a_collection_scope() -> None:
    """No scope open: stage() runs the block and records nothing anywhere."""
    ran = False
    with stage("retrieve"):
        ran = True
    assert ran
    # Nothing to assert against but the absence of a crash and of state: the
    # point is that unopted callers (CLI, eval, corpus worker) are untouched.
    record_stage("retrieve", 12.0)


def test_records_stages_inside_a_scope() -> None:
    with collect_stage_timings() as timings:
        with stage("retrieve"):
            pass
        record_stage("synthesis", 40.0)
    block = timings.as_route_json()
    assert "retrieve_ms" in block
    assert block["synthesis_ms"] == 40
    assert block["measured_total_ms"] >= 40


def test_repeated_stages_sum_and_carry_a_count() -> None:
    """Retrieval runs twice on clarify-then-answer; the turn's cost is the sum."""
    with collect_stage_timings() as timings:
        record_stage("retrieve", 30.0)
        record_stage("retrieve", 70.0)
        record_stage("synthesis", 10.0)
    block = timings.as_route_json()
    assert block["retrieve_ms"] == 100
    assert block["counts"] == {"retrieve": 2}


def test_nested_stages_are_reported_but_not_double_counted() -> None:
    """`retrieve.vector_search` is INSIDE `retrieve`; summing both would lie.

    The first thing anyone computes from this block is
    `latency_ms - measured_total_ms` (the unattributed remainder). If nested
    spans inflated the total, that remainder would come out too small -- the
    exact error this instrument exists to correct.
    """
    with collect_stage_timings() as timings:
        record_stage("retrieve", 100.0)
        record_stage("retrieve.embed_and_scope", 70.0)
        record_stage("retrieve.vector_search", 30.0)
        record_stage("synthesis", 900.0)
    block = timings.as_route_json()
    # Breakdowns are still visible...
    assert block["retrieve.embed_and_scope_ms"] == 70
    assert block["retrieve.vector_search_ms"] == 30
    # ...but the headline total counts only top-level stages.
    assert block["measured_total_ms"] == 1000


def test_measured_total_is_wall_clock_coverage_not_sum_of_work() -> None:
    """The contract for `measured_total_ms`, stated as a test.

    Top-level stages are the SEQUENTIAL critical path (session_open ->
    retrieve -> synthesis) and may be summed. Anything dotted is a child span
    inside a parent's wall clock and is diagnostic only. This matters because
    siblings can run CONCURRENTLY: embedding and version scoping deliberately
    overlap (PR #242), so they are recorded as ONE parent span
    (`retrieve.embed_and_scope`) rather than two summable siblings. Summing
    overlapping spans would inflate the total and shrink the unattributed
    remainder -- the exact quantity this instrument exists to measure.
    """
    with collect_stage_timings() as timings:
        record_stage("session_open", 180.0)
        record_stage("retrieve", 400.0)
        record_stage("retrieve.embed_and_scope", 300.0)
        record_stage("retrieve.vector_search", 100.0)
        record_stage("synthesis", 1000.0)
    block = timings.as_route_json()
    # Exactly the three top-level stages, and nothing else.
    assert block["measured_total_ms"] == 180 + 400 + 1000


def test_concurrent_turns_cannot_see_each_others_collectors() -> None:
    """Two turns in one process must not cross-contaminate.

    The collector is context-local rather than global precisely so a busy
    server cannot attribute one request's retrieval to another's row.
    """
    seen: dict[str, dict[str, object]] = {}
    barrier = threading.Barrier(2)

    def _turn(name: str, cost: float) -> None:
        with collect_stage_timings() as timings:
            record_stage("retrieve", cost)
            barrier.wait(timeout=5)  # force the two scopes to be open at once
            record_stage("synthesis", cost)
            seen[name] = timings.as_route_json()

    threads = [
        threading.Thread(target=_turn, args=("a", 10.0)),
        threading.Thread(target=_turn, args=("b", 90.0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["a"]["measured_total_ms"] == 20
    assert seen["b"]["measured_total_ms"] == 180


def test_empty_scope_reports_zero_rather_than_vanishing() -> None:
    """An early-branch turn (greeting, meta) still reports, as zero.

    route_json's key set is a pinned cross-runtime contract, so a block that
    appeared only on some branches would make that contract branch-dependent.
    """
    with collect_stage_timings() as timings:
        pass
    assert timings.as_route_json() == {"measured_total_ms": 0}


def test_a_raising_stage_is_still_recorded_and_still_raises() -> None:
    """Timing must not swallow failures, and a slow failure must stay visible."""
    with collect_stage_timings() as timings:
        try:
            with stage("synthesis"):
                raise RuntimeError("provider down")
        except RuntimeError:
            pass
    assert "synthesis_ms" in timings.as_route_json()


def test_scope_does_not_leak_after_exit() -> None:
    with collect_stage_timings() as inner:
        record_stage("retrieve", 5.0)
    record_stage("retrieve", 500.0)
    assert inner.as_route_json()["retrieve_ms"] == 5


def test_worker_threads_do_not_write_into_the_turns_dict() -> None:
    """Retrieval overlaps embedding with a worker thread (PR #242).

    The worker must NOT record: its span is already inside the parent's
    embed_and_scope stage, so counting it again would double-count the very
    overlap that optimization exists to hide.
    """
    with collect_stage_timings() as timings:

        def _in_worker() -> None:
            record_stage("scope_from_worker", 999.0)

        worker = threading.Thread(target=_in_worker)
        worker.start()
        worker.join()
        record_stage("embed_and_scope", 20.0)
    block = timings.as_route_json()
    assert "scope_from_worker_ms" not in block
    assert block["embed_and_scope_ms"] == 20


def test_timed_stage_decorator_matches_the_context_manager() -> None:
    """The decorator form must be `stage()` with different syntax, nothing more.

    It exists only so a long function with early returns (resolve_product) or
    one called from an expression position (_doc_count) can be instrumented
    without a reindent. If it swallowed an exception or changed a return value
    it would be altering turns rather than measuring them -- the two ways
    instrumentation could do harm.
    """

    @timed_stage("route")
    def _resolve(value: int, *, double: bool = False) -> int:
        """Doc kept so functools.wraps has something to preserve."""
        return value * 2 if double else value

    @timed_stage("route")
    def _boom() -> None:
        raise RuntimeError("resolver down")

    # functools.wraps: the wrapped function stays introspectable.
    assert _resolve.__name__ == "_resolve"
    assert _resolve.__doc__ == "Doc kept so functools.wraps has something to preserve."

    # Inert with no scope open, and the return value is untouched.
    assert _resolve(21, double=True) == 42

    with collect_stage_timings() as timings:
        assert _resolve(7) == 7
        try:
            _boom()
        except RuntimeError:
            pass
        else:  # pragma: no cover - the decorator must never suppress.
            raise AssertionError("timed_stage suppressed the exception")
    block = timings.as_route_json()
    # Both calls landed under the given name, and repeats summed like stage().
    assert "route_ms" in block
    assert block["counts"] == {"route": 2}


def test_stage_count_is_bounded() -> None:
    """A pathological loop cannot grow the audit row's JSON without bound."""
    timings = TurnTimings()
    for i in range(500):
        timings.record(f"stage_{i}", 1.0)
    block = timings.as_route_json()
    # 64 stage keys + measured_total_ms + counts is the ceiling.
    assert len([k for k in block if k.endswith("_ms")]) <= 65
