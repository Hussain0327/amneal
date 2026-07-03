"""#2 meta answer performance: the corpus doc count is ONE aggregate query.

`_meta_answer_text` used to sum a per-product `_doc_count` loop -- an N+1 that
issued one COUNT round trip per distinct product (~1.4k on the full catalog)
for every "what's new" / "what can you do" question. These tests pin both the
behavior (the counted total, scoped to the corpus names) and the shape (a
single psg_document statement, independent of the number of products).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event

from regwatch.generate import grounded_qa as qa_mod
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import get_engine, init_db, session_scope
from regwatch.store.models import PsgDocument
from regwatch.store.queries import count_documents
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants


def _seed_docs(names_with_counts: dict[str, int]) -> None:
    """One psg_document row per (name, i); no versions needed for the count."""
    init_db()
    seq = 0
    with session_scope() as s:
        for name, n in names_with_counts.items():
            for i in range(n):
                seq += 1
                appl = f"{900000 + seq:06d}"
                s.add(
                    PsgDocument(
                        active_ingredient=name.title(),
                        normalized_name=name,
                        dosage_form="Tablet",
                        route="Oral",
                        appl_no=appl,
                        psg_type="draft",
                        recommended_date="2026-01-01",
                        source_url=f"http://example/PSG_{appl}.pdf",
                        content_hash=f"hash-{name}-{i}",
                    )
                )


def _seed_chunks(names: list[str]) -> None:
    """Seed the vector store so distinct_metadata_values sees these products."""
    emb = get_embedding_provider()
    texts = [f"BE study guidance for {n}." for n in names]
    add_chunks(
        ids=[f"meta-chunk-{i}" for i in range(len(names))],
        embeddings=emb.embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": i + 1,
                "version_id": (i + 1) * 10,
                "page": 1,
                "normalized_name": n,
                "appl_no": f"00{i}001",
                "source_url": f"http://example/{i}.pdf",
                "section_path": "",
                "dosage_form": "Tablet",
                "route": "Oral",
                "psg_type": "draft",
            }
            for i, n in enumerate(names)
        ],
    )


def test_count_documents_scopes_to_names_and_skips_db_when_empty() -> None:
    _seed_docs({"alpha drug": 2, "beta drug": 1, "delta drug": 1})
    assert count_documents([]) == 0
    assert count_documents(["", "missing drug"]) == 0
    assert count_documents(["alpha drug"]) == 2
    assert count_documents(["alpha drug", "beta drug"]) == 3
    # Names outside the set are excluded -- it is an IN count, not a table COUNT.
    assert count_documents(["alpha drug", "missing drug"]) == 2


def test_meta_answer_counts_corpus_docs_in_one_query() -> None:
    """Behavior parity with the old per-product sum, in ONE psg_document
    statement regardless of how many products the corpus holds."""
    # 3 corpus products / 4 docs; "delta drug" has a doc row but NO chunks, so
    # it is outside the askable corpus and must not be counted.
    _seed_docs({"alpha drug": 2, "beta drug": 1, "gamma drug": 1, "delta drug": 3})
    _seed_chunks(["alpha drug", "beta drug", "gamma drug"])

    engine = get_engine()
    doc_statements: list[str] = []

    def _capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
    ) -> None:
        if "psg_document" in statement:
            doc_statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        text = qa_mod._meta_answer_text("what can you do")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert "3 products" in text
    assert "(4 documents)" in text
    # The N+1 is gone: one aggregate statement, not one per product.
    assert len(doc_statements) == 1
