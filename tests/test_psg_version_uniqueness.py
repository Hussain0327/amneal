"""psg_version (psg_document_id, content_hash) uniqueness + dev-mode semantics.

Migration 0014 / models.PsgVersion add a unique index so the duplicate-revision
race (two overlapping ingest runs landing the same content) collides in the DB
instead of double-recording one FDA change (INV-4). These tests run on the
default SQLite gate: the index exists there too, and the pipeline must map the
violation onto the existing "unchanged" skip path when the colliding row is
the doc's latest version -- and onto a loud "error" when it is an OLDER one
(an FDA revert the schema cannot represent). They also pin the dev-mode
(Chroma) ordering: version commit then chunk indexing, with the unchanged-path
backfill as crash recovery -- dev has no cross-store transaction to lean on.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from regwatch.ingest import pipeline as pipeline_mod
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from tests.test_pipeline_idempotent import (
    PAGES,
    _listing,
    _patch_pipeline_state,
    _row_count,
)


def _seed_doc_with_version(content_hash: str) -> int:
    with session_scope() as s:
        doc = PsgDocument(
            appl_no="020503",
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            psg_type="draft",
            source_url="https://example.invalid/PSG_020503.pdf",
            content_hash=content_hash,
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        s.add(PsgVersion(psg_document_id=doc.id, content_hash=content_hash))
        return doc.id


def test_unique_index_rejects_duplicate_version_rows() -> None:
    """Would fail if the model/migration lost the (doc, hash) unique index."""
    init_db()
    doc_id = _seed_doc_with_version("hash-1")
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(PsgVersion(psg_document_id=doc_id, content_hash="hash-1"))
    # A different hash for the same doc still inserts fine.
    with session_scope() as s:
        s.add(PsgVersion(psg_document_id=doc_id, content_hash="hash-2"))
    assert _row_count(PsgVersion) == 2


def test_insert_race_surfaces_as_constraint_and_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residual race the in-transaction hash re-check cannot see: the
    overlapping run commits AFTER our re-check ran. Simulated by pinning
    _latest_hash_in_session to the stale pre-race value while the racing row
    already exists -- the INSERT must hit the unique index and be handled as
    the existing skip path ("unchanged", INV-4: no duplicate row to re-alert),
    and the doc row's content fields must roll back with it."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    state["hash"] = "new-hash"

    # The racing run's row (it did not get to the doc-field update yet).
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        assert doc.id is not None
        s.add(
            PsgVersion(
                psg_document_id=doc.id,
                content_hash="new-hash",
                diff_summary="committed by the overlapping run",
            )
        )

    # Freeze both hash checks (resolve-time and in-transaction) at the stale
    # value so only the unique index stands between us and a duplicate row.
    monkeypatch.setattr(pipeline_mod, "_latest_hash_in_session", lambda s, d: "old-hash")

    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"
    assert _row_count(PsgVersion) == 2
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        # Our rolled-back transaction must not have flipped the doc fields.
        assert doc.content_hash == "old-hash"


def test_revert_to_prior_content_surfaces_as_error_not_silent_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FDA re-serving a PRIOR revision byte-identically (hash A -> B -> back to
    A) collides with the OLD version row, not a racing duplicate of the latest.
    Mapping that onto the race-skip path would permanently misreport the FDA
    change as "unchanged" while re-paying parse + change-summary LLM on every
    daily run; it must surface as "error" (loud in the watch ledger) until
    reverts get an owner-decided representation."""
    state: dict[str, Any] = {"hash": "hash-a", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    state["hash"] = "hash-b"
    assert pipeline_mod.ingest_listing(_listing()) == "revised"

    state["hash"] = "hash-a"  # FDA reposts the original bytes
    assert pipeline_mod.ingest_listing(_listing()) == "error"
    assert _row_count(PsgVersion) == 2  # nothing landed silently
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        # The failed transaction rolled back; the doc still describes the last
        # revision that actually ingested.
        assert doc.content_hash == "hash-b"


def test_duplicate_race_classifier_matches_both_dialect_messages() -> None:
    def err(message: str) -> IntegrityError:
        return IntegrityError("INSERT INTO psg_version ...", {}, Exception(message))

    assert pipeline_mod._is_duplicate_version_race(
        err('duplicate key value violates unique constraint "uq_psg_version_doc_hash"')
    )
    assert pipeline_mod._is_duplicate_version_race(
        err("UNIQUE constraint failed: psg_version.psg_document_id, " "psg_version.content_hash")
    )
    assert not pipeline_mod._is_duplicate_version_race(err("FOREIGN KEY constraint failed"))
    assert not pipeline_mod._is_duplicate_version_race(
        err('duplicate key value violates unique constraint "uq_alert_version_listing_product"')
    )
    # A NOT NULL violation on the indexed column must NOT read as the race:
    # only 'UNIQUE constraint failed:' on the pair is the duplicate insert.
    assert not pipeline_mod._is_duplicate_version_race(
        err("NOT NULL constraint failed: psg_version.psg_document_id")
    )


def test_unrelated_integrity_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the psg_version unique index maps to the skip path; any other
    integrity failure inside the commit transaction is a real bug and must
    re-raise instead of masquerading as "unchanged"."""
    init_db()
    doc_id = _seed_doc_with_version("hash-1")

    def unrelated(s: object, d: object) -> str:
        raise IntegrityError("INSERT ...", {}, Exception("FOREIGN KEY constraint failed"))

    # Raise from inside _commit_version_and_doc's transaction body.
    monkeypatch.setattr(pipeline_mod, "_latest_hash_in_session", unrelated)
    with pytest.raises(IntegrityError):
        pipeline_mod._commit_version_and_doc(
            listing=_listing(),
            psg_document_id=doc_id,
            content_hash="hash-2",
            pdf_path="/tmp/x.pdf",
            parsed_text_path=None,
            diff_summary=None,
        )
    assert _row_count(PsgVersion) == 1
