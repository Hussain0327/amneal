"""End-to-end ingest pipeline.

Given a `PsgListing`, the pipeline:
  1. Downloads the PDF (cached on disk).
  2. Resolves the `psg_document` row keyed on the FDA application number
     (`appl_no`) — the canonical PSG identity. Idempotent. The row's
     content-describing fields only ever commit together with a version row
     (see `_commit_version_and_doc`), so the doc never claims content that
     was not actually ingested.
  3. If the new content hash differs from the latest `psg_version`, creates a
     new version row, regenerates chunks, regenerates the `be_requirement`
     extraction. Idempotent on re-run.

All vector and DB writes commit before we touch the next PSG so a partial run
leaves the DB consistent. A revision lands ATOMICALLY: the version row, the
doc's content fields, the version's pgvector chunk rows, and its
be_requirement row (when extraction succeeded) commit in one transaction, so
a crash mid-ingest can never leave a version without its chunks (the sole
path since R5 -- the non-transactional SQLite/Chroma dev mode is gone).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from config.settings import get_settings
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from regwatch.common.logging import get_logger
from regwatch.ingest.pdf_parser import ParsedPdf, parse_pdf
from regwatch.ingest.psg_crawler import PsgListing, download_pdf
from regwatch.process.change_detector import summarize_change
from regwatch.process.chunker import Chunk, chunk_pdf
from regwatch.process.embedder import (
    embed_documents,
    get_embedding_provider,
    get_embedding_provider_for_profile,
)
from regwatch.process.extractor import ExtractionResult, extract_be
from regwatch.store.db import init_db, session_scope
from regwatch.store.embedding_profiles import content_hash as embedding_content_hash
from regwatch.store.graph_store import derive_document_graph
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.store.vector_store import (
    add_chunks,
    chunks_exist,
    delete_chunks_for_doc,
    delete_chunks_for_doc_except_version,
    get_embedding_profile,
    upsert_profile_embeddings,
)

log = get_logger(__name__)


@dataclass
class IngestStats:
    scanned: int = 0
    added: int = 0
    revised: int = 0
    unchanged: int = 0
    errors: int = 0


@dataclass(frozen=True)
class _ProfileEmbeddingBatch:
    profile_id: str
    embeddings: list[list[float]]
    content_hashes: list[str]
    required: bool


def _legacy_document_embeddings(texts: list[str]) -> Sequence[list[float] | None]:
    """Embed the rollback space, or leave it NULL after a Qwen-only cutover."""
    if not texts:
        return []
    provider = get_embedding_provider()
    active_profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    if active_profile_id != "legacy" and getattr(provider, "name", "") == "qwen3":
        # A Qwen vector in the unversioned legacy column would silently mix
        # geometries with the historical OpenAI corpus.  The named profile is
        # the source of truth after cutover; NULL keeps that boundary honest.
        return [None] * len(texts)
    return embed_documents(provider, texts)


def _profile_document_embeddings(texts: list[str]) -> list[_ProfileEmbeddingBatch]:
    """Precompute active/shadow profile vectors before opening a DB transaction."""
    if not texts:
        return []
    settings = get_settings()
    active_profile_id = (settings.active_embedding_profile or "legacy").strip()
    shadow_profile_id = (settings.embedding_shadow_profile or "").strip()
    targets: list[tuple[str, bool]] = []
    if active_profile_id != "legacy":
        targets.append((active_profile_id, True))
    if shadow_profile_id and shadow_profile_id != active_profile_id:
        targets.append((shadow_profile_id, False))

    hashes = [embedding_content_hash(text) for text in texts]
    batches: list[_ProfileEmbeddingBatch] = []
    for profile_id, required in targets:
        try:
            profile = get_embedding_profile(profile_id)
            provider = get_embedding_provider_for_profile(profile)
            embeddings = embed_documents(provider, texts)
        except Exception as exc:
            if required:
                raise
            # Shadow traffic must never take down the FDA ingest path.  The
            # durable pending-chunk query makes the missed batch resumable.
            log.warning(
                "shadow_embedding_skipped",
                profile_id=profile_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        batches.append(
            _ProfileEmbeddingBatch(
                profile_id=profile_id,
                embeddings=embeddings,
                content_hashes=list(hashes),
                required=required,
            )
        )
    return batches


def _write_profile_batches(
    session: Session,
    chunk_ids: list[str],
    batches: list[_ProfileEmbeddingBatch],
) -> None:
    """Persist required profiles atomically; isolate best-effort shadow writes."""
    for batch in batches:
        if batch.required:
            upsert_profile_embeddings(
                batch.profile_id,
                chunk_ids,
                batch.embeddings,
                batch.content_hashes,
                conn=session.connection(),
            )
            continue
        try:
            with session.begin_nested():
                upsert_profile_embeddings(
                    batch.profile_id,
                    chunk_ids,
                    batch.embeddings,
                    batch.content_hashes,
                    conn=session.connection(),
                )
        except Exception as exc:
            log.warning(
                "shadow_embedding_write_skipped",
                profile_id=batch.profile_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )


def _apply_content_fields(
    doc: PsgDocument, listing: PsgListing, content_hash: str, pdf_path: str
) -> None:
    """Set the doc-row fields that describe INGESTED CONTENT (which revision the
    document is). Callers must only apply these when the matching version row
    is (or is being) committed, so the doc never advertises a revision (e.g.
    a draft->final flip) whose content was never ingested."""
    doc.content_hash = content_hash
    doc.psg_type = listing.psg_type
    doc.recommended_date = listing.recommended_date
    doc.source_url = listing.pdf_url
    doc.pdf_path = pdf_path


def _latest_hash_in_session(s: Session, psg_document_id: int) -> str | None:
    """Most recent version content_hash for a doc, read inside the caller's session."""
    rows = list(
        s.scalars(
            select(PsgVersion.content_hash)
            .where(PsgVersion.psg_document_id == psg_document_id)
            .order_by(desc(PsgVersion.captured_at))  # type: ignore[arg-type]
            .limit(1)
        )
    )
    return rows[0] if rows else None


