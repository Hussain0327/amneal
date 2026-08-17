"""Resumable, per-document atomic synchronization of the FDA corpus."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
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
from regwatch.corpus.artifact_store import (
    ArtifactReference,
    ArtifactStore,
    build_artifact_store,
)
from regwatch.corpus.lifecycle import (
    embedding_complete_in_session,
    mark_embedding_failed,
    refresh_embedding_state,
    upsert_embedding_state,
)
from regwatch.corpus.manifest import CorpusArtifact, CorpusManifest
from regwatch.corpus.resolution import (
    SOURCE_BYTES_HASH_KIND,
    TERMINAL_OBSERVATION_HASH_KIND,
    TERMINAL_OBSERVATION_MIME_TYPE,
    TERMINAL_RESOLUTION_STATUSES,
    ResolutionStatus,
    missing_observation_content_hash,
    missing_observation_fingerprint,
)
from regwatch.corpus.sharding import corpus_shard_id
from regwatch.ingest.embedding_writer import (
    legacy_document_embeddings,
    profile_document_embeddings,
    write_profile_batches,
)
from regwatch.ingest.pdf_parser import ParsedPdf, PdfParseError, parse_pdf_path
from regwatch.process.chunker import CHUNKING_VERSION, Chunk, chunk_document_pages, chunk_pdf
from regwatch.sources.http import (
    SourceMissingError,
    download_authoritative_file,
    owned_fda_client,
)
from regwatch.sources.policy import FdaSourceFamily
from regwatch.store.db import session_scope
from regwatch.store.models import (
    FdaCorpusRun,
    FdaDocument,
    FdaDocumentVersion,
)
from regwatch.store.vector_store import (
    add_chunks,
    delete_chunks_for_fda_document,
    update_legacy_chunk_embeddings,
)

log = get_logger(__name__)

_PROCESSING_SPEC = {
    "schema_version": 2,
    "chunking_version": CHUNKING_VERSION,
    "pdf_parser": "path-pdfplumber-pypdf-tesseract-page-preserving-v2",
    "inline_parser": "utf8-single-page-v1",
    "html_parser": "selectolax-main-content-v1",
}


def _processing_spec() -> dict[str, object]:
    settings = get_settings()
    return _PROCESSING_SPEC | {
        "ocr": {
            "enabled": settings.fda_corpus_ocr_enabled,
            "contract": "tesseract-grayscale-page-v1",
            "language": settings.fda_corpus_ocr_language,
            "dpi": settings.fda_corpus_ocr_dpi,
        }
    }


def _processing_fingerprint(spec: dict[str, object]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ArtifactPayload:
    source_url: str
    path: Path
    content_hash: str
    byte_size: int
    mime_type: str
    fetched_at: datetime
    response_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AcquiredVersion:
    version_id: int
    was_added: bool
    prior_complete_versions: int
    indexed: bool
    embedding_ready: bool


@dataclass
class CorpusSyncStats:
    run_id: str
    expected_documents: int
    discovered_documents: int
    added_documents: int = 0
    revised_documents: int = 0
    unchanged_documents: int = 0
    terminal_documents: int = 0
    error_documents: int = 0
    retired_documents: int = 0
    chunks_written: int = 0
    embeddings_deferred: bool = False
    workers: int = 1
    shard_id: int | None = None
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
    artifact_store: ArtifactStore | None = None,
    shard_id: int | None = None,
) -> CorpusSyncStats:
    """Synchronize a manifest, committing one complete document at a time."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    if shard_id is not None and not 0 <= shard_id < 512:
        raise ValueError("shard_id must be between 0 and 511")
    discovered = list(manifest.artifacts)
    if shard_id is not None:
        discovered = [
            artifact
            for artifact in discovered
            if corpus_shard_id(artifact.canonical_id) == shard_id
        ]
    artifacts = discovered[:limit] if limit else discovered
    run_id = str(uuid.uuid4())
    stats = CorpusSyncStats(
        run_id=run_id,
        expected_documents=len(artifacts),
        discovered_documents=len(discovered),
        embeddings_deferred=defer_embeddings,
        workers=workers,
        shard_id=shard_id,
    )
    complete_run = manifest.complete_universe and not limit and shard_id is None
    _create_run(stats, manifest, complete_universe=complete_run)
    try:
        selected_store = artifact_store or build_artifact_store()
        with owned_fda_client(client) as active_client:
            for artifact, outcome in _sync_artifacts(
                artifacts,
                manifest_sha256=manifest.sha256,
                client=active_client,
                defer_embeddings=defer_embeddings,
                workers=workers,
                artifact_store=selected_store,
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
        if stats.succeeded and manifest.complete_universe and not limit and shard_id is None:
            stats.retired_documents = reconcile_active_manifest(manifest)
    except BaseException:
        _finish_run(stats, status="failed")
        raise
    _finish_run(stats, status="succeeded" if stats.succeeded else "failed")
    return stats


def _sync_artifacts(
    artifacts: list[CorpusArtifact],
    *,
    manifest_sha256: str,
    client: httpx.Client,
    defer_embeddings: bool,
    workers: int,
    artifact_store: ArtifactStore,
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
                    manifest_sha256=manifest_sha256,
                    client=client,
                    defer_embeddings=defer_embeddings,
                    artifact_store=artifact_store,
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
    manifest_sha256: str,
    client: httpx.Client,
    defer_embeddings: bool = False,
    artifact_store: ArtifactStore | None = None,
) -> tuple[str, int]:
    """Stage, retain, parse, and publish one document with durable checkpoints."""

    spec = _processing_spec()
    fingerprint = _processing_fingerprint(spec)
    if _already_complete(
        artifact,
        processing_fingerprint=fingerprint,
        defer_embeddings=defer_embeddings,
    ):
        _touch_document(artifact, source_url=None)
        return "unchanged", 0
    selected_store = artifact_store or build_artifact_store()
    try:
        with fetch_artifact(artifact, client=client) as payload:
            reference = selected_store.put_file(
                payload.path,
                content_hash=payload.content_hash,
                namespace=f"documents/{artifact.source_family.value}",
                suffix=_artifact_suffix(payload.mime_type),
            )
            acquired = _record_acquired_version(
                artifact,
                payload,
                reference,
                processing_fingerprint=fingerprint,
                processing_spec=spec,
            )
            if acquired.indexed and (defer_embeddings or acquired.embedding_ready):
                _touch_document(artifact, source_url=payload.source_url)
                return "unchanged", 0
            if acquired.indexed:
                try:
                    _embed_existing_version(acquired.version_id)
                except Exception as exc:
                    _mark_active_embedding_failed(acquired.version_id, exc)
                    raise
                return "unchanged", 0

            _begin_resolution_attempt(
                acquired.version_id,
                manifest_sha256=manifest_sha256,
            )
            try:
                parsed = parse_artifact(payload)
                chunks = _chunk_artifact(artifact, parsed, payload.source_url)
                if not chunks:
                    raise RuntimeError(
                        f"artifact produced no citable text chunks: {artifact.canonical_id}"
                    )
                status, ids, texts = _publish_chunks(
                    artifact,
                    payload,
                    parsed,
                    chunks,
                    acquired,
                )
            except PdfParseError as exc:
                if _mark_unparseable_failure(
                    acquired.version_id,
                    artifact,
                    manifest_sha256=manifest_sha256,
                    exc=exc,
                ):
                    return "terminal", 0
                raise
            except Exception as exc:
                _mark_chunk_failed(acquired.version_id, exc)
                raise

            if not defer_embeddings:
                try:
                    _embed_chunk_rows(acquired.version_id, ids, texts)
                except Exception as exc:
                    _mark_active_embedding_failed(acquired.version_id, exc)
                    raise
            return status, len(chunks)
    except SourceMissingError as exc:
        if _record_missing_source_attempt(
            artifact,
            manifest_sha256=manifest_sha256,
            exc=exc,
        ):
            return "terminal", 0
        raise


@contextmanager
def fetch_artifact(
    artifact: CorpusArtifact,
    *,
    client: httpx.Client,
) -> Iterator[ArtifactPayload]:
    """Stage exactly one bounded artifact and unlink it on every exit path."""

    fetched_at = datetime.now(UTC)
    settings = get_settings()
    directory = settings.fda_corpus_temp_dir
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    if artifact.inline_text is not None:
        content = artifact.inline_text.encode("utf-8")
        if len(content) > settings.fda_corpus_pdf_max_bytes:
            raise RuntimeError("inline FDA snapshot row exceeds the corpus artifact byte limit")
        fd, name = tempfile.mkstemp(prefix="regwatch-fda-inline-", suffix=".part", dir=directory)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            yield ArtifactPayload(
                source_url=artifact.source_url,
                path=path,
                content_hash=hashlib.sha256(content).hexdigest(),
                byte_size=len(content),
                mime_type="text/plain; charset=utf-8",
                fetched_at=fetched_at,
                response_metadata={"representation": "deterministic_fda_snapshot_record"},
            )
        finally:
            path.unlink(missing_ok=True)
        return

    with download_authoritative_file(
        client,
        artifact.source_url,
        artifact.source_family,
        max_bytes=settings.fda_corpus_pdf_max_bytes,
        directory=directory,
        min_interval_s=settings.crawl_min_interval_ms / 1000.0,
    ) as downloaded:
        content_type = downloaded.headers.get("content-type", "").lower()
        with downloaded.path.open("rb") as handle:
            leading = handle.read(512).lstrip().lower()
        if leading.startswith(b"%pdf-"):
            mime_type = "application/pdf"
        elif "text/html" in content_type or leading.startswith((b"<!doctype html", b"<html")):
            mime_type = "text/html; charset=utf-8"
        else:
            raise RuntimeError(
                "FDA document has an unsupported representation "
                f"({content_type or 'unknown content type'}): {artifact.canonical_id}"
            )
        yield ArtifactPayload(
            source_url=downloaded.final_url,
            path=downloaded.path,
            content_hash=downloaded.sha256,
            byte_size=downloaded.byte_size,
            mime_type=mime_type,
            fetched_at=fetched_at,
            response_metadata={
                "content_type": downloaded.headers.get("content-type", ""),
                "etag": downloaded.headers.get("etag", ""),
                "last_modified": downloaded.headers.get("last-modified", ""),
            },
        )


def parse_artifact(payload: ArtifactPayload) -> ParsedPdf:
    if payload.mime_type.startswith("text/plain"):
        text = payload.path.read_text(encoding="utf-8")
        return ParsedPdf(text=text, pages=[text], engine="utf8-inline")
    if payload.mime_type.startswith("text/html"):
        text = _extract_html_text(payload.path.read_bytes())
        if not text:
            raise RuntimeError("FDA HTML document contains no citable main text")
        return ParsedPdf(text=text, pages=[text], engine="selectolax-main-content")
    settings = get_settings()
    return parse_pdf_path(
        payload.path,
        timeout_s=settings.fda_corpus_pdf_parse_timeout_s,
        max_pages=settings.fda_corpus_pdf_max_pages,
    )


def _artifact_suffix(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return ".pdf"
    if mime_type.startswith("text/html"):
        return ".html"
    return ".txt"


def _record_acquired_version(
    artifact: CorpusArtifact,
    payload: ArtifactPayload,
    reference: ArtifactReference,
    *,
    processing_fingerprint: str,
    processing_spec: dict[str, object],
) -> AcquiredVersion:
    """Commit checksum/storage provenance before any untrusted parse work."""

    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        document = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).first()
        was_added = document is None
        if document is None:
            document = FdaDocument(
                canonical_id=artifact.canonical_id,
                source_family=artifact.source_family.value,
                document_type=artifact.document_type.value,
                title=artifact.title,
                source_url=payload.source_url,
            )
            session.add(document)
            session.flush()
        if document.id is None:
            raise RuntimeError("fda_document insert did not produce an id")

        prior_complete_versions = int(
            session.exec(
                select(func.count()).where(
                    FdaDocumentVersion.fda_document_id == document.id,
                    FdaDocumentVersion.chunk_status == "complete",
                )
            ).one()
        )
        version = session.exec(
            select(FdaDocumentVersion).where(
                FdaDocumentVersion.fda_document_id == document.id,
                FdaDocumentVersion.content_hash == payload.content_hash,
                FdaDocumentVersion.processing_fingerprint == processing_fingerprint,
            )
        ).first()
        if version is None:
            version = FdaDocumentVersion(
                fda_document_id=document.id,
                content_hash=payload.content_hash,
                content_hash_kind=SOURCE_BYTES_HASH_KIND,
                processing_fingerprint=processing_fingerprint,
                source_updated_at=artifact.source_updated_at,
                fetched_at=payload.fetched_at,
                acquired_at=payload.fetched_at,
                mime_type=payload.mime_type,
                byte_size=payload.byte_size,
                page_count=0,
                chunk_count=0,
                artifact_uri=reference.uri,
                artifact_retained=reference.retained,
                chunk_status="pending",
                resolution_status=ResolutionStatus.PENDING.value,
                metadata_json={
                    **payload.response_metadata,
                    "processing_spec": processing_spec,
                },
            )
            session.add(version)
            session.flush()
        else:
            if (
                version.byte_size != payload.byte_size
                or version.mime_type != payload.mime_type
                or version.content_hash_kind != SOURCE_BYTES_HASH_KIND
            ):
                raise RuntimeError("immutable FDA version acquisition metadata changed")
            if reference.retained or not version.artifact_uri:
                version.artifact_uri = reference.uri
                version.artifact_retained = reference.retained
            if (
                version.chunk_status != "complete"
                and version.resolution_status not in TERMINAL_RESOLUTION_STATUSES
            ):
                version.chunk_status = "pending"
                version.chunk_error = None
                version.resolution_status = ResolutionStatus.PENDING.value
                version.resolution_error = None
                version.resolved_at = None
            version.metadata_json = {
                **(version.metadata_json or {}),
                **payload.response_metadata,
                "processing_spec": processing_spec,
            }
            session.add(version)
        if version.id is None:
            raise RuntimeError("fda_document_version insert did not produce an id")

        _make_version_current(session, document.id, version.id)
        _apply_document_fields(document, artifact, payload.source_url)
        session.add(document)
        indexed = version.chunk_status == "complete" and _version_indexed_in_session(
            session, version
        )
        if indexed and version.resolution_status == ResolutionStatus.PENDING.value:
            # A worker running pre-0025 code can complete a version while the
            # ledger columns hold their ADD COLUMN defaults; migration 0025's
            # one-time backfill cannot cover rows written after it commits.
            # Credit the proven-searchable row here, or it stays 'pending'
            # forever and permanently blocks its shard's readiness.
            version.resolution_status = ResolutionStatus.INDEXED.value
            version.resolved_at = version.chunked_at or datetime.now(UTC)
            version.resolution_error = None
            session.add(version)
        embedding_ready = indexed and _version_ready_in_session(session, version)
        return AcquiredVersion(
            version_id=version.id,
            was_added=was_added,
            prior_complete_versions=prior_complete_versions,
            indexed=indexed,
            embedding_ready=embedding_ready,
        )


def _publish_chunks(
    artifact: CorpusArtifact,
    payload: ArtifactPayload,
    parsed: ParsedPdf,
    chunks: list[Chunk],
    acquired: AcquiredVersion,
) -> tuple[str, list[str], list[str]]:
    texts = [chunk.text for chunk in chunks]
    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        document = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).one()
        version = session.get(FdaDocumentVersion, acquired.version_id)
        if document.id is None or version is None or version.id is None:
            raise RuntimeError("acquired FDA document version vanished before chunk publish")
        if version.fda_document_id != document.id:
            raise RuntimeError("acquired FDA version no longer belongs to its document")

        if version.chunk_status == "complete" and _version_indexed_in_session(session, version):
            if version.resolution_status == ResolutionStatus.PENDING.value:
                # Same pre-0025-writer self-heal as _record_acquired_version,
                # reachable when a concurrent worker completed this version
                # between acquisition and publish.
                version.resolution_status = ResolutionStatus.INDEXED.value
                version.resolved_at = version.chunked_at or datetime.now(UTC)
                version.resolution_error = None
                session.add(version)
            rows = session.connection().execute(
                sa_text(
                    "SELECT id, text FROM chunk WHERE fda_version_id = :version_id ORDER BY id"
                ),
                {"version_id": version.id},
            )
            existing = [(str(row[0]), str(row[1])) for row in rows]
            return "unchanged", [row[0] for row in existing], [row[1] for row in existing]

        _apply_document_fields(document, artifact, payload.source_url)
        session.add(document)
        delete_chunks_for_fda_document(document.id, conn=session.connection())
        ids, metadata = _index_rows(
            document.id,
            version.id,
            artifact,
            chunks,
            payload.source_url,
        )
        add_chunks(
            ids=ids,
            embeddings=[None] * len(texts),
            documents=texts,
            metadatas=metadata,
            conn=session.connection(),
        )
        version.page_count = len(parsed.pages)
        version.chunk_count = len(chunks)
        version.parse_engine = parsed.engine
        version.chunk_status = "complete"
        version.chunked_at = datetime.now(UTC)
        version.chunk_error = None
        version.is_current = True
        version.resolution_status = ResolutionStatus.INDEXED.value
        version.resolved_at = version.chunked_at
        version.resolution_error = None
        version.resolution_evidence_json = {}
        session.add(version)
        for profile_id in _target_embedding_profile_ids():
            upsert_embedding_state(
                session,
                version.id,
                profile_id,
                expected_chunks=len(chunks),
                embedded_chunks=0,
                status="pending",
            )
        status = (
            "added" if acquired.was_added or acquired.prior_complete_versions == 0 else "revised"
        )
        return status, ids, texts


