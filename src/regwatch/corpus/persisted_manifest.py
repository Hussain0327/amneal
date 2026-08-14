"""Durable exact-manifest handoff between discovery and shard workers."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings
from sqlmodel import col, select

from regwatch.corpus.artifact_store import ArtifactStore, build_artifact_store
from regwatch.corpus.manifest import CorpusManifest, load_manifest_gzip, write_manifest_gzip
from regwatch.store.db import session_scope
from regwatch.store.models import FdaCorpusManifest


@dataclass(frozen=True)
class PersistedManifest:
    manifest_sha256: str
    artifact_uri: str
    artifact_sha256: str
    document_count: int


def persist_manifest(
    manifest: CorpusManifest,
    *,
    artifact_store: ArtifactStore | None = None,
) -> PersistedManifest:
    """Persist one exact manifest and atomically publish its database pointer."""

    store = artifact_store or build_artifact_store()
    path = _temporary_path("write")
    try:
        artifact_sha256 = write_manifest_gzip(manifest, path)
        reference = store.put_file(
            path,
            content_hash=artifact_sha256,
            namespace="manifests",
            suffix=".json.gz",
        )
    finally:
        path.unlink(missing_ok=True)
    if not reference.retained:
        raise RuntimeError("exact corpus manifests require retained artifact storage")

    with session_scope() as session:
        row = session.get(FdaCorpusManifest, manifest.sha256)
        if row is None:
            row = FdaCorpusManifest(
                sha256=manifest.sha256,
                artifact_uri=reference.uri,
                artifact_sha256=artifact_sha256,
                document_count=len(manifest.artifacts),
                complete_universe=manifest.complete_universe,
                source_snapshots_json=dict(manifest.source_snapshots),
                counts_json={
                    "by_source_family": manifest.counts_by_family(),
                    "by_document_type": manifest.counts_by_document_type(),
                },
            )
        else:
            expected = (
                row.document_count,
                row.complete_universe,
                dict(row.source_snapshots_json),
            )
            actual = (
                len(manifest.artifacts),
                manifest.complete_universe,
                dict(manifest.source_snapshots),
            )
            if expected != actual:
                raise RuntimeError("manifest fingerprint collided with different discovery facts")
            row.artifact_uri = reference.uri
            row.artifact_sha256 = artifact_sha256
            row.artifact_retained = True
        session.add(row)
    return PersistedManifest(
        manifest_sha256=manifest.sha256,
        artifact_uri=reference.uri,
        artifact_sha256=artifact_sha256,
        document_count=len(manifest.artifacts),
    )


def load_persisted_manifest(
    manifest_sha256: str,
    *,
    artifact_store: ArtifactStore | None = None,
) -> CorpusManifest:
    store = artifact_store or build_artifact_store()
    with session_scope() as session:
        row = session.exec(
            select(FdaCorpusManifest).where(FdaCorpusManifest.sha256 == manifest_sha256)
        ).one()
        uri = row.artifact_uri
        expected_artifact_hash = row.artifact_sha256
        expected_documents = row.document_count

    path = _temporary_path("read")
    try:
        store.materialize(uri, path)
        actual_artifact_hash = _file_sha256(path)
        if actual_artifact_hash != expected_artifact_hash:
            raise RuntimeError("persisted corpus manifest artifact checksum mismatch")
        manifest = load_manifest_gzip(path)
    finally:
        path.unlink(missing_ok=True)
    if manifest.sha256 != manifest_sha256:
        raise RuntimeError("persisted corpus manifest logical checksum mismatch")
    if len(manifest.artifacts) != expected_documents:
        raise RuntimeError("persisted corpus manifest document count mismatch")
    return manifest


def latest_persisted_manifest_sha256(*, complete_universe: bool = True) -> str:
    with session_scope() as session:
        statement = select(FdaCorpusManifest)
        if complete_universe:
            statement = statement.where(col(FdaCorpusManifest.complete_universe).is_(True))
        row = session.exec(statement.order_by(col(FdaCorpusManifest.created_at).desc())).first()
        if row is None:
            raise RuntimeError("no persisted authoritative FDA corpus manifest exists")
        return row.sha256


def _temporary_path(operation: str) -> Path:
    directory = get_settings().fda_corpus_temp_dir
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"regwatch-fda-manifest-{operation}-",
        suffix=".json.gz.part",
        dir=directory,
    )
    os.close(fd)
    return Path(name)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
