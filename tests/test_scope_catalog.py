"""Catalog expansion for bounded route-shadow corpus policies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from regwatch.generate.route import CorpusPolicyHint
from regwatch.retrieve.scope_catalog import (
    CorpusPolicyCatalogError,
    load_corpus_policy_snapshots,
)
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion


def _add_document(
    *,
    appl_no: str | None,
    route: str,
    current_hash: str,
    with_version: bool = True,
) -> int:
    with session_scope() as session:
        document = PsgDocument(
            active_ingredient=f"ingredient-{appl_no}",
            normalized_name=f"ingredient-{appl_no}",
            dosage_form="Aerosol, Metered" if "inhal" in route.lower() else "Tablet",
            route=route,
            appl_no=appl_no,
            psg_type="draft",
            source_url=f"https://example.test/{appl_no or 'missing'}.pdf",
            content_hash=current_hash,
        )
        session.add(document)
        session.flush()
        assert document.id is not None
        doc_id = int(document.id)
        if with_version:
            session.add(
                PsgVersion(
                    psg_document_id=doc_id,
                    content_hash=current_hash,
                    captured_at=datetime(2026, 8, 10, tzinfo=UTC),
                )
            )
    return doc_id


def test_policy_expands_only_inhalation_docs_at_each_latest_version() -> None:
    init_db()
    first_id = _add_document(
        appl_no="020503",
        route="Inhalation",
        current_hash="current-020503",
    )
    second_id = _add_document(
        appl_no="020911",
        route="Respiratory (Inhalation)",
        current_hash="current-020911",
    )
    _add_document(appl_no="999001", route="Oral", current_hash="oral-current")
    with session_scope() as session:
        session.add(
            PsgVersion(
                psg_document_id=first_id,
                content_hash="old-020503",
                captured_at=datetime(2026, 8, 9, tzinfo=UTC),
            )
        )

    snapshot = load_corpus_policy_snapshots()[CorpusPolicyHint.INHALATION_PSG]

    assert [document.doc_id for document in snapshot.documents] == [first_id, second_id]
    assert [document.appl_no for document in snapshot.documents] == ["020503", "020911"]
    assert [document.short_name for document in snapshot.documents] == [
        "PSG_020503",
        "PSG_020911",
    ]
    assert all(document.is_current for document in snapshot.documents)
    assert snapshot.validation_failure() is None


def test_policy_with_no_matching_documents_is_known_but_empty() -> None:
    init_db()
    _add_document(appl_no="999001", route="Oral", current_hash="oral-current")

    snapshot = load_corpus_policy_snapshots()[CorpusPolicyHint.INHALATION_PSG]

    assert snapshot.documents == ()


@pytest.mark.parametrize("failure", ["missing_application", "missing_version", "hash_mismatch"])
def test_incomplete_policy_provenance_fails_instead_of_silently_shrinking(
    failure: str,
) -> None:
    init_db()
    if failure == "missing_application":
        _add_document(appl_no=None, route="Inhalation", current_hash="current")
    elif failure == "missing_version":
        _add_document(
            appl_no="020503",
            route="Inhalation",
            current_hash="current",
            with_version=False,
        )
    else:
        doc_id = _add_document(
            appl_no="020503",
            route="Inhalation",
            current_hash="document-current",
            with_version=False,
        )
        with session_scope() as session:
            session.add(
                PsgVersion(
                    psg_document_id=doc_id,
                    content_hash="version-other",
                    captured_at=datetime.now(UTC) + timedelta(seconds=1),
                )
            )

    with pytest.raises(CorpusPolicyCatalogError):
        load_corpus_policy_snapshots()
