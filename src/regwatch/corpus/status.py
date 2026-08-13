"""Exact database-derived coverage for the authoritative FDA corpus."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from config.settings import get_settings
from sqlalchemy import text as sa_text

from regwatch.sources.policy import allowed_source_families
from regwatch.store.db import get_engine


@dataclass(frozen=True)
class CorpusCoverage:
    corpus: str
    embedding_profile: str
    documents: int
    versions: int
    chunks: int
    embedded_chunks: int
    pending_chunks: int
    coverage_percent: float
    complete: bool
    activation_ready: bool
    activation_blockers: tuple[str, ...]
    policy_violations: int
    by_source_family: dict[str, dict[str, int]]
    by_document_type: dict[str, dict[str, int]]
    latest_run: dict[str, Any] | None
    latest_complete_run: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"allowed_sources": list(allowed_source_families())}


def authoritative_corpus_coverage() -> CorpusCoverage:
    """Compute coverage from the rows retrieval can actually serve."""
    profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    with get_engine().connect() as conn:
        documents = int(
            conn.execute(
                sa_text(
                    "SELECT count(DISTINCT fda_document_id) FROM chunk "
                    "WHERE fda_document_id IS NOT NULL"
                )
            ).scalar()
            or 0
        )
        versions = int(
            conn.execute(sa_text("SELECT count(*) FROM fda_document_version")).scalar() or 0
        )
        chunks = int(
            conn.execute(
                sa_text("SELECT count(*) FROM chunk WHERE fda_document_id IS NOT NULL")
            ).scalar()
            or 0
        )
        if profile_id == "legacy":
            embedded = int(
                conn.execute(
                    sa_text("SELECT count(embedding) FROM chunk WHERE fda_document_id IS NOT NULL")
                ).scalar()
                or 0
            )
        else:
            embedded = int(
                conn.execute(
                    sa_text(
                        "SELECT count(*) FROM chunk c JOIN chunk_embedding ce "
                        "ON ce.chunk_id = c.id AND ce.profile_id = :profile_id "
                        "WHERE c.fda_document_id IS NOT NULL"
                    ),
                    {"profile_id": profile_id},
                ).scalar()
                or 0
            )
        violations = int(
            conn.execute(
                sa_text(
                    "SELECT count(*) FROM chunk WHERE fda_document_id IS NOT NULL "
                    "AND (source_family IS NULL OR source_family <> ALL(:allowed))"
                ),
                {"allowed": list(allowed_source_families())},
            ).scalar()
            or 0
        )
        by_family = _group_counts(conn, "source_family")
        by_type = _group_counts(conn, "document_type")
        run_columns = (
            "id, status, manifest_sha256, started_at, completed_at, "
            "expected_documents, discovered_documents, added_documents, "
            "revised_documents, unchanged_documents, error_documents, chunks_written, "
            "stats_json"
        )
        latest = (
            conn.execute(
                sa_text(
                    f"SELECT {run_columns} FROM fda_corpus_run "  # noqa: S608
                    "ORDER BY started_at DESC, id DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
        latest_complete = (
            conn.execute(
                sa_text(
                    f"SELECT {run_columns} FROM fda_corpus_run "  # noqa: S608
                    "WHERE status = 'succeeded' "
                    "AND stats_json->>'complete_universe' = 'true' "
                    "ORDER BY started_at DESC, id DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )

    pending = max(chunks - embedded, 0)
    percent = round(100.0 * embedded / chunks, 2) if chunks else 0.0
    complete = chunks > 0 and pending == 0 and violations == 0
    activation_blockers = _activation_blockers(
        complete=complete,
        documents=documents,
        by_family=by_family,
        complete_run=dict(latest_complete) if latest_complete is not None else None,
    )
    return CorpusCoverage(
        corpus="authoritative_fda",
        embedding_profile=profile_id,
        documents=documents,
        versions=versions,
        chunks=chunks,
        embedded_chunks=embedded,
        pending_chunks=pending,
        coverage_percent=percent,
        complete=complete,
        activation_ready=not activation_blockers,
        activation_blockers=tuple(activation_blockers),
        policy_violations=violations,
        by_source_family=by_family,
        by_document_type=by_type,
        latest_run=dict(latest) if latest is not None else None,
        latest_complete_run=(dict(latest_complete) if latest_complete is not None else None),
    )


def assert_authoritative_corpus_ready_for_activation() -> CorpusCoverage:
    """Fail closed when the serving namespace is incomplete or unverified."""

    coverage = authoritative_corpus_coverage()
    if not coverage.activation_ready:
        detail = "; ".join(coverage.activation_blockers)
        raise RuntimeError(f"authoritative FDA corpus is not activation-ready: {detail}")
    return coverage


def _activation_blockers(
    *,
    complete: bool,
    documents: int,
    by_family: dict[str, dict[str, int]],
    complete_run: dict[str, Any] | None,
) -> list[str]:
    blockers: list[str] = []
    if not complete:
        blockers.append("chunk embedding coverage or source-policy compliance is incomplete")
    missing = [
        family
        for family in allowed_source_families()
        if by_family.get(family, {}).get("documents", 0) <= 0
    ]
    if missing:
        blockers.append("missing source families: " + ", ".join(missing))
    if complete_run is None:
        blockers.append("no successful complete-universe sync is recorded")
        return blockers
    expected = int(complete_run.get("expected_documents") or 0)
    discovered = int(complete_run.get("discovered_documents") or 0)
    processed = sum(
        int(complete_run.get(key) or 0)
        for key in ("added_documents", "revised_documents", "unchanged_documents")
    )
    if expected <= 0 or expected != discovered or processed != expected:
        blockers.append("latest complete-universe run did not process its full manifest")
    if int(complete_run.get("error_documents") or 0) != 0:
        blockers.append("latest complete-universe run recorded document errors")
    if documents != discovered:
        blockers.append(
            f"searchable document count {documents} does not match full manifest {discovered}"
        )
    return blockers


def _group_counts(conn: Any, column: str) -> dict[str, dict[str, int]]:
    if column not in {"source_family", "document_type"}:
        raise ValueError(f"unsupported coverage group: {column}")
    rows = conn.execute(
        sa_text(
            f"SELECT {column} AS key, count(DISTINCT fda_document_id) AS documents, "  # noqa: S608
            "count(*) AS chunks FROM chunk WHERE fda_document_id IS NOT NULL "
            f"GROUP BY {column} ORDER BY {column}"
        )
    ).mappings()
    return {
        str(row["key"] or "<missing>"): {
            "documents": int(row["documents"]),
            "chunks": int(row["chunks"]),
        }
        for row in rows
    }
