"""End-to-end ingest pipeline.

Given a `PsgListing`, the pipeline:
  1. Downloads the PDF (cached on disk).
  2. Upserts the `psg_document` row keyed on the FDA application number
     (`appl_no`) — the canonical PSG identity. Idempotent.
  3. If the new content hash differs from the latest `psg_version`, creates a
     new version row, regenerates chunks (in Chroma), regenerates the
     `be_requirement` extraction. Idempotent on re-run.

All vector and DB writes commit before we touch the next PSG so a partial run
leaves the DB consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc
from sqlmodel import select

from regwatch.common.logging import get_logger
from regwatch.ingest.pdf_parser import parse_pdf
from regwatch.ingest.psg_crawler import PsgListing, download_pdf
from regwatch.process.change_detector import summarize_change
from regwatch.process.chunker import chunk_pdf
from regwatch.process.embedder import get_embedding_provider
from regwatch.process.extractor import extract_be
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.store.vector_store import add_chunks, delete_chunks_for_doc_except_version

log = get_logger(__name__)


@dataclass
class IngestStats:
    scanned: int = 0
    added: int = 0
    revised: int = 0
    unchanged: int = 0
    errors: int = 0


def _upsert_psg_document(listing: PsgListing, content_hash: str, pdf_path: str) -> tuple[int, bool]:
    """Upsert psg_document keyed on the FDA application number. Returns (id, is_new)."""
    rld_or_rs_key = ",".join(sorted(listing.rld_or_rs_numbers))
    with session_scope() as s:
        stmt = select(PsgDocument).where(PsgDocument.appl_no == listing.appl_no)
        rows = list(s.scalars(stmt))
        if rows:
            doc = rows[0]
            doc.last_seen_at = datetime.now(UTC)
            doc.active_ingredient = listing.active_ingredient
            doc.normalized_name = listing.normalized_name
            doc.dosage_form = listing.dosage_form
            doc.route = listing.route
            doc.rld_or_rs_number = rld_or_rs_key
            doc.content_hash = content_hash
            doc.recommended_date = listing.recommended_date
            doc.psg_type = listing.psg_type
            doc.source_url = listing.pdf_url
            doc.pdf_path = pdf_path
            s.add(doc)
            s.flush()
            if doc.id is None:
                raise RuntimeError("psg_document upsert did not produce an id")
            return doc.id, False
        doc = PsgDocument(
            appl_no=listing.appl_no,
            active_ingredient=listing.active_ingredient,
            normalized_name=listing.normalized_name,
            dosage_form=listing.dosage_form,
            route=listing.route,
            rld_or_rs_number=rld_or_rs_key,
            psg_type=listing.psg_type,
            recommended_date=listing.recommended_date,
            source_url=listing.pdf_url,
            pdf_path=pdf_path,
            content_hash=content_hash,
        )
        s.add(doc)
        s.flush()
        if doc.id is None:
            raise RuntimeError("psg_document insert did not produce an id")
        return doc.id, True


def _latest_version_hash(psg_document_id: int) -> str | None:
    """Return only the most recent content_hash for a doc, to avoid detached instances."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(PsgVersion.content_hash)
                .where(PsgVersion.psg_document_id == psg_document_id)
                .order_by(desc(PsgVersion.captured_at))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        return rows[0] if rows else None


def _latest_version_id(psg_document_id: int) -> int | None:
    """Return only the most recent version id for a doc, to avoid detached instances."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(PsgVersion.id)
                .where(PsgVersion.psg_document_id == psg_document_id)
                .order_by(desc(PsgVersion.captured_at))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        return rows[0] if rows else None


def _be_requirement_exists(version_id: int) -> bool:
    """True iff at least one be_requirement row is attached to this version."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(BeRequirement.id).where(BeRequirement.version_id == version_id).limit(1)
            )
        )
        return bool(rows)


def _insert_version(
    psg_document_id: int,
    content_hash: str,
    recommended_date: str | None,
    parsed_text_path: str | None,
    diff_summary: str | None,
) -> int:
    with session_scope() as s:
        v = PsgVersion(
            psg_document_id=psg_document_id,
            content_hash=content_hash,
            recommended_date=recommended_date,
            parsed_text_path=parsed_text_path,
            diff_summary=diff_summary,
        )
        s.add(v)
        s.flush()
        if v.id is None:
            raise RuntimeError("psg_version insert did not produce an id")
        return v.id


def _save_be_requirement(
    psg_document_id: int,
    version_id: int,
    fields: dict[str, object],
    citations: dict[str, object],
) -> None:
    with session_scope() as s:
        be = BeRequirement(
            psg_document_id=psg_document_id,
            version_id=version_id,
            study_type=_scalar_text(fields.get("study_type")),
            study_design=_scalar_text(fields.get("study_design")),
            strengths=_scalar_text(fields.get("strengths")),
            dissolution=_scalar_text(fields.get("dissolution")),
            waiver_conditions=_scalar_text(fields.get("waiver_conditions")),
            additional_notes=_scalar_text(fields.get("additional_notes")),
            fields_json=dict(fields),
            citations_json=dict(citations),
        )
        s.add(be)


