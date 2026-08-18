"""Graph tables are write-only: the serving retrieval path never reads them.

Migration 0018 created graph_node/graph_edge/graph_node_chunk, and the only
writers are `regwatch graph-backfill` and the ingest pipeline (Tier-1,
write-only by design -- the D1 flip was moved OFF the graph path). Retrieval
cost and blast radius are therefore pinned to the chunk table(s): a reader
sneaking into retrieve() would silently couple query latency and correctness
to tables no serving code maintains. These tests run the REAL retrieve() --
no store stubs -- while capturing every SQL statement at the engine, and fail
on the first statement that references a graph table. Both corpus arms are
covered (legacy and authoritative_fda).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event

from regwatch.retrieve.retriever import retrieve
from regwatch.store.db import get_engine, init_db
from tests.test_invariants import _meta, _seed_corpus

_GRAPH_TABLES = ("graph_node_chunk", "graph_node", "graph_edge")
_QUESTION = "What study design is recommended?"


def _capture_retrieve_sql() -> list[str]:
    """Runs a real retrieve() and returns every SQL statement it executed.

    The listener sits on the ONE shared engine (pgvector_store memoizes the
    same instance), so the vector search, the version-scoping queries, and the
    scoping worker thread's statements are all captured.
    """
    engine = get_engine()
    statements: list[str] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        retrieve(_QUESTION)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return statements


def _assert_no_graph_reads(statements: list[str]) -> None:
    # Positive control first: the capture saw the vector search itself (its
    # SQL names the chunk table), so an empty or misattached capture can never
    # fake a pass.
    assert any("chunk" in s for s in statements), "capture missed the vector search"
    offenders = [s for s in statements if any(t in s for t in _GRAPH_TABLES)]
    assert offenders == [], f"retrieval consulted graph tables: {offenders}"


def test_legacy_arm_reads_no_graph_tables() -> None:
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3))])
    _assert_no_graph_reads(_capture_retrieve_sql())


def test_authoritative_arm_reads_no_graph_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    # validation_alias, not the field name: settings.py binds retrieval_corpus
    # to REGWATCH_RETRIEVAL_CORPUS (same pattern as
    # test_retriever_scoping_overlap.py). The autouse fixture re-clears the
    # settings cache for the next test.
    monkeypatch.setenv("REGWATCH_RETRIEVAL_CORPUS", "authoritative_fda")
    cs.get_settings.cache_clear()
    # No seeding needed: the arm's search SQL executes (and is captured) even
    # over zero authoritative rows.
    init_db()
    _assert_no_graph_reads(_capture_retrieve_sql())
