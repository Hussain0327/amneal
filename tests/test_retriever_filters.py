"""Server-side case-insensitivity of catalog filters (dosage_form / route / psg_type).

The corpus stores FDA listing values verbatim ("Aerosol, Metered", "Inhalation")
while a hand-typed UI filter arrives in whatever casing the user chose. Filters
must match case-insensitively on the server, or a naturally-typed value silently
retrieves nothing and surfaces as a spurious no-passages refusal. The clarify-pick
round-trip (exact stored casing) must keep working unchanged.
"""

from __future__ import annotations

from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import retrieve
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.store.queries import current_dosage_form_routes
from regwatch.store.vector_store import add_chunks


def _seed_albuterol() -> None:
    """One albuterol doc + chunk with FDA title-case dosage_form/route metadata."""
    init_db()
    with session_scope() as s:
        doc = PsgDocument(
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            appl_no="020503",
            psg_type="draft",
            recommended_date="2026-05-21",
            source_url="https://example.invalid/PSG_020503.pdf",
            content_hash="h1",
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        ver = PsgVersion(psg_document_id=doc.id, content_hash="h1")
        s.add(ver)
        s.flush()
        assert ver.id is not None
        doc_id = doc.id
        version_id = ver.id

    texts = ["albuterol fasting single-dose two-way crossover BE study"]
    add_chunks(
        ids=["020503-1"],
        embeddings=get_embedding_provider().embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": doc_id,
                "version_id": version_id,
                "page": 1,
                "normalized_name": "albuterol sulfate",
                "appl_no": "020503",
                "source_url": "https://example.invalid/PSG_020503.pdf",
                "section_path": "",
                "dosage_form": "Aerosol, Metered",
                "route": "Inhalation",
                "psg_type": "draft",
            }
        ],
    )


def test_retrieve_matches_hand_typed_lowercase_filters() -> None:
    _seed_albuterol()

    passages = retrieve(
        "fasting BE study design",
        k=5,
        filters={
            "normalized_name": "albuterol sulfate",
            "dosage_form": "aerosol, metered",
            "route": "inhalation",
            "psg_type": "Draft",
        },
    )

    assert passages
    assert {p.normalized_name for p in passages} == {"albuterol sulfate"}


def test_retrieve_stored_casing_round_trip_unbroken() -> None:
    # The clarify options carry the exact stored values; picking one must keep
    # matching (case folding is a widening, never a narrowing).
    _seed_albuterol()

    passages = retrieve(
        "fasting BE study design",
        k=5,
        filters={
            "normalized_name": "albuterol sulfate",
            "dosage_form": "Aerosol, Metered",
            "route": "Inhalation",
        },
    )

    assert passages


def test_retrieve_unknown_dosage_form_still_matches_nothing() -> None:
    # A value that is not a stored FDA dosage form under ANY casing keeps
    # matching nothing -- case folding must not loosen the exact-match contract.
    _seed_albuterol()

    passages = retrieve(
        "fasting BE study design",
        k=5,
        filters={
            "normalized_name": "albuterol sulfate",
            "dosage_form": "inhalation aerosol",
        },
    )

    assert passages == []


def test_current_dosage_form_routes_pins_combo_case_insensitively() -> None:
    _seed_albuterol()

    combos = current_dosage_form_routes(
        "albuterol sulfate",
        dosage_form="aerosol, metered",
        route="INHALATION",
    )

    assert combos == [("Aerosol, Metered", "Inhalation")]
