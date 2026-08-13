"""Resumable, per-document atomic synchronization of the FDA corpus."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from config.settings import get_settings
from selectolax.parser import HTMLParser
from sqlalchemy import func
from sqlalchemy import text as sa_text
from sqlmodel import Session, col, select

from regwatch.common.logging import get_logger
from regwatch.corpus.manifest import CorpusArtifact, CorpusManifest
from regwatch.ingest.embedding_writer import (
    ProfileEmbeddingBatch,
    legacy_document_embeddings,
    profile_document_embeddings,
    write_profile_batches,
)
from regwatch.ingest.pdf_parser import ParsedPdf, parse_pdf
from regwatch.process.chunker import CHUNKING_VERSION, Chunk, chunk_document_pages, chunk_pdf
from regwatch.sources.http import get_authoritative_bytes, owned_fda_client
from regwatch.sources.policy import FdaSourceFamily
from regwatch.store.db import session_scope
from regwatch.store.models import FdaCorpusRun, FdaDocument, FdaDocumentVersion
from regwatch.store.vector_store import add_chunks, delete_chunks_for_fda_document

log = get_logger(__name__)

_PROCESSING_SPEC = {
    "schema_version": 1,
    "chunking_version": CHUNKING_VERSION,
    "pdf_parser": "pdfplumber-pypdf-page-preserving-v1",
    "inline_parser": "utf8-single-page-v1",
    "html_parser": "selectolax-main-content-v1",
}
PROCESSING_FINGERPRINT = hashlib.sha256(
    json.dumps(_PROCESSING_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class ArtifactPayload:
    source_url: str
    content: bytes
    content_hash: str
    mime_type: str
    fetched_at: datetime
    response_metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class CorpusSyncStats:
    run_id: str
    expected_documents: int
    discovered_documents: int
    added_documents: int = 0
    revised_documents: int = 0
    unchanged_documents: int = 0
    error_documents: int = 0
    retired_documents: int = 0
    chunks_written: int = 0
    embeddings_deferred: bool = False
    workers: int = 1
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.error_documents == 0


def sync_manifest(
    manifest: CorpusManifest,
    *,
    limit: int = 0,
    defer_embeddings: bool = False,
    workers: int = 1,
    client: httpx.Client | None = None,
) -> CorpusSyncStats:
    """Synchronize a manifest, committing one complete document at a time."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    artifacts = list(manifest.artifacts[:limit] if limit else manifest.artifacts)
    run_id = str(uuid.uuid4())
    stats = CorpusSyncStats(
        run_id=run_id,
        expected_documents=len(artifacts),
        discovered_documents=len(manifest.artifacts),
        embeddings_deferred=defer_embeddings,
        workers=workers,
    )
    _create_run(stats, manifest)
    try:
        with owned_fda_client(client) as active_client:
            for artifact, outcome in _sync_artifacts(
                artifacts,
                client=active_client,
                defer_embeddings=defer_embeddings,
                workers=workers,
            ):
                try:
                    status, chunks = outcome.result()
                    setattr(stats, f"{status}_documents", getattr(stats, f"{status}_documents") + 1)
                    stats.chunks_written += chunks
                except Exception as exc:
                    stats.error_documents += 1
                    error = {
                        "canonical_id": artifact.canonical_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1_000],
                    }
                    stats.errors.append(error)
                    log.exception("authoritative_corpus_document_failed", **error)
                _checkpoint_run(stats)
        if stats.succeeded and manifest.complete_universe and not limit:
            stats.retired_documents = _reconcile_active_manifest(manifest)
    except BaseException:
        _finish_run(stats, status="failed")
        raise
    _finish_run(stats, status="succeeded" if stats.succeeded else "failed")
    return stats


