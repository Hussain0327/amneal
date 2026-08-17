"""Fail-closed shard and full-manifest acceptance for the FDA corpus."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config.settings import get_settings
from sqlalchemy import text as sa_text

from regwatch.corpus.manifest import CorpusManifest
from regwatch.corpus.resolution import (
    TERMINAL_RESOLUTION_STATUSES,
    ResolutionStatus,
    terminal_evidence_issues,
)
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
    terminal_documents: int
    missing_at_source_documents: int
    unparseable_documents: int
    embedded_documents: int
    chunks: int
    embedded_chunks: int
    issues: tuple[str, ...]

    @property
    def resolved_documents(self) -> int:
        return self.chunked_documents + self.terminal_documents

    @property
    def ready(self) -> bool:
        return (
            not self.issues
            and self.resolved_documents == self.expected_documents
            and self.embedded_documents == self.chunked_documents
        )


@dataclass(frozen=True)
class ManifestReadiness:
    manifest_sha256: str
    profile_id: str
    expected_documents: int
    chunked_documents: int
    terminal_documents: int
    missing_at_source_documents: int
    unparseable_documents: int
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
        terminal_documents=sum(result.terminal_documents for result in shard_results),
        missing_at_source_documents=sum(
            result.missing_at_source_documents for result in shard_results
        ),
        unparseable_documents=sum(result.unparseable_documents for result in shard_results),
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
                    "SELECT id, unchanged_documents, terminal_documents, stats_json "
                    "FROM fda_corpus_run WHERE manifest_sha256 = :manifest_sha256 "
                    "AND status = 'succeeded' "
                    "AND stats_json->>'complete_universe' = 'true' "
                    "AND stats_json->>'orchestrated' = 'true' "
                    "ORDER BY completed_at DESC LIMIT 1"
                ),
                {"manifest_sha256": manifest.sha256},
            )
            .mappings()
            .first()
        )
    if existing is not None and retired == 0:
        existing_stats = existing["stats_json"] or {}
        same_resolution = (
            int(existing["unchanged_documents"] or 0) == readiness.chunked_documents
            and int(existing["terminal_documents"] or 0) == readiness.terminal_documents
            and str(existing_stats.get("embedding_profile") or "") == readiness.profile_id
            and int(existing_stats.get("chunks") or 0) == readiness.chunks
            and int(existing_stats.get("embedded_chunks") or 0) == readiness.embedded_chunks
        )
        if same_resolution:
            return str(existing["id"]), readiness

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
                unchanged_documents=readiness.chunked_documents,
                terminal_documents=readiness.terminal_documents,
                chunks_written=0,
                stats_json={
                    "errors": [],
                    "complete_universe": True,
                    "orchestrated": True,
                    "embedding_profile": readiness.profile_id,
                    "indexed_documents": readiness.chunked_documents,
                    "chunks": readiness.chunks,
                    "embedded_chunks": readiness.embedded_chunks,
                    "retired_documents": retired,
                    "shards": FDA_CORPUS_SHARD_COUNT,
                    "terminal_documents": readiness.terminal_documents,
                    "terminal_by_status": {
                        ResolutionStatus.MISSING_AT_SOURCE.value: (
                            readiness.missing_at_source_documents
                        ),
                        ResolutionStatus.UNPARSEABLE.value: (readiness.unparseable_documents),
                    },
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
    selected_artifacts = [
        artifact
        for artifact in manifest.artifacts
        if corpus_shard_id(artifact.canonical_id) == shard_id
    ]
    canonical_ids = [artifact.canonical_id for artifact in selected_artifacts]
    artifact_by_canonical = {artifact.canonical_id: artifact for artifact in selected_artifacts}
    if not canonical_ids:
        return ShardReadiness(
            shard_id=shard_id,
            expected_documents=0,
            chunked_documents=0,
            terminal_documents=0,
            missing_at_source_documents=0,
            unparseable_documents=0,
            embedded_documents=0,
            chunks=0,
            embedded_chunks=0,
            issues=(),
        )

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
            "v.is_current, v.content_hash_kind, v.chunk_status, v.chunk_count, "
            "v.resolution_status, v.resolution_attempts, v.resolved_at, "
            "v.resolution_error, v.resolution_evidence_json, "
            "v.artifact_retained, v.artifact_uri, count(c.id) AS chunks, "
            f"{embedded_expression} AS embedded "
            "FROM fda_document d LEFT JOIN fda_document_version v "
            "ON v.fda_document_id = d.id AND v.is_current "
            "LEFT JOIN chunk c ON c.fda_version_id = v.id "
            f"{profile_join}"
            "WHERE d.canonical_id = ANY(CAST(:canonical_ids AS text[])) "
            "GROUP BY d.canonical_id, d.shard_id, v.id "
            "ORDER BY d.canonical_id"
        ),
        parameters,
    ).mappings()
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_canonical.setdefault(str(row["canonical_id"]), []).append(dict(row))

    issues: list[str] = []
    chunked_documents = 0
    terminal_documents = 0
    missing_at_source_documents = 0
    unparseable_documents = 0
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
        if row["version_id"] is None or not bool(row["is_current"]):
            issues.append(f"shard {shard_id:03d} {canonical_id}: current version missing")
            continue
        actual_chunks = int(row["chunks"])
        actual_embedded = int(row["embedded"])
        expected_chunks = int(row["chunk_count"])
        chunks += actual_chunks
        embedded_chunks += actual_embedded
        resolution_status = str(row["resolution_status"] or "")
        shard_matches = row["shard_id"] is not None and int(row["shard_id"]) == shard_id
        if resolution_status in TERMINAL_RESOLUTION_STATUSES:
            terminal_issues = list(
                terminal_evidence_issues(
                    row,
                    artifact_by_canonical[canonical_id],
                    manifest_sha256=manifest.sha256,
                    minimum_attempts=get_settings().fda_corpus_terminal_attempts,
                )
            )
            if not shard_matches:
                terminal_issues.append("terminal document has incorrect shard ownership")
            if row["chunk_status"] != "failed" or expected_chunks != 0:
                terminal_issues.append("terminal document has an invalid chunk lifecycle")
            if actual_chunks != 0 or actual_embedded != 0:
                terminal_issues.append("terminal document still has searchable rows or vectors")
            if terminal_issues:
                issues.extend(
                    f"shard {shard_id:03d} {canonical_id}: {issue}" for issue in terminal_issues
                )
                continue
            terminal_documents += 1
            if resolution_status == ResolutionStatus.MISSING_AT_SOURCE.value:
                missing_at_source_documents += 1
            else:
                unparseable_documents += 1
            continue
        chunk_complete = (
            shard_matches
            and resolution_status == ResolutionStatus.INDEXED.value
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
        terminal_documents=terminal_documents,
        missing_at_source_documents=missing_at_source_documents,
        unparseable_documents=unparseable_documents,
        embedded_documents=embedded_documents,
        chunks=chunks,
        embedded_chunks=embedded_chunks,
        issues=tuple(issues[:100]),
    )