def _target_embedding_profile_ids() -> tuple[str, ...]:
    settings = get_settings()
    active = (settings.active_embedding_profile or "legacy").strip()
    shadow = (settings.embedding_shadow_profile or "").strip()
    return tuple(dict.fromkeys(profile for profile in (active, shadow) if profile))


def _embed_chunk_rows(version_id: int, ids: list[str], texts: list[str]) -> None:
    legacy = list(legacy_document_embeddings(texts))
    if any(vector is not None for vector in legacy):
        if not all(vector is not None for vector in legacy):
            raise RuntimeError("legacy embedding provider returned a partial document batch")
        update_legacy_chunk_embeddings(
            ids,
            texts,
            [vector for vector in legacy if vector is not None],
        )
        refresh_embedding_state(version_id, "legacy")

    batches = profile_document_embeddings(texts)
    if batches:
        with session_scope() as session:
            write_profile_batches(session, ids, batches)
        for batch in batches:
            refresh_embedding_state(version_id, batch.profile_id)

    active = (get_settings().active_embedding_profile or "legacy").strip()
    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None or not embedding_complete_in_session(session, version, active):
            raise RuntimeError(f"required embedding profile is incomplete after write: {active}")


def _embed_existing_version(version_id: int) -> None:
    with session_scope() as session:
        rows = session.connection().execute(
            sa_text("SELECT id, text FROM chunk WHERE fda_version_id = :version_id ORDER BY id"),
            {"version_id": version_id},
        )
        materialized = [(str(row[0]), str(row[1])) for row in rows]
    if not materialized:
        raise RuntimeError("indexed FDA version has no chunks to embed")
    _embed_chunk_rows(
        version_id,
        [row[0] for row in materialized],
        [row[1] for row in materialized],
    )