def _sync_artifacts(
    artifacts: list[CorpusArtifact],
    *,
    client: httpx.Client,
    defer_embeddings: bool,
    workers: int,
) -> Iterator[tuple[CorpusArtifact, Future[tuple[str, int]]]]:
    """Yield a bounded, manifest-ordered stream of completed worker futures.

    The queue is capped at four items per worker, so a 140k-document manifest
    never becomes 140k in-memory futures. Results merge in manifest order for
    reproducible run ledgers even when network completion order varies.
    """

    def iterator() -> Iterator[tuple[CorpusArtifact, Future[tuple[str, int]]]]:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fda-corpus")
        source = iter(artifacts)
        queue: deque[tuple[CorpusArtifact, Future[tuple[str, int]]]] = deque()

        def run_one(artifact: CorpusArtifact) -> tuple[str, int]:
            started = time.monotonic()
            try:
                status, chunks = sync_artifact(
                    artifact,
                    client=client,
                    defer_embeddings=defer_embeddings,
                )
            except Exception as exc:
                log.warning(
                    "authoritative_corpus_document_completed",
                    canonical_id=artifact.canonical_id,
                    source_family=artifact.source_family.value,
                    status="error",
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started) * 1_000, 1),
                )
                raise
            log.info(
                "authoritative_corpus_document_completed",
                canonical_id=artifact.canonical_id,
                source_family=artifact.source_family.value,
                status=status,
                chunks=chunks,
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
            )
            return status, chunks

        def submit_next() -> bool:
            try:
                artifact = next(source)
            except StopIteration:
                return False
            future = pool.submit(run_one, artifact)
            queue.append((artifact, future))
            return True

        try:
            for _ in range(min(len(artifacts), workers * 4)):
                submit_next()
            while queue:
                artifact, future = queue.popleft()
                yield artifact, future
                submit_next()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return iterator()


def sync_artifact(
    artifact: CorpusArtifact,
    *,
    client: httpx.Client,
    defer_embeddings: bool = False,
) -> tuple[str, int]:
    """Fetch and atomically publish one artifact; return (status, chunks written)."""
    payload = fetch_artifact(artifact, client=client)
    ready = _ready_version(
        artifact.canonical_id,
        payload.content_hash,
        require_embeddings=not defer_embeddings,
    )
    if ready is not None:
        _touch_document(artifact, payload.source_url)
        return "unchanged", 0

    parsed = parse_artifact(payload)
    chunks = _chunk_artifact(artifact, parsed, payload.source_url)
    if not chunks:
        raise RuntimeError(f"artifact produced no citable text chunks: {artifact.canonical_id}")
    texts = [chunk.text for chunk in chunks]
    legacy_embeddings: Sequence[list[float] | None]
    profile_batches: list[ProfileEmbeddingBatch]
    if defer_embeddings:
        legacy_embeddings = [None] * len(texts)
        profile_batches = []
    else:
        legacy_embeddings = legacy_document_embeddings(texts)
        profile_batches = profile_document_embeddings(texts)
    artifact_path = _persist_artifact(artifact, payload)

    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        doc = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).first()
        was_added = doc is None
        if doc is None:
            doc = FdaDocument(
                canonical_id=artifact.canonical_id,
                source_family=artifact.source_family.value,
                document_type=artifact.document_type.value,
                title=artifact.title,
                source_url=payload.source_url,
            )
            session.add(doc)
            session.flush()
        if doc.id is None:
            raise RuntimeError("fda_document insert did not produce an id")

        version = session.exec(
            select(FdaDocumentVersion).where(
                FdaDocumentVersion.fda_document_id == doc.id,
                FdaDocumentVersion.content_hash == payload.content_hash,
                FdaDocumentVersion.processing_fingerprint == PROCESSING_FINGERPRINT,
            )
        ).first()
        if version is not None and _version_ready_in_session(session, version):
            _apply_document_fields(doc, artifact, payload.source_url)
            session.add(doc)
            return "unchanged", 0

        prior_versions = int(
            session.exec(
                select(func.count()).where(FdaDocumentVersion.fda_document_id == doc.id)
            ).one()
        )
        if version is None:
            version = FdaDocumentVersion(
                fda_document_id=doc.id,
                content_hash=payload.content_hash,
                processing_fingerprint=PROCESSING_FINGERPRINT,
                source_updated_at=artifact.source_updated_at,
                fetched_at=payload.fetched_at,
                mime_type=payload.mime_type,
                byte_size=len(payload.content),
                page_count=len(parsed.pages),
                chunk_count=len(chunks),
                artifact_path=str(artifact_path),
                parse_engine=parsed.engine,
                metadata_json={
                    **payload.response_metadata,
                    "processing_spec": _PROCESSING_SPEC,
                },
            )
            session.add(version)
            session.flush()
        elif version.chunk_count != len(chunks):
            raise RuntimeError(
                "reconstructed chunk count does not match immutable version metadata "
                f"({len(chunks)} != {version.chunk_count})"
            )
        if version.id is None:
            raise RuntimeError("fda_document_version insert did not produce an id")

        _apply_document_fields(doc, artifact, payload.source_url)
        session.add(doc)
        delete_chunks_for_fda_document(doc.id, conn=session.connection())
        ids, metadata = _index_rows(doc.id, version.id, artifact, chunks, payload.source_url)
        add_chunks(
            ids=ids,
            embeddings=legacy_embeddings,
            documents=texts,
            metadatas=metadata,
            conn=session.connection(),
        )
        write_profile_batches(session, ids, profile_batches)
        status = "added" if was_added or prior_versions == 0 else "revised"
        return status, len(chunks)


