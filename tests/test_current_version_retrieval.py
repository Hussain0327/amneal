"""Current-version retrieval invariants."""

from __future__ import annotations

from datetime import UTC, datetime

from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import retrieve
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.store.vector_store import add_chunks


def test_retrieve_filters_out_stale_psg_versions_even_if_chunks_remain() -> None:
    init_db()
    with session_scope() as s:
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
        s.add(doc)
        s.flush()
        assert doc.id is not None
        old = PsgVersion(
            psg_document_id=doc.id,
            content_hash="old",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        current = PsgVersion(
            psg_document_id=doc.id,
            content_hash="new",
            captured_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        s.add(old)
        s.add(current)
        s.flush()
        assert old.id is not None
        assert current.id is not None
        doc_id = doc.id
        old_version_id = old.id
        current_version_id = current.id

    texts = [
        "Obsolete PSG text: old fasting study language.",
        "Current PSG text: updated fasting study language.",
    ]
    embeddings = get_embedding_provider().embed(texts)
    add_chunks(
        ids=["old-chunk", "current-chunk"],
        embeddings=embeddings,
        documents=texts,
        metadatas=[
            {
                "doc_id": doc_id,
                "version_id": old_version_id,
                "page": 1,
                "normalized_name": "albuterol sulfate",
                "appl_no": "020503",
                "source_url": "https://example.invalid/PSG_020503.pdf",
                "section_path": "",
            },
            {
                "doc_id": doc_id,
                "version_id": current_version_id,
                "page": 1,
                "normalized_name": "albuterol sulfate",
                "appl_no": "020503",
                "source_url": "https://example.invalid/PSG_020503.pdf",
                "section_path": "",
            },
        ],
    )

    passages = retrieve(
        "old fasting study language",
        k=5,
        filters={"normalized_name": "albuterol sulfate"},
    )

    assert passages
    assert {p.version_id for p in passages} == {current_version_id}
    assert all("Obsolete PSG text" not in p.text for p in passages)


def test_retrieve_respects_explicit_zero_k() -> None:
    assert retrieve("anything", k=0) == []