def _begin_resolution_attempt(version_id: int, *, manifest_sha256: str) -> None:
    """Durably count one exact-manifest parser invocation before it begins."""

    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None:
            raise RuntimeError("FDA version vanished before its resolution attempt")
        evidence = version.resolution_evidence_json or {}
        prior_manifest = evidence.get("manifest_sha256") or evidence.get("attempt_manifest_sha256")
        if version.resolution_attempts and prior_manifest != manifest_sha256:
            version.resolution_attempts = 0
        # A retry reopens a terminal row while work is in flight. Acceptance
        # must never count stale terminal evidence from another attempt or
        # manifest as a current resolution.
        version.resolution_status = ResolutionStatus.PENDING.value
        version.resolved_at = None
        version.resolution_error = None
        version.resolution_attempts += 1
        version.last_resolution_attempt_at = datetime.now(UTC)
        version.resolution_evidence_json = {
            "schema_version": 1,
            "attempt_manifest_sha256": manifest_sha256,
            "attempts": version.resolution_attempts,
        }
        session.add(version)


def _record_missing_source_attempt(
    artifact: CorpusArtifact,
    *,
    manifest_sha256: str,
    exc: SourceMissingError,
) -> bool:
    """Record an exact 404 and terminalize only after the durable retry budget."""

    now = datetime.now(UTC)
    error = f"{type(exc).__name__}: {exc}"[:2_000]
    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        document = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).first()
        if document is None:
            document = FdaDocument(
                canonical_id=artifact.canonical_id,
                source_family=artifact.source_family.value,
                document_type=artifact.document_type.value,
                title=artifact.title,
                source_url=artifact.source_url,
            )
            session.add(document)
            session.flush()
        if document.id is None:
            raise RuntimeError("fda_document insert did not produce an id")

        content_hash = missing_observation_content_hash(artifact)
        fingerprint = missing_observation_fingerprint()
        version = session.exec(
            select(FdaDocumentVersion).where(
                FdaDocumentVersion.fda_document_id == document.id,
                FdaDocumentVersion.content_hash == content_hash,
                FdaDocumentVersion.processing_fingerprint == fingerprint,
            )
        ).first()
        if version is None:
            version = FdaDocumentVersion(
                fda_document_id=document.id,
                content_hash=content_hash,
                content_hash_kind=TERMINAL_OBSERVATION_HASH_KIND,
                processing_fingerprint=fingerprint,
                source_updated_at=artifact.source_updated_at,
                fetched_at=now,
                acquired_at=now,
                mime_type=TERMINAL_OBSERVATION_MIME_TYPE,
                byte_size=0,
                page_count=0,
                chunk_count=0,
                artifact_uri=None,
                artifact_retained=False,
                chunk_status="failed",
                chunk_error=error,
                resolution_status=ResolutionStatus.PENDING.value,
                metadata_json={"observation_schema_version": 1},
            )
            session.add(version)
            session.flush()
        elif version.content_hash_kind != TERMINAL_OBSERVATION_HASH_KIND:
            raise RuntimeError("missing-source observation collided with captured FDA bytes")
        if version.id is None:
            raise RuntimeError("FDA source observation did not produce a version id")

        _make_version_current(session, document.id, version.id)
        _apply_document_fields(document, artifact, artifact.source_url)
        prior_evidence = version.resolution_evidence_json or {}
        if version.resolution_attempts and prior_evidence.get("manifest_sha256") != manifest_sha256:
            version.resolution_attempts = 0
        version.resolution_attempts += 1
        version.last_resolution_attempt_at = now
        version.chunk_status = "failed"
        version.chunk_count = 0
        version.chunked_at = None
        version.chunk_error = error
        version.resolution_error = error
        version.resolution_evidence_json = {
            "schema_version": 1,
            "canonical_id": artifact.canonical_id,
            "source_url": artifact.source_url,
            "observed_url": str(exc.response.url),
            "http_status": 404,
            "manifest_sha256": manifest_sha256,
            "attempts": version.resolution_attempts,
        }
        terminal = version.resolution_attempts >= get_settings().fda_corpus_terminal_attempts
        if terminal:
            delete_chunks_for_fda_document(document.id, conn=session.connection())
            version.resolution_status = ResolutionStatus.MISSING_AT_SOURCE.value
            version.resolved_at = now
        else:
            version.resolution_status = ResolutionStatus.PENDING.value
            version.resolved_at = None
        session.add(document)
        session.add(version)
        return terminal


