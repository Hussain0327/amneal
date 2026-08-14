"""Fail-closed shard and full-manifest acceptance for the FDA corpus."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config.settings import get_settings
from sqlalchemy import text as sa_text

from regwatch.corpus.manifest import CorpusManifest
from regwatch.corpus.sharding import FDA_CORPUS_SHARD_COUNT, corpus_shard_id
from regwatch.corpus.sync import reconcile_active_manifest
from regwatch.sources.policy import allowed_source_families
from regwatch.store.db import get_engine, session_scope
from regwatch.store.models import FdaCorpusRun


@dataclass(frozen=True)
class ShardReadiness:
    shard_id: int
    expected_documents: int
    chunked_documents: int
    embedded_documents: int
    chunks: int
    embedded_chunks: int
    issues: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return (
            not self.issues
            and self.chunked_documents == self.expected_documents
            and self.embedded_documents == self.expected_documents
        )


@dataclass(frozen=True)
class ManifestReadiness:
    manifest_sha256: str
    profile_id: str
    expected_documents: int
    chunked_documents: int
    embedded_documents: int
    chunks: int
    embedded_chunks: int
    incomplete_shards: tuple[int, ...]
    issues: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.issues and not self.incomplete_shards


def shard_readiness(
    manifest: CorpusManifest,
    shard_id: int,
    *,
    profile_id: str | None = None,
) -> ShardReadiness:
    selected = (profile_id or get_settings().active_embedding_profile or "legacy").strip()
    with get_engine().connect() as connection:
        return _shard_readiness_with_connection(
            connection,
            manifest,
            shard_id,
            profile_id=selected,
        )


def manifest_readiness(
    manifest: CorpusManifest,
    *,
    profile_id: str | None = None,
) -> ManifestReadiness:
    selected = (profile_id or get_settings().active_embedding_profile or "legacy").strip()
    shard_results: list[ShardReadiness] = []
    with get_engine().connect() as connection:
        for shard_id in range(FDA_CORPUS_SHARD_COUNT):
            shard_results.append(
                _shard_readiness_with_connection(
                    connection,
                    manifest,
                    shard_id,
                    profile_id=selected,
                )
            )
    issues: list[str] = []
    missing_families = [
        family
        for family in allowed_source_families()
        if manifest.counts_by_family().get(family, 0) <= 0
    ]
    if missing_families:
        issues.append("manifest is missing source families: " + ", ".join(missing_families))
    if not manifest.complete_universe:
        issues.append("manifest is not marked as a complete FDA universe")
    for result in shard_results:
        issues.extend(result.issues[:3])
        if len(issues) >= 100:
            break
    incomplete = tuple(result.shard_id for result in shard_results if not result.ready)
    return ManifestReadiness(
        manifest_sha256=manifest.sha256,
        profile_id=selected,
        expected_documents=len(manifest.artifacts),
        chunked_documents=sum(result.chunked_documents for result in shard_results),
        embedded_documents=sum(result.embedded_documents for result in shard_results),
        chunks=sum(result.chunks for result in shard_results),
        embedded_chunks=sum(result.embedded_chunks for result in shard_results),
        incomplete_shards=incomplete,
        issues=tuple(issues[:100]),
    )


def finalize_orchestrated_manifest(
    manifest: CorpusManifest,
    *,
    profile_id: str | None = None,
) -> tuple[str, ManifestReadiness]:
    """Validate every shard, reconcile removals, and record one full sync run."""

    readiness = manifest_readiness(manifest, profile_id=profile_id)
    if not readiness.ready:
        detail = "; ".join(readiness.issues[:10])
        if readiness.incomplete_shards:
            detail += f"; incomplete shards: {list(readiness.incomplete_shards[:20])}"
        raise RuntimeError(f"authoritative FDA manifest is not acceptance-ready: {detail}")

    # Reconcile after readiness on every invocation. A prior successful
    # acceptance record must not prevent a later rerun from retiring a
    # document that became active outside the frozen manifest.
    retired = reconcile_active_manifest(manifest)
    with session_scope() as session:
        existing = (
            session.connection()
            .execute(
                sa_text(
                    "SELECT id FROM fda_corpus_run WHERE manifest_sha256 = :manifest_sha256 "
                    "AND status = 'succeeded' "
                    "AND stats_json->>'complete_universe' = 'true' "
                    "AND stats_json->>'orchestrated' = 'true' "
                    "ORDER BY completed_at DESC LIMIT 1"
                ),
                {"manifest_sha256": manifest.sha256},
            )
            .scalar()
        )
    if existing is not None and retired == 0:
        return str(existing), readiness

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            FdaCorpusRun(
                id=run_id,
                mode="sync",
                status="succeeded",
                manifest_sha256=manifest.sha256,
                started_at=now,
                completed_at=now,
                expected_documents=len(manifest.artifacts),
                discovered_documents=len(manifest.artifacts),
                unchanged_documents=len(manifest.artifacts),
                chunks_written=0,
                stats_json={
                    "errors": [],
                    "complete_universe": True,
                    "orchestrated": True,
                    "embedding_profile": readiness.profile_id,
                    "retired_documents": retired,
                    "shards": FDA_CORPUS_SHARD_COUNT,
                },
            )
        )
    return run_id, readiness


def _shard_readiness_with_connection(
    connection: Any,
    manifest: CorpusManifest,
    shard_id: int,
    *,
    profile_id: str,
) -> ShardReadiness:
    if not 0 <= shard_id < FDA_CORPUS_SHARD_COUNT:
        raise ValueError("shard_id must be between 0 and 511")
    canonical_ids = [
        artifact.canonical_id
        for artifact in manifest.artifacts
        if corpus_shard_id(artifact.canonical_id) == shard_id
    ]
    if not canonical_ids:
        return ShardReadiness(shard_id, 0, 0, 0, 0, 0, ())

    if profile_id == "legacy":
        embedded_expression = "count(c.embedding)"
        profile_join = ""
        parameters: dict[str, object] = {"canonical_ids": canonical_ids}
    else:
        embedded_expression = "count(ce.chunk_id)"
        profile_join = (
            "LEFT JOIN chunk_embedding ce ON ce.chunk_id = c.id " "AND ce.profile_id = :profile_id "
        )
        parameters = {"canonical_ids": canonical_ids, "profile_id": profile_id}
    rows = connection.execute(
        sa_text(
            "SELECT d.canonical_id, d.shard_id, v.id AS version_id, "  # noqa: S608
            "v.chunk_status, v.chunk_count, count(c.id) AS chunks, "
            f"{embedded_expression} AS embedded "
            "FROM fda_document d JOIN chunk c ON c.fda_document_id = d.id "
            "JOIN fda_document_version v ON v.id = c.fda_version_id "
            f"{profile_join}"
            "WHERE d.canonical_id = ANY(CAST(:canonical_ids AS text[])) "
            "GROUP BY d.canonical_id, d.shard_id, v.id, v.chunk_status, v.chunk_count "
            "ORDER BY d.canonical_id"
        ),
        parameters,
    ).mappings()
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_canonical.setdefault(str(row["canonical_id"]), []).append(dict(row))

    issues: list[str] = []
    chunked_documents = 0
    embedded_documents = 0
    chunks = 0
    embedded_chunks = 0
    for canonical_id in canonical_ids:
        matches = by_canonical.get(canonical_id, [])
        if len(matches) != 1:
            issues.append(
                f"shard {shard_id:03d} {canonical_id}: current version count={len(matches)}"
            )
            continue
        row = matches[0]
        actual_chunks = int(row["chunks"])
        actual_embedded = int(row["embedded"])
        expected_chunks = int(row["chunk_count"])
        chunks += actual_chunks
        embedded_chunks += actual_embedded
        chunk_complete = (
            int(row["shard_id"]) == shard_id
            and row["chunk_status"] == "complete"
            and expected_chunks > 0
            and actual_chunks == expected_chunks
        )
        if chunk_complete:
            chunked_documents += 1
        else:
            issues.append(f"shard {shard_id:03d} {canonical_id}: chunk lifecycle incomplete")
        if chunk_complete and actual_embedded == expected_chunks:
            embedded_documents += 1
        else:
            issues.append(f"shard {shard_id:03d} {canonical_id}: embeddings incomplete")
    return ShardReadiness(
        shard_id=shard_id,
        expected_documents=len(canonical_ids),
        chunked_documents=chunked_documents,
        embedded_documents=embedded_documents,
        chunks=chunks,
        embedded_chunks=embedded_chunks,
        issues=tuple(issues[:100]),
    )