def fetch_artifact(artifact: CorpusArtifact, *, client: httpx.Client) -> ArtifactPayload:
    fetched_at = datetime.now(UTC)
    if artifact.inline_text is not None:
        content = artifact.inline_text.encode("utf-8")
        return ArtifactPayload(
            source_url=artifact.source_url,
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            mime_type="text/plain; charset=utf-8",
            fetched_at=fetched_at,
            response_metadata={"representation": "deterministic_fda_snapshot_record"},
        )

    settings = get_settings()
    final_url, content, headers = get_authoritative_bytes(
        client,
        artifact.source_url,
        artifact.source_family,
        max_bytes=settings.fda_corpus_pdf_max_bytes,
        min_interval_s=settings.crawl_min_interval_ms / 1000.0,
    )
    content_type = headers.get("content-type", "").lower()
    leading = content.lstrip()[:512].lower()
    if content.startswith(b"%PDF-"):
        mime_type = "application/pdf"
    elif "text/html" in content_type or leading.startswith((b"<!doctype html", b"<html")):
        mime_type = "text/html; charset=utf-8"
    else:
        raise RuntimeError(
            "FDA document has an unsupported representation "
            f"({content_type or 'unknown content type'}): {artifact.canonical_id}"
        )
    return ArtifactPayload(
        source_url=final_url,
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        mime_type=mime_type,
        fetched_at=fetched_at,
        response_metadata={
            "content_type": headers.get("content-type", ""),
            "etag": headers.get("etag", ""),
            "last_modified": headers.get("last-modified", ""),
        },
    )


def parse_artifact(payload: ArtifactPayload) -> ParsedPdf:
    if payload.mime_type.startswith("text/plain"):
        text = payload.content.decode("utf-8")
        return ParsedPdf(text=text, pages=[text], engine="utf8-inline")
    if payload.mime_type.startswith("text/html"):
        text = _extract_html_text(payload.content)
        if not text:
            raise RuntimeError("FDA HTML document contains no citable main text")
        return ParsedPdf(text=text, pages=[text], engine="selectolax-main-content")
    settings = get_settings()
    return parse_pdf(
        payload.content,
        timeout_s=settings.fda_corpus_pdf_parse_timeout_s,
        max_pages=settings.fda_corpus_pdf_max_pages,
    )