def _mark_unparseable_failure(
    version_id: int,
    artifact: CorpusArtifact,
    *,
    manifest_sha256: str,
    exc: PdfParseError,
) -> bool:
    """Persist parser evidence and return whether this version is now terminal."""

    now = datetime.now(UTC)
    error = f"{type(exc).__name__}: {exc}"[:2_000]
    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None:
            raise RuntimeError("FDA version vanished while recording parser failure")
        document = session.get(FdaDocument, version.fda_document_id)
        if document is None or document.id is None:
            raise RuntimeError("FDA document vanished while recording parser failure")

        version.chunk_status = "failed"
        version.chunk_count = 0
        version.chunked_at = None
        version.chunk_error = error
        version.resolution_error = error
        version.resolution_evidence_json = {
            "schema_version": 1,
            "canonical_id": artifact.canonical_id,
            "source_url": artifact.source_url,
            "manifest_sha256": manifest_sha256,
            "error_type": type(exc).__name__,
            "attempts": version.resolution_attempts,
        }
        terminal = (
            version.artifact_retained
            and bool(version.artifact_uri)
            and version.resolution_attempts >= get_settings().fda_corpus_terminal_attempts
        )
        if terminal:
            delete_chunks_for_fda_document(document.id, conn=session.connection())
            version.resolution_status = ResolutionStatus.UNPARSEABLE.value
            version.resolved_at = now
        else:
            version.resolution_status = ResolutionStatus.PENDING.value
            version.resolved_at = None
        session.add(version)
        return terminal


