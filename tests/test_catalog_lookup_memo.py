"""Per-process memo for catalog presence probes on the ask path.

``db.table_exists`` replaced three ``inspect().has_table`` pairs that re-asked
Postgres "does psg_document/psg_version exist" on every turn. The memo caches
PRESENCE only; caching absence would latch a hot process into the
catalog-absent branch and silently disable current-version scoping.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, event

from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import retrieve
from regwatch.store import db
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.store.vector_store import add_chunks

# SQLAlchemy's postgresql has_table() resolves the name through this catalog
# relation, so its presence in a statement is the has_table round trip.
_CATALOG_PROBE = "pg_catalog.pg_class"
_QUESTION = "What fasting bioequivalence study is recommended?"
_CHUNK_TEXT = "Current PSG text: a fasting bioequivalence study is recommended."


@contextmanager
def _capture(engine: Engine) -> Iterator[list[str]]:
    """Collects every SQL statement executed on ``engine`` inside the block.

    Args:
        engine: The one shared engine. retrieve()'s scoping worker thread runs
            on it too, so a single listener sees the whole turn.

    Yields:
        The live list of statement texts, in execution order.
    """
    statements: list[str] = []

    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def _seed_scoped_corpus() -> int:
    """Seeds one PSG document with a current version and a chunk for it.

    The catalog rows are what make retrieval version-scoped: without a
    ``psg_document`` row the scoping helper short-circuits and the vector query
    carries no ``version_id`` clause, so the SQL assertion would pass vacuously.

    Returns:
        The seeded current ``psg_version`` id.
    """
    db.init_db()
    with db.session_scope() as session:
        doc = PsgDocument(
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            rld_or_rs_number="020503",
            psg_type="draft",
            recommended_date="2026-05-21",
            source_url="https://example.invalid/PSG_020503.pdf",
            content_hash="new",
        )
        session.add(doc)
        session.flush()
        assert doc.id is not None
        version = PsgVersion(
            psg_document_id=doc.id,
            content_hash="new",
            captured_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        session.add(version)
        session.flush()
        assert version.id is not None
        doc_id = doc.id
        version_id = version.id

    add_chunks(
        ids=["current-chunk"],
        embeddings=get_embedding_provider().embed([_CHUNK_TEXT]),
        documents=[_CHUNK_TEXT],
        metadatas=[
            {
                "doc_id": doc_id,
                "version_id": version_id,
                "page": 1,
                "normalized_name": "albuterol sulfate",
                "appl_no": "020503",
                "source_url": "https://example.invalid/PSG_020503.pdf",
                "section_path": "",
            }
        ],
    )
    return version_id


def test_table_exists_caches_presence_but_never_absence() -> None:
    db.init_db()
    # reset_for_tests is the memo's only invalidation seam; calling it here is
    # what makes this test start from the same state as a fresh process.
    db.reset_for_tests()
    engine = db.get_engine()

    with _capture(engine) as present:
        assert db.table_exists("psg_document") is True
        assert db.table_exists("psg_document") is True
    with _capture(engine) as absent:
        assert db.table_exists("regwatch_no_such_table") is False
        assert db.table_exists("regwatch_no_such_table") is False

    assert len([s for s in present if _CATALOG_PROBE in s]) == 1
    # The compliance guard: a cached False for psg_document would permanently
    # skip current-version scoping in this process and make superseded PSG
    # chunks citable, so absence must keep costing a probe.
    assert len([s for s in absent if _CATALOG_PROBE in s]) == 2


def test_second_retrieve_issues_no_catalog_probes_and_stays_version_scoped() -> None:
    db.reset_for_tests()
    version_id = _seed_scoped_corpus()
    engine = db.get_engine()

    warm = retrieve(_QUESTION)
    assert [p.version_id for p in warm] == [version_id]

    with _capture(engine) as statements:
        passages = retrieve(_QUESTION)

    assert [p.version_id for p in passages] == [version_id]
    assert [s for s in statements if _CATALOG_PROBE in s] == []
    # Positive control plus the invariant the removed probes were guarding: the
    # vector search still ran, and it is still scoped to current versions.
    vector_selects = [s for s in statements if "FROM chunk" in s and "AS distance" in s]
    assert len(vector_selects) == 1
    assert "version_id = ANY(" in vector_selects[0]