def _extract_html_text(content: bytes) -> str:
    """Extract visible FDA page text without executing or fetching subresources."""

    tree = HTMLParser(content.decode("utf-8", errors="replace"))
    for selector in ("script", "style", "noscript", "svg", "form", "nav", "header", "footer"):
        for node in tree.css(selector):
            node.decompose()
    root = (
        tree.css_first("main")
        or tree.css_first('[role="main"]')
        or tree.css_first("article")
        or tree.css_first("body")
    )
    if root is None:
        return ""
    lines = [" ".join(line.split()) for line in root.text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _chunk_artifact(
    artifact: CorpusArtifact,
    parsed: ParsedPdf,
    source_url: str,
) -> list[Chunk]:
    base = {
        "normalized_name": artifact.normalized_name or "",
        "dosage_form": artifact.dosage_form or "",
        "route": artifact.route or "",
        "source_url": source_url,
        "appl_no": artifact.application_number or "",
        "short_name": _short_name(artifact),
        "source_family": artifact.source_family.value,
        "document_type": artifact.document_type.value,
    }
    if artifact.source_family is FdaSourceFamily.PSG:
        return chunk_pdf(parsed.pages, base_metadata=base)
    return chunk_document_pages(parsed.pages, base_metadata=base)


def _index_rows(
    document_id: int,
    version_id: int,
    artifact: CorpusArtifact,
    chunks: list[Chunk],
    source_url: str,
) -> tuple[list[str], list[dict[str, object]]]:
    ids = [f"fda:{document_id}:{version_id}:{chunk.ordinal}" for chunk in chunks]
    metadata = [
        {
            **chunk.metadata,
            "fda_document_id": document_id,
            "fda_version_id": version_id,
            "source_url": source_url,
            "source_family": artifact.source_family.value,
            "document_type": artifact.document_type.value,
            "locator": f"page:{chunk.page}",
        }
        for chunk in chunks
    ]
    return ids, metadata


def _ready_version(
    canonical_id: str,
    content_hash: str,
    *,
    require_embeddings: bool = True,
) -> int | None:
    with session_scope() as session:
        row = session.exec(
            select(FdaDocumentVersion)
            .join(FdaDocument)
            .where(
                FdaDocument.canonical_id == canonical_id,
                FdaDocumentVersion.content_hash == content_hash,
                FdaDocumentVersion.processing_fingerprint == PROCESSING_FINGERPRINT,
            )
        ).first()
        if row is None or not _version_indexed_in_session(session, row):
            return None
        if require_embeddings and not _version_ready_in_session(session, row):
            return None
        return row.id


def _version_indexed_in_session(session: Session, version: FdaDocumentVersion) -> bool:
    if version.id is None or version.chunk_count <= 0:
        return False
    total = (
        session.connection()
        .execute(
            sa_text("SELECT count(*) FROM chunk WHERE fda_version_id = :version_id"),
            {"version_id": version.id},
        )
        .scalar()
    )
    return int(total or 0) == version.chunk_count


def _version_ready_in_session(session: Session, version: FdaDocumentVersion) -> bool:
    if not _version_indexed_in_session(session, version):
        return False
    if version.id is None:
        return False
    profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    params: dict[str, object]
    if profile_id == "legacy":
        sql = (
            "SELECT count(*) AS total, count(embedding) AS embedded FROM chunk "
            "WHERE fda_version_id = :version_id"
        )
        params = {"version_id": version.id}
    else:
        sql = (
            "SELECT count(*) AS total, count(ce.chunk_id) AS embedded FROM chunk c "
            "LEFT JOIN chunk_embedding ce ON ce.chunk_id = c.id AND ce.profile_id = :profile_id "
            "WHERE c.fda_version_id = :version_id"
        )
        params = {"version_id": version.id, "profile_id": profile_id}
    row = session.connection().execute(sa_text(sql), params).one()
    return int(row[0]) == version.chunk_count and int(row[1]) == version.chunk_count


def _touch_document(artifact: CorpusArtifact, source_url: str) -> None:
    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        doc = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).one()
        _apply_document_fields(doc, artifact, source_url)
        session.add(doc)


def _apply_document_fields(
    doc: FdaDocument,
    artifact: CorpusArtifact,
    source_url: str,
) -> None:
    doc.source_family = artifact.source_family.value
    doc.document_type = artifact.document_type.value
    doc.title = artifact.title
    doc.source_url = source_url
    doc.application_number = artifact.application_number
    doc.product_number = artifact.product_number
    doc.active_ingredient = artifact.active_ingredient
    doc.normalized_name = artifact.normalized_name
    doc.brand_name = artifact.brand_name
    doc.dosage_form = artifact.dosage_form
    doc.route = artifact.route
    doc.is_active = True
    doc.metadata_json = dict(artifact.metadata)
    doc.last_seen_at = datetime.now(UTC)