def _mark_chunk_failed(version_id: int, exc: Exception) -> None:
    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None or version.chunk_status == "complete":
            return
        version.chunk_status = "failed"
        version.chunk_error = f"{type(exc).__name__}: {exc}"[:2_000]
        if version.resolution_status not in TERMINAL_RESOLUTION_STATUSES:
            version.resolution_status = ResolutionStatus.PENDING.value
            version.resolved_at = None
            version.resolution_error = version.chunk_error
        session.add(version)


def _mark_active_embedding_failed(version_id: int, exc: Exception) -> None:
    profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    mark_embedding_failed(version_id, profile_id, exc)


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
    return embedding_complete_in_session(session, version, profile_id)


def _already_complete(
    artifact: CorpusArtifact,
    *,
    processing_fingerprint: str,
    defer_embeddings: bool,
) -> bool:
    """Whether a paced FDA fetch for this artifact cannot produce new work.

    Mirrors the post-fetch ``acquired.indexed and (defer_embeddings or
    acquired.embedding_ready)`` unchanged check, evaluated from the lifecycle
    tables alone so an already-complete document costs one read transaction
    instead of a politeness-paced download (~5s each on every driver resume).

    Only versions the resolution ledger already credits as INDEXED may skip:
    a chunk-complete row still marked pending (written by pre-0025 worker
    code) must take the fetch path so _record_acquired_version's self-heal
    can credit it, or its shard's readiness would never recover.

    Content drift is deliberately not detected here: the backfill processes a
    frozen manifest snapshot, and reconciling live-URL drift is the watch
    pipeline's job. Any uncertainty (missing document, other fingerprint,
    partial chunks, missing embeddings) returns False and takes the fetch
    path unchanged.
    """
    if artifact.inline_text is not None:
        # Inline artifacts carry their content in the manifest: staging them
        # costs no FDA request, and their content hash is how revisions are
        # detected, so skipping would trade correctness for nothing.
        return False
    with session_scope() as session:
        document = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).first()
        if document is None or document.id is None:
            return False
        versions = session.exec(
            select(FdaDocumentVersion).where(
                FdaDocumentVersion.fda_document_id == document.id,
                FdaDocumentVersion.processing_fingerprint == processing_fingerprint,
                FdaDocumentVersion.chunk_status == "complete",
                FdaDocumentVersion.resolution_status == ResolutionStatus.INDEXED.value,
            )
        ).all()
        for version in versions:
            if not _version_indexed_in_session(session, version):
                continue
            if defer_embeddings or _version_ready_in_session(session, version):
                return True
        return False