def _latest_committed_hash(psg_document_id: int) -> str | None:
    """Latest version hash via a FRESH session, for classifying a unique-index
    collision: by the time the insert has failed, our transaction (including
    its `_latest_hash_in_session` pre-check) has rolled back and is stale by
    definition, while the colliding winner's commit is visible to a new
    session. Id breaks captured_at ties the same way migration 0014 picks its
    dedupe keeper."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(PsgVersion.content_hash)
                .where(PsgVersion.psg_document_id == psg_document_id)
                .order_by(col(PsgVersion.captured_at).desc(), col(PsgVersion.id).desc())
                .limit(1)
            )
        )
        return rows[0] if rows else None


def _resolve_psg_document(
    listing: PsgListing, content_hash: str, pdf_path: str
) -> tuple[int | None, str | None]:
    """Look up the doc row for a listing. Returns (doc_id or None, latest version hash).

    Refreshes the listing-identity fields and last_seen_at on every sighting,
    but content-describing fields are refreshed here ONLY when the latest
    ingested version already matches this download (an unchanged sighting, so
    the fields describe real content). For a new revision they must wait for
    _commit_version_and_doc: parse/summarize can still fail after this point,
    and a doc row updated without its version would serve the NEW revision's
    metadata (psg_type/recommended_date/content_hash) next to the OLD
    version's content on every read of sources/psg.py."""
    with session_scope() as s:
        stmt = select(PsgDocument).where(PsgDocument.appl_no == listing.appl_no)
        rows = list(s.scalars(stmt))
        if not rows:
            return None, None
        doc = rows[0]
        if doc.id is None:
            raise RuntimeError("psg_document row has no id")
        latest_hash = _latest_hash_in_session(s, doc.id)
        doc.last_seen_at = datetime.now(UTC)
        doc.active_ingredient = listing.active_ingredient
        doc.normalized_name = listing.normalized_name
        doc.dosage_form = listing.dosage_form
        doc.route = listing.route
        doc.rld_or_rs_number = ",".join(sorted(listing.rld_or_rs_numbers))
        if latest_hash == content_hash:
            _apply_content_fields(doc, listing, content_hash, pdf_path)
        s.add(doc)
        return doc.id, latest_hash