def _lock_document(session: Session, canonical_id: str) -> None:
    session.connection().execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:canonical_id, 0))"),
        {"canonical_id": canonical_id},
    )


def _persist_artifact(artifact: CorpusArtifact, payload: ArtifactPayload) -> Path:
    if payload.mime_type == "application/pdf":
        suffix = ".pdf"
    elif payload.mime_type.startswith("text/html"):
        suffix = ".html"
    else:
        suffix = ".txt"
    directory = get_settings().data_dir / "fda_corpus" / "artifacts" / artifact.source_family.value
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{payload.content_hash}{suffix}"
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != payload.content_hash:
            raise RuntimeError(f"content-addressed artifact is corrupt: {destination}")
        return destination
    fd, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _short_name(artifact: CorpusArtifact) -> str:
    return artifact.canonical_id.replace(":", "_").replace("/", "_")[:180]


def _create_run(stats: CorpusSyncStats, manifest: CorpusManifest) -> None:
    with session_scope() as session:
        session.add(
            FdaCorpusRun(
                id=stats.run_id,
                mode="sync",
                status="running",
                manifest_sha256=manifest.sha256,
                expected_documents=stats.expected_documents,
                discovered_documents=stats.discovered_documents,
                stats_json={
                    "errors": [],
                    "complete_universe": manifest.complete_universe,
                },
            )
        )


def _checkpoint_run(stats: CorpusSyncStats) -> None:
    with session_scope() as session:
        run = session.get(FdaCorpusRun, stats.run_id)
        if run is None:
            raise RuntimeError(f"corpus run vanished: {stats.run_id}")
        _copy_stats(run, stats)
        session.add(run)


def _finish_run(stats: CorpusSyncStats, *, status: str) -> None:
    with session_scope() as session:
        run = session.get(FdaCorpusRun, stats.run_id)
        if run is None:
            return
        _copy_stats(run, stats)
        run.status = status
        run.completed_at = datetime.now(UTC)
        session.add(run)


def _copy_stats(run: FdaCorpusRun, stats: CorpusSyncStats) -> None:
    run.added_documents = stats.added_documents
    run.revised_documents = stats.revised_documents
    run.unchanged_documents = stats.unchanged_documents
    run.error_documents = stats.error_documents
    run.chunks_written = stats.chunks_written
    complete_universe = bool((run.stats_json or {}).get("complete_universe"))
    run.stats_json = {
        "errors": stats.errors[-100:],
        "complete_universe": complete_universe,
        "retired_documents": stats.retired_documents,
        "embeddings_deferred": stats.embeddings_deferred,
        "workers": stats.workers,
    }


def _reconcile_active_manifest(manifest: CorpusManifest) -> int:
    """Retire documents absent from a successful complete discovery.

    This is intentionally forbidden for scoped or limited runs. The document
    flag and its current-search chunks change in one transaction, so retrieval
    cannot observe an inactive document that remains searchable.
    """

    canonical_ids = [artifact.canonical_id for artifact in manifest.artifacts]
    if not canonical_ids:
        raise RuntimeError("refusing to reconcile an empty complete corpus manifest")
    with session_scope() as session:
        session.connection().execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": "regwatch:authoritative-fda-corpus:reconcile"},
        )
        retired = list(
            session.exec(
                select(FdaDocument).where(
                    col(FdaDocument.is_active).is_(True),
                    col(FdaDocument.canonical_id).not_in(canonical_ids),
                )
            )
        )
        for document in retired:
            if document.id is None:
                raise RuntimeError("active FDA document has no id")
            delete_chunks_for_fda_document(document.id, conn=session.connection())
            document.is_active = False
            session.add(document)
        return len(retired)


def stats_dict(stats: CorpusSyncStats) -> dict[str, object]:
    """JSON/rich-safe public representation."""
    return asdict(stats) | {"succeeded": stats.succeeded}