def _scalar_text(value: object) -> str | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item not in (None, "")]
        return ", ".join(part for part in parts if part) or None
    return str(value)


def _extract_and_save_be(doc_id: int, version_id: int, pages: list[str], appl_no: str) -> None:
    """Run BE extraction for a version and persist it. Failures are logged, not raised.

    Swallowing a transient extractor outage keeps ingest durable; the missing
    row is detected and backfilled on the next run (see ingest_listing) so a
    one-time outage never permanently drops citations (INV-1).
    """
    try:
        extraction = extract_be(pages)
        _save_be_requirement(
            psg_document_id=doc_id,
            version_id=version_id,
            fields=extraction.fields,
            citations=extraction.citations,
        )
    except Exception as exc:
        log.warning(
            "be_extraction_skipped",
            appl_no=appl_no,
            error=str(exc),
        )


def _chunk_metadata_base(doc_id: int, version_id: int, listing: PsgListing) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "version_id": version_id,
        "active_ingredient": listing.active_ingredient,
        "normalized_name": listing.normalized_name,
        "dosage_form": listing.dosage_form or "",
        "route": listing.route or "",
        "recommended_date": listing.recommended_date or "",
        "source_url": listing.pdf_url,
        "psg_type": listing.psg_type,
        "appl_no": listing.appl_no,
    }


def ingest_listing(listing: PsgListing, *, extract: bool = True) -> str:
    """Ingest one PSG listing. Returns 'added' | 'revised' | 'unchanged' | 'error'.

    `extract=False` skips the per-PSG LLM BE extraction (which is the only paid
    step); chunks are still embedded locally, so the Ask/retrieval path works.
    """
    init_db()
    try:
        log.info("psg_download", appl_no=listing.appl_no, name=listing.normalized_name)
        path, pdf_bytes, content_hash = download_pdf(listing.pdf_url)
        doc_id, is_new = _upsert_psg_document(listing, content_hash, str(path))
        latest_hash = _latest_version_hash(doc_id)
        if latest_hash == content_hash:
            # Content is unchanged, but a prior run may have failed BE extraction
            # (e.g. a transient extractor outage) and left the latest version with
            # no be_requirement row. Because the hash matches forever, that gap is
            # never closed by the normal path — so backfill it here rather than
            # silently skipping (missing extraction == missing citations, INV-1).
            if extract:
                latest_version_id = _latest_version_id(doc_id)
                if latest_version_id is not None and not _be_requirement_exists(latest_version_id):
                    parsed = parse_pdf(pdf_bytes)
                    _extract_and_save_be(doc_id, latest_version_id, parsed.pages, listing.appl_no)
            return "unchanged"

        parsed = parse_pdf(pdf_bytes)

        diff_summary = summarize_change(
            previous_text=None,
            current_text=parsed.text,
            current_page_count=len(parsed.pages),
        )
        version_id = _insert_version(
            psg_document_id=doc_id,
            content_hash=content_hash,
            recommended_date=listing.recommended_date,
            parsed_text_path=None,
            diff_summary=diff_summary,
        )

        base_meta = _chunk_metadata_base(doc_id, version_id, listing)
        chunks = chunk_pdf(parsed.pages, base_metadata=base_meta)
        if chunks:
            embedder = get_embedding_provider()
            texts = [c.text for c in chunks]
            embeddings = embedder.embed(texts)
            ids = [f"{doc_id}-{version_id}-{c.ordinal}" for c in chunks]
            metas = [
                {**c.metadata, "section_path": c.metadata.get("section_path") or ""} for c in chunks
            ]
            add_chunks(ids=ids, embeddings=embeddings, documents=texts, metadatas=metas)
            log.info("chunks_added", doc_id=doc_id, version_id=version_id, n=len(chunks))
            try:
                deleted = delete_chunks_for_doc_except_version(
                    doc_id=doc_id,
                    keep_version_id=version_id,
                )
                if deleted:
                    log.info(
                        "stale_chunks_deleted",
                        doc_id=doc_id,
                        keep_version_id=version_id,
                        n=deleted,
                    )
            except Exception as exc:
                log.error(
                    "stale_chunk_cleanup_failed",
                    doc_id=doc_id,
                    keep_version_id=version_id,
                    error=str(exc),
                )

        if extract:
            _extract_and_save_be(doc_id, version_id, parsed.pages, listing.appl_no)

        return "added" if is_new else "revised"
    except Exception as exc:
        log.error(
            "ingest_failed",
            appl_no=listing.appl_no,
            error=str(exc),
        )
        return "error"


def ingest_listings(listings: list[PsgListing], *, extract: bool = True) -> IngestStats:
    """Ingest a batch of listings. Idempotent on re-run."""
    stats = IngestStats()
    for listing in listings:
        stats.scanned += 1
        outcome = ingest_listing(listing, extract=extract)
        if outcome == "added":
            stats.added += 1
        elif outcome == "revised":
            stats.revised += 1
        elif outcome == "unchanged":
            stats.unchanged += 1
        else:
            stats.errors += 1
    return stats