def _create_psg_document(listing: PsgListing, content_hash: str, pdf_path: str) -> int:
    """Create a brand-new doc row. Content columns are NOT NULL so they are set
    at insert; there is no prior version whose served content they could
    misdescribe, and downstream steps key parsed text/chunks on the new id."""
    with session_scope() as s:
        doc = PsgDocument(
            appl_no=listing.appl_no,
            active_ingredient=listing.active_ingredient,
            normalized_name=listing.normalized_name,
            dosage_form=listing.dosage_form,
            route=listing.route,
            rld_or_rs_number=",".join(sorted(listing.rld_or_rs_numbers)),
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
        return doc.id


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


def _latest_version_text_path(psg_document_id: int) -> str | None:
    """The most recent version's persisted parsed-text path (None if absent)."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(PsgVersion.parsed_text_path)
                .where(PsgVersion.psg_document_id == psg_document_id)
                .order_by(desc(PsgVersion.captured_at))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        return rows[0] if rows else None


def _write_parsed_text(doc_id: int, content_hash: str, text: str) -> str:
    """Persist a version's parsed text so the NEXT revision can produce a real
    cited diff. Lives under data_dir (isolated per-test via DATA_DIR)."""
    out_dir = get_settings().data_dir / "parsed_text"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{doc_id}_{content_hash}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _read_parsed_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _be_requirement_exists(version_id: int) -> bool:
    """True iff at least one be_requirement row is attached to this version."""
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(BeRequirement.id).where(BeRequirement.version_id == version_id).limit(1)
            )
        )
        return bool(rows)


def _is_duplicate_version_race(exc: IntegrityError) -> bool:
    """True iff an IntegrityError is the psg_version unique index firing.

    Postgres names the index ('... violates unique constraint
    "uq_psg_version_doc_hash"'); other backends may name the column pair ('UNIQUE
    constraint failed: psg_version.psg_document_id, ...') -- the 'UNIQUE
    constraint failed:' prefix is part of the needle so a (hypothetical) NOT
    NULL violation on the same column cannot be mistaken for the race. Any
    other integrity failure in the commit transaction is a real bug and must
    propagate.
    """
    message = str(getattr(exc, "orig", None) or exc)
    return (
        "uq_psg_version_doc_hash" in message
        or "UNIQUE constraint failed: psg_version.psg_document_id" in message
    )


def _commit_version_and_doc(
    *,
    listing: PsgListing,
    psg_document_id: int,
    content_hash: str,
    pdf_path: str,
    parsed_text_path: str | None,
    diff_summary: str | None,
    chunk_payload: (
        tuple[
            list[Chunk],
            Sequence[list[float] | None],
            list[_ProfileEmbeddingBatch],
        ]
        | None
    ) = None,
    extraction: ExtractionResult | None = None,
) -> int | None:
    """Insert the new version AND the doc row's content fields in ONE transaction.

    Postgres mode additionally threads the version's chunk rows and (when
    extraction succeeded) its be_requirement row through the SAME transaction:
    `chunk_payload` carries legacy and named-profile vectors embedded BEFORE
    this call (no network call may run while the transaction is open -- the
    2026-06-18 idle-in-transaction incident class), and every vector upsert runs
    on this session's connection.
    Either everything for the revision lands or nothing does, so a crash can
    never leave a version row without its chunks. Passing neither payload is
    the legacy non-atomic calling convention: the caller
    keeps the historical commit-then-index order there.

    Returns the new version id, or None when this exact revision already
    landed via an overlapping run (e.g. the watch-daily cron plus a manual
    `ingest-all` against the same DB). Skipping the duplicate keeps a single
    version row per revision, so pairs_without_alert cannot re-alert the same
    FDA change on the next run (INV-4). Two layers catch it:
    - The in-transaction latest-hash re-check (cheap, catches an overlap that
      committed during the caller's parse+LLM stretch).
    - The unique (psg_document_id, content_hash) index (migration 0014), which
      closes the residual insert race for good: the loser's INSERT raises and
      is mapped onto the same skip path here -- but ONLY when the colliding
      row is the doc's latest version (the winner just committed it). A
      collision with an OLDER version means FDA re-served prior content
      (hash A -> B -> back to A), which one-row-per-(doc, hash) cannot record
      as a new version; that re-raises so the run surfaces "error" instead of
      silently absorbing an FDA change (see the except branch).

    The doc row still never describes content that has no version row: a parse
    or LLM failure aborts before this function, and a rolled-back transaction
    reverts the content fields together with the version insert.
    """
    try:
        with session_scope() as s:
            if _latest_hash_in_session(s, psg_document_id) == content_hash:
                return None
            v = PsgVersion(
                psg_document_id=psg_document_id,
                content_hash=content_hash,
                recommended_date=listing.recommended_date,
                parsed_text_path=parsed_text_path,
                diff_summary=diff_summary,
            )
            s.add(v)
            doc = s.get(PsgDocument, psg_document_id)
            if doc is None:
                raise RuntimeError("psg_document row vanished during ingest")
            _apply_content_fields(doc, listing, content_hash, pdf_path)
            s.add(doc)
            s.flush()
            if v.id is None:
                raise RuntimeError("psg_version insert did not produce an id")
            if chunk_payload is not None:
                chunks, embeddings, profile_batches = chunk_payload
                if chunks:
                    ids, metas, texts = _index_rows(psg_document_id, v.id, chunks)
                    add_chunks(
                        ids=ids,
                        embeddings=embeddings,
                        documents=texts,
                        metadatas=metas,
                        conn=s.connection(),
                    )
                    _write_profile_batches(s, ids, profile_batches)
                    derive_document_graph(
                        doc_id=psg_document_id,
                        version_id=v.id,
                        chunk_ids=ids,
                        chunk_metas=metas,
                        doc_attrs=_graph_doc_attrs(listing),
                        conn=s.connection(),
                    )
                    log.info("chunks_added", doc_id=psg_document_id, version_id=v.id, n=len(ids))
            if extraction is not None:
                s.add(
                    _be_requirement_row(
                        psg_document_id=psg_document_id,
                        version_id=v.id,
                        fields=extraction.fields,
                        citations=extraction.citations,
                    )
                )
            return v.id
    except IntegrityError as exc:
        if not _is_duplicate_version_race(exc):
            raise
        latest_committed = _latest_committed_hash(psg_document_id)
        if latest_committed == content_hash:
            # The overlapping run won the insert race after our in-transaction
            # hash check. The WHOLE transaction (version + doc fields + chunks
            # + BE row) rolled back, so the winner alone owns this revision
            # (INV-4).
            log.info(
                "duplicate_version_race_skipped",
                appl_no=listing.appl_no,
                doc_id=psg_document_id,
                content_hash=content_hash,
            )
            return None
        # The colliding row is an OLDER version: FDA re-served prior content
        # (hash A -> B -> back to A), which one-row-per-(doc, hash) cannot
        # record as a new version. Treating it as the race skip would silently
        # drop an FDA change AND re-pay parse/diff (plus PG-mode embeddings and
        # BE extraction) on every later run, so surface it as an ingest error
        # -- visible in the watch ledger/digest -- until reverts get an owner-
        # decided representation (e.g. touch/bump semantics).
        log.error(
            "version_revert_unrepresentable",
            appl_no=listing.appl_no,
            doc_id=psg_document_id,
            content_hash=content_hash,
            latest_hash=latest_committed,
        )
        raise


def _be_requirement_row(
    psg_document_id: int,
    version_id: int,
    fields: dict[str, object],
    citations: dict[str, object],
) -> BeRequirement:
    """The BeRequirement row for one extraction; callers choose the session."""
    return BeRequirement(
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


def _save_be_requirement(
    psg_document_id: int,
    version_id: int,
    fields: dict[str, object],
    citations: dict[str, object],
) -> None:
    with session_scope() as s:
        s.add(_be_requirement_row(psg_document_id, version_id, fields, citations))


def _scalar_text(value: object) -> str | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item not in (None, "")]
        return ", ".join(part for part in parts if part) or None
    return str(value)


def _extract_be_for_commit(pages: list[str], appl_no: str) -> ExtractionResult | None:
    """Postgres path: run the (paid, slow) BE LLM extraction BEFORE the commit
    transaction opens, so the transaction never sits idle across a network
    call. Failures are logged and yield None -- the version and its chunks
    still land, and the unchanged-path backfill retries the extraction on the
    next run so a one-time outage never permanently drops citations (INV-1),
    mirroring _extract_and_save_be's swallow on the dev path.
    """
    try:
        return extract_be(pages)
    except Exception as exc:
        # Same event/fields as _extract_and_save_be so triage stays one query.
        log.warning(
            "be_extraction_skipped",
            appl_no=appl_no,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


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
        # error_type mirrors the sibling ingest_failed log: this except swallows
        # both LLM and DB-write failures, and the unchanged-content backfill
        # re-pays the LLM cost every run until the row exists -- so triaging
        # "bad LLM JSON" vs "broken DB write" from logs matters.
        log.warning(
            "be_extraction_skipped",
            appl_no=appl_no,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def _graph_doc_attrs(listing: PsgListing) -> dict[str, object]:
    """The doc-identity attrs the tier-1 graph derivation stamps on nodes."""
    return {
        "appl_no": listing.appl_no,
        "normalized_name": listing.normalized_name,
        "dosage_form": listing.dosage_form or "",
        "route": listing.route or "",
        "psg_type": listing.psg_type,
    }


def _chunk_metadata_base(doc_id: int, listing: PsgListing) -> dict[str, object]:
    # version_id is intentionally absent: _index_rows stamps it, so the
    # Postgres path can chunk+embed BEFORE the version row (and its id) exists.
    return {
        "doc_id": doc_id,
        "active_ingredient": listing.active_ingredient,
        "normalized_name": listing.normalized_name,
        "dosage_form": listing.dosage_form or "",
        "route": listing.route or "",
        "recommended_date": listing.recommended_date or "",
        "source_url": listing.pdf_url,
        "psg_type": listing.psg_type,
        "appl_no": listing.appl_no,
    }


def _index_rows(
    doc_id: int, version_id: int, chunks: list[Chunk]
) -> tuple[list[str], list[dict[str, object]], list[str]]:
    """Vector-index (ids, metadatas, texts) for one version's chunks."""
    ids = [f"{doc_id}-{version_id}-{c.ordinal}" for c in chunks]
    metas: list[dict[str, object]] = [
        {
            **c.metadata,
            "version_id": version_id,
            "section_path": c.metadata.get("section_path") or "",
        }
        for c in chunks
    ]
    texts = [c.text for c in chunks]
    return ids, metas, texts


def _cleanup_stale_chunks(doc_id: int, version_id: int) -> None:
    """Drop superseded chunks for a doc. Failures are logged, not raised: the
    stale rows are re-collected on the next revision, so cleanup must never
    take down an ingest whose version+chunks already landed."""
    try:
        deleted = delete_chunks_for_doc_except_version(doc_id=doc_id, keep_version_id=version_id)
        if deleted:
            log.info("stale_chunks_deleted", doc_id=doc_id, keep_version_id=version_id, n=deleted)
    except Exception as exc:
        log.error(
            "stale_chunk_cleanup_failed",
            doc_id=doc_id,
            keep_version_id=version_id,
            error=str(exc),
        )


def _regenerate_chunks(
    doc_id: int, version_id: int, parsed: ParsedPdf, listing: PsgListing
) -> None:
    """Embed and store this version's chunks, then drop any superseded ones.

    Shared by the dev-mode revised/added path and (both modes) the
    unchanged-but-chunkless backfill, so both produce an identical,
    current-only index for the document. The Postgres revision path instead
    threads the chunk upsert through the version-commit transaction (see
    _commit_version_and_doc).
    """
    chunks = chunk_pdf(parsed.pages, base_metadata=_chunk_metadata_base(doc_id, listing))
    if not chunks:
        return
    ids, metas, texts = _index_rows(doc_id, version_id, chunks)
    embeddings = _legacy_document_embeddings(texts)
    profile_batches = _profile_document_embeddings(texts)
    with session_scope() as s:
        add_chunks(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metas,
            conn=s.connection(),
        )
        if profile_batches:
            _write_profile_batches(s, ids, profile_batches)
        derive_document_graph(
            doc_id=doc_id,
            version_id=version_id,
            chunk_ids=ids,
            chunk_metas=metas,
            doc_attrs=_graph_doc_attrs(listing),
            conn=s.connection(),
        )
    log.info("chunks_added", doc_id=doc_id, version_id=version_id, n=len(chunks))
    _cleanup_stale_chunks(doc_id, version_id)


def _listing_from_document(doc: PsgDocument) -> PsgListing:
    """Rebuild the crawler listing shape from a stored psg_document row, for
    re-chunking without re-crawling the FDA index. Every field the chunk
    metadata consumes (_chunk_metadata_base) is persisted on the row;
    doc.source_url IS the PDF url (_create_psg_document stores listing.pdf_url
    there)."""
    return PsgListing(
        appl_no=doc.appl_no or "",
        active_ingredient=doc.active_ingredient,
        normalized_name=doc.normalized_name,
        stripped_name=doc.normalized_name,
        psg_type=doc.psg_type,
        route=doc.route,
        dosage_form=doc.dosage_form,
        rld_or_rs_numbers=[n for n in (doc.rld_or_rs_number or "").split(",") if n],
        recommended_date=doc.recommended_date,
        pdf_url=doc.source_url,
        source_url=doc.source_url,
    )


def rechunk_document(doc_id: int) -> str:
    """Re-chunk one PSG document's CURRENT version from its cached PDF.

    Recomputes chunks under the current chunking recipe and replaces the
    document's rows atomically: delete + insert in ONE transaction, because a
    recipe that emits fewer chunks than its predecessor would otherwise leave
    the old recipe's high-ordinal rows behind as stale retrieval hits (the
    id-keyed upsert alone cannot remove them). Embeddings are recomputed
    through the same legacy + profile dual-write path as ingest. The
    psg_version row and stored parsed text are untouched: this changes the
    retrieval view, never the audit record.

    Returns 'rechunked' | 'missing' | 'no-version' | 'no-pdf' | 'empty'.
    """
    init_db()
    with session_scope() as s:
        doc = s.get(PsgDocument, doc_id)
        if doc is None:
            return "missing"
        # Read everything needed WHILE the row is session-bound: session_scope
        # expires attributes on exit, and a detached-instance refresh raises.
        listing = _listing_from_document(doc)
        pdf_path = doc.pdf_path
    version_id = _latest_version_id(doc_id)
    if version_id is None:
        return "no-version"
    pdf_bytes: bytes | None = None
    if pdf_path and Path(pdf_path).is_file():
        pdf_bytes = Path(pdf_path).read_bytes()
    if pdf_bytes is None:
        # Cache miss (fresh checkout / moved data dir): the download path is
        # polite (on-disk cache + backoff) and the listing's source_url is the
        # PDF url.
        try:
            _, pdf_bytes, _ = download_pdf(listing.pdf_url)
        except Exception as exc:
            log.error("rechunk_pdf_unavailable", doc_id=doc_id, error=str(exc))
            return "no-pdf"
    parsed = parse_pdf(pdf_bytes)
    chunks = chunk_pdf(parsed.pages, base_metadata=_chunk_metadata_base(doc_id, listing))
    if not chunks:
        log.warning("rechunk_empty", doc_id=doc_id)
        return "empty"
    ids, metas, texts = _index_rows(doc_id, version_id, chunks)
    embeddings = _legacy_document_embeddings(texts)
    profile_batches = _profile_document_embeddings(texts)
    with session_scope() as s:
        conn = s.connection()
        delete_chunks_for_doc(doc_id, conn=conn)
        add_chunks(ids=ids, embeddings=embeddings, documents=texts, metadatas=metas, conn=conn)
        if profile_batches:
            _write_profile_batches(s, ids, profile_batches)
        derive_document_graph(
            doc_id=doc_id,
            version_id=version_id,
            chunk_ids=ids,
            chunk_metas=metas,
            doc_attrs=_graph_doc_attrs(listing),
            conn=conn,
        )
    log.info("rechunked", doc_id=doc_id, version_id=version_id, n=len(chunks))
    return "rechunked"


def ingest_listing(listing: PsgListing, *, extract: bool = True) -> str:
    """Ingest one PSG listing. Returns 'added' | 'revised' | 'unchanged' | 'error'.

    `extract=False` skips the per-PSG LLM BE extraction (which is the only paid
    step); chunks are still embedded locally, so the Ask/retrieval path works.
    """
    init_db()
    try:
        log.info("psg_download", appl_no=listing.appl_no, name=listing.normalized_name)
        path, pdf_bytes, content_hash = download_pdf(listing.pdf_url)
        doc_id, latest_hash = _resolve_psg_document(listing, content_hash, str(path))
        if doc_id is None:
            doc_id = _create_psg_document(listing, content_hash, str(path))
        elif latest_hash == content_hash:
            # Content is unchanged, but this version may still have gaps: a BE
            # row missing because extraction failed at ingest time (both
            # modes), or chunks missing from a dev-mode crash between the
            # version commit and the Chroma write / from prod data that
            # predates the atomic Postgres commit. Because the hash matches
            # forever, the normal path never revisits it -- so backfill any
            # MISSING chunks (else this version is a permanent retrieval blind
            # spot) or be_requirement (missing extraction == missing
            # citations, INV-1) here. Parse the PDF at most once, only when a
            # gap exists.
            latest_version_id = _latest_version_id(doc_id)
            if latest_version_id is not None:
                need_chunks = not chunks_exist(doc_id, latest_version_id)
                need_be = extract and not _be_requirement_exists(latest_version_id)
                if need_chunks or need_be:
                    parsed = parse_pdf(pdf_bytes)
                    if need_chunks:
                        _regenerate_chunks(doc_id, latest_version_id, parsed, listing)
                        log.info(
                            "chunkless_version_backfilled",
                            doc_id=doc_id,
                            version_id=latest_version_id,
                        )
                    if need_be:
                        _extract_and_save_be(
                            doc_id, latest_version_id, parsed.pages, listing.appl_no
                        )
            return "unchanged"

        # A real revision (a prior version exists) gets a real cited diff against
        # the prior version's persisted text; the genuine first version (no prior)
        # keeps the "Initial version ingested" marker. A prior version with no
        # persisted text (created before this was wired) degrades to the marker.
        prior_text_path = _latest_version_text_path(doc_id) if latest_hash is not None else None
        parsed = parse_pdf(pdf_bytes)

        diff_summary = summarize_change(
            previous_text=_read_parsed_text(prior_text_path),
            current_text=parsed.text,
            current_page_count=len(parsed.pages),
        )

        # Everything network-bound (chunking is local, but embedding and BE
        # extraction are API calls) runs BEFORE the commit transaction, then
        # version + doc fields + chunks + BE row land atomically -- the sole
        # ingest path since R5 removed the non-transactional SQLite/Chroma
        # dev mode.
        chunks = chunk_pdf(parsed.pages, base_metadata=_chunk_metadata_base(doc_id, listing))
        texts = [c.text for c in chunks]
        embeddings = _legacy_document_embeddings(texts)
        profile_batches = _profile_document_embeddings(texts)
        chunk_payload = (chunks, embeddings, profile_batches)
        extraction: ExtractionResult | None = None
        if extract:
            extraction = _extract_be_for_commit(parsed.pages, listing.appl_no)

        version_id = _commit_version_and_doc(
            listing=listing,
            psg_document_id=doc_id,
            content_hash=content_hash,
            pdf_path=str(path),
            parsed_text_path=_write_parsed_text(doc_id, content_hash, parsed.text),
            diff_summary=diff_summary,
            chunk_payload=chunk_payload,
            extraction=extraction,
        )
        if version_id is None:
            # An overlapping run already committed this exact revision (and owns
            # its chunks/BE backfill); reporting it changed again here would
            # double-count one FDA change (INV-4).
            return "unchanged"

        # Post-commit on purpose: cleanup failure must not roll back a
        # revision that fully landed (see _cleanup_stale_chunks).
        _cleanup_stale_chunks(doc_id, version_id)

        # Classify by version history, not doc-row novelty: a doc row can
        # persist from an earlier run whose parse failed AFTER the row was
        # created, and that PSG has still never been ingested; its first
        # successful version must be reported "added", not "revised".
        return "added" if latest_hash is None else "revised"
    except Exception as exc:
        # error_type lets a watch run be triaged by which guard fired
        # (PdfTooLargeError / PdfInvalidError / PdfParseTimeoutError / ...).
        log.error(
            "ingest_failed",
            appl_no=listing.appl_no,
            error_type=type(exc).__name__,
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
