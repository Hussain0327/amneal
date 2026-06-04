"""End-to-end ingest pipeline.

Given a `PsgListing`, the pipeline:
  1. Downloads the PDF (cached on disk).
  2. Upserts the `psg_document` row keyed on `(normalized_name, dosage_form,
     route, appl_no)`. Idempotent.
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
from regwatch.store.vector_store import add_chunks

log = get_logger(__name__)


@dataclass
class IngestStats:
    scanned: int = 0
    added: int = 0
    revised: int = 0
    unchanged: int = 0
    errors: int = 0


def _upsert_psg_document(listing: PsgListing, content_hash: str, pdf_path: str) -> tuple[int, bool]:
    """Upsert psg_document. Returns (id, is_new)."""
    rld_or_rs_key = ",".join(sorted(listing.rld_or_rs_numbers))
    with session_scope() as s:
        stmt = (
            select(PsgDocument)
            .where(PsgDocument.normalized_name == listing.normalized_name)
            .where(PsgDocument.dosage_form == listing.dosage_form)
            .where(PsgDocument.route == listing.route)
            .where(PsgDocument.rld_or_rs_number == rld_or_rs_key)
        )
        rows = list(s.scalars(stmt))
        if rows:
            doc = rows[0]
            doc.last_seen_at = datetime.now(UTC)
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
            study_type=fields.get("study_type"),  # type: ignore[arg-type]
            study_design=fields.get("study_design"),  # type: ignore[arg-type]
            strengths=fields.get("strengths"),  # type: ignore[arg-type]
            dissolution=fields.get("dissolution"),  # type: ignore[arg-type]
            waiver_conditions=fields.get("waiver_conditions"),  # type: ignore[arg-type]
            additional_notes=fields.get("additional_notes"),  # type: ignore[arg-type]
            fields_json=dict(fields),
            citations_json=dict(citations),
        )
        s.add(be)


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

        if extract:
            try:
                extraction = extract_be(parsed.pages)
                _save_be_requirement(
                    psg_document_id=doc_id,
                    version_id=version_id,
                    fields=extraction.fields,
                    citations=extraction.citations,
                )
            except Exception as exc:
                log.warning(
                    "be_extraction_skipped",
                    appl_no=listing.appl_no,
                    error=str(exc),
                )

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
