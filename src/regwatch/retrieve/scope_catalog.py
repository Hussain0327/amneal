"""Application-owned expansion of allowlisted conversational corpus policies."""

from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlmodel import col

from regwatch.generate.route import CorpusPolicyHint
from regwatch.retrieve.scope import CorpusDocumentRef, CorpusPolicySnapshot
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument, PsgVersion

INHALATION_PSG_MAX_DOCUMENTS = 64


class CorpusPolicyCatalogError(RuntimeError):
    """The catalog could not prove a complete current-version policy set."""


def _inhalation_psg_snapshot() -> CorpusPolicySnapshot:
    """Expand the initial policy from catalog truth, not model-authored terms.

    The policy is intentionally narrow: current PSG documents whose FDA route
    contains ``inhal``.  Every matching document must retain an application
    number and a current version.  Missing provenance fails the shadow compile
    instead of silently shrinking the allowed set.
    """

    with session_scope() as session:
        document_rows = session.execute(
            select(
                col(PsgDocument.id),
                col(PsgDocument.appl_no),
                col(PsgDocument.content_hash),
            )
            .where(func.lower(func.coalesce(col(PsgDocument.route), "")).contains("inhal"))
            .order_by(col(PsgDocument.id))
        ).all()
        document_ids = [int(row[0]) for row in document_rows if row[0] is not None]
        version_rows = (
            session.execute(
                select(
                    col(PsgVersion.psg_document_id),
                    col(PsgVersion.id),
                    col(PsgVersion.content_hash),
                )
                .where(col(PsgVersion.psg_document_id).in_(document_ids))
                .order_by(
                    col(PsgVersion.psg_document_id),
                    desc(col(PsgVersion.captured_at)),
                    desc(col(PsgVersion.id)),
                )
            ).all()
            if document_ids
            else []
        )

    current_versions: dict[int, tuple[int, str]] = {}
    for doc_id, version_id, content_hash in version_rows:
        if doc_id is None or version_id is None:
            continue
        current_versions.setdefault(int(doc_id), (int(version_id), str(content_hash or "")))

    documents: list[CorpusDocumentRef] = []
    for raw_doc_id, raw_appl_no, raw_content_hash in document_rows:
        if raw_doc_id is None:
            raise CorpusPolicyCatalogError("inhalation policy document has no id")
        doc_id = int(raw_doc_id)
        appl_no = str(raw_appl_no or "").strip()
        if not appl_no:
            raise CorpusPolicyCatalogError(
                f"inhalation policy document {doc_id} has no application number"
            )
        current = current_versions.get(doc_id)
        if current is None:
            raise CorpusPolicyCatalogError(
                f"inhalation policy document {doc_id} has no current version"
            )
        version_id, version_hash = current
        document_hash = str(raw_content_hash or "")
        if not document_hash or version_hash != document_hash:
            raise CorpusPolicyCatalogError(
                f"inhalation policy document {doc_id} current-version hash mismatch"
            )
        documents.append(
            CorpusDocumentRef(
                doc_id=doc_id,
                version_id=version_id,
                appl_no=appl_no,
                short_name=f"PSG_{appl_no}",
                is_current=True,
            )
        )

    return CorpusPolicySnapshot(
        policy=CorpusPolicyHint.INHALATION_PSG,
        documents=tuple(documents),
        max_documents=INHALATION_PSG_MAX_DOCUMENTS,
    )


def load_corpus_policy_snapshots() -> dict[CorpusPolicyHint, CorpusPolicySnapshot]:
    """Expand every application-allowlisted policy for one shadow turn."""

    return {CorpusPolicyHint.INHALATION_PSG: _inhalation_psg_snapshot()}