def _touch_document(artifact: CorpusArtifact, *, source_url: str | None) -> None:
    """Refresh manifest-sourced document fields; ``source_url=None`` keeps the
    redirect-resolved URL the last real fetch established (the skip path has no
    payload, and the manifest URL may be the pre-redirect form)."""
    with session_scope() as session:
        _lock_document(session, artifact.canonical_id)
        doc = session.exec(
            select(FdaDocument).where(FdaDocument.canonical_id == artifact.canonical_id)
        ).one()
        _apply_document_fields(doc, artifact, doc.source_url if source_url is None else source_url)
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
    doc.shard_id = corpus_shard_id(artifact.canonical_id)
    doc.is_active = True
    doc.metadata_json = dict(artifact.metadata)
    doc.last_seen_at = datetime.now(UTC)


def _make_version_current(session: Session, document_id: int, version_id: int) -> None:
    """Select one exact current lifecycle row under the document advisory lock."""

    # Flush first so a newly acquired target row exists before the direct SQL
    # updates.  Keep the target out of the clearing update: on a retry the ORM
    # identity map may still hold ``is_current=True`` while direct SQL has
    # changed the database value, in which case assigning True again would not
    # be detected as a dirty attribute and the current marker would be lost.
    session.flush()
    session.connection().execute(
        sa_text(
            "UPDATE fda_document_version SET is_current = false "
            "WHERE fda_document_id = :document_id "
            "AND id <> :version_id AND is_current"
        ),
        {"document_id": document_id, "version_id": version_id},
    )
    session.connection().execute(
        sa_text(
            "UPDATE fda_document_version SET is_current = true "
            "WHERE fda_document_id = :document_id "
            "AND id = :version_id AND NOT is_current"
        ),
        {"document_id": document_id, "version_id": version_id},
    )
    version = session.get(FdaDocumentVersion, version_id)
    if version is None:
        raise RuntimeError("FDA version vanished while selecting the current version")
    session.expire(version, ["is_current"])
    if not version.is_current:
        raise RuntimeError("FDA version could not be selected as current")


def _lock_document(session: Session, canonical_id: str) -> None:
    session.connection().execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:canonical_id, 0))"),
        {"canonical_id": canonical_id},
    )


def _short_name(artifact: CorpusArtifact) -> str:
    return artifact.canonical_id.replace(":", "_").replace("/", "_")[:180]


def _create_run(
    stats: CorpusSyncStats,
    manifest: CorpusManifest,
    *,
    complete_universe: bool,
) -> None:
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
                    "complete_universe": complete_universe,
                    "shard_id": stats.shard_id,
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
    run.terminal_documents = stats.terminal_documents
    run.error_documents = stats.error_documents
    run.chunks_written = stats.chunks_written
    complete_universe = bool((run.stats_json or {}).get("complete_universe"))
    run.stats_json = {
        "errors": stats.errors[-100:],
        "complete_universe": complete_universe,
        "retired_documents": stats.retired_documents,
        "embeddings_deferred": stats.embeddings_deferred,
        "workers": stats.workers,
        "shard_id": stats.shard_id,
    }


def reconcile_active_manifest(manifest: CorpusManifest) -> int:
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
