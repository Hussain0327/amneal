"""Per-stage wall-clock timing for one Ask turn.

Why this exists: `query_log.latency_ms` records what a turn COST but not where
it went. Measured 2026-08-19, the language-model call is only ~1.0s of a
3.4-5.25s turn, and even a clarify turn (61 output tokens, no long answer)
costs ~1.9s -- so most of the turn is fixed per-turn overhead nobody can
currently attribute. These stamps make the remainder addressable from real
production traffic instead of inference.

Collection is CONTEXT-LOCAL and opt-in: `stage()` is a no-op unless a shell has
opened `collect_stage_timings()`, so retrieval and store code can be annotated
without changing a single call signature and without affecting any caller that
does not opt in (tests, CLI, eval, the corpus worker).

Deliberately NOT a metrics system: no counters, no export, no I/O. It appends
integers to a dict that the shell folds into the turn's existing `route_json`.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

# The active collector for the current turn, or None when nobody is collecting.
# A ContextVar (not a global) so concurrent turns in one process cannot write
# into each other's dict. Worker threads start with a fresh context and
# therefore see None -- which is why a stage running in a thread records
# nothing rather than corrupting another turn's numbers.
_ACTIVE: contextvars.ContextVar[TurnTimings | None] = contextvars.ContextVar(
    "regwatch_stage_timings", default=None
)

# Guard against a pathological accumulation (a retry loop annotating the same
# stage forever) turning the audit row's JSON into an unbounded blob.
_MAX_STAGES = 64


class TurnTimings:
    """Accumulates elapsed milliseconds per named stage for one turn.

    A stage may be entered more than once per turn (retrieval runs twice on a
    clarify-then-answer path); repeats SUM, and the call count is kept so an
    average is recoverable. Sums, not last-write-wins: the question this
    answers is "how much of the turn did retrieval own", not "how long did the
    final retrieval take".
    """

    __slots__ = ("_counts", "_elapsed_ms")

    def __init__(self) -> None:
        self._elapsed_ms: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    def record(self, stage: str, elapsed_ms: float) -> None:
        """Adds one observation of `stage`.

        Args:
            stage: Stage name; becomes a key in the audit row's timing block.
            elapsed_ms: Wall time for this observation, in milliseconds.
        """
        if stage not in self._elapsed_ms and len(self._elapsed_ms) >= _MAX_STAGES:
            return
        self._elapsed_ms[stage] = self._elapsed_ms.get(stage, 0.0) + elapsed_ms
        self._counts[stage] = self._counts.get(stage, 0) + 1

    def as_route_json(self) -> dict[str, Any]:
        """Renders the collected stages for `query_log.route_json`.

        Stage names are HIERARCHICAL: a dotted name (`retrieve.vector_search`)
        is a breakdown of its parent (`retrieve`) and its time is already
        counted there. Only top-level stages feed `measured_total_ms`, so the
        headline number stays a true sum instead of counting nested spans twice.

        THE INVARIANT, for anyone adding a stage: `measured_total_ms` is
        wall-clock COVERAGE of the critical path, not a sum of work done. So a
        top-level stage must be SEQUENTIAL with every other top-level stage.
        Work that runs CONCURRENTLY with another span must never become a
        second top-level stage -- summing overlapping spans would inflate the
        total and shrink the unattributed remainder, which is the one number
        this instrument exists to expose. Concurrent work is recorded either as
        one span covering the whole overlap (embedding and version scoping
        overlap by design, so they share `retrieve.embed_and_scope`) or as a
        dotted child, which is reported but never summed.

        Returns:
            A dict of `<stage>_ms` integers, plus `measured_total_ms` (the sum
            of TOP-LEVEL stages only) and a `counts` map for stages entered
            more than once.

            ALWAYS non-empty, even when no stage ran: a turn that branched
            early (greeting, meta) reports `measured_total_ms: 0`, which is a
            fact rather than an absence. The key set of `route_json` is a
            pinned cross-runtime contract (tests_contract), so this block is
            deterministic on every Python-authored row instead of appearing
            and disappearing with the branch taken.

            `measured_total_ms` is still NOT expected to equal the row's
            `latency_ms`: unannotated work and the audit write itself are
            missing from it. The GAP between the two is the point of this
            instrument -- it is the unattributed time.
        """
        block: dict[str, Any] = {
            f"{stage}_ms": int(ms) for stage, ms in sorted(self._elapsed_ms.items())
        }
        block["measured_total_ms"] = int(
            sum(ms for stage, ms in self._elapsed_ms.items() if "." not in stage)
        )
        repeats = {s: n for s, n in sorted(self._counts.items()) if n > 1}
        if repeats:
            block["counts"] = repeats
        return block


@contextmanager
def collect_stage_timings() -> Iterator[TurnTimings]:
    """Opens a collection scope for one turn (shell-owned).

    Yields:
        The collector, so the shell can fold `as_route_json()` into the audit
        row after the turn computes.
    """
    timings = TurnTimings()
    token = _ACTIVE.set(timings)
    try:
        yield timings
    finally:
        _ACTIVE.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Times the enclosed block as `name`, if anyone is collecting.

    A no-op (two ContextVar reads, no allocation) when no scope is open, and
    it NEVER suppresses or alters an exception: timing an operation must not
    change whether that operation succeeds. A stage that raises is still
    recorded, so a slow failure is visible rather than missing.

    Args:
        name: Stage name, e.g. `"retrieve"`.
    """
    timings = _ACTIVE.get()
    if timings is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        timings.record(name, (perf_counter() - started) * 1000.0)


def record_stage(name: str, elapsed_ms: float) -> None:
    """Records an already-measured duration, for callers that time it themselves.

    Args:
        name: Stage name.
        elapsed_ms: Wall time in milliseconds.
    """
    timings = _ACTIVE.get()
    if timings is not None:
        timings.record(name, elapsed_ms)
