"""Durable lifecycle transitions shared by sync and embedding workers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text as sa_text
from sqlmodel import Session, select

from regwatch.store.db import session_scope
from regwatch.store.models import FdaDocumentVersion, FdaVersionEmbeddingState


def embedded_count_in_session(session: Session, version_id: int, profile_id: str) -> int:
    params: dict[str, object]
    if profile_id == "legacy":
        sql = "SELECT count(embedding) FROM chunk WHERE fda_version_id = :version_id"
        params = {"version_id": version_id}
    else:
        sql = (
            "SELECT count(ce.chunk_id) FROM chunk c JOIN chunk_embedding ce "
            "ON ce.chunk_id = c.id AND ce.profile_id = :profile_id "
            "WHERE c.fda_version_id = :version_id"
        )
        params = {"version_id": version_id, "profile_id": profile_id}
    return int(session.connection().execute(sa_text(sql), params).scalar() or 0)


def embedding_complete_in_session(
    session: Session,
    version: FdaDocumentVersion,
    profile_id: str,
) -> bool:
    if version.id is None or version.chunk_count <= 0:
        return False
    return embedded_count_in_session(session, version.id, profile_id) == version.chunk_count


def upsert_embedding_state(
    session: Session,
    version_id: int,
    profile_id: str,
    *,
    expected_chunks: int,
    embedded_chunks: int,
    status: str,
    last_error: str | None = None,
) -> None:
    state = session.exec(
        select(FdaVersionEmbeddingState).where(
            FdaVersionEmbeddingState.fda_version_id == version_id,
            FdaVersionEmbeddingState.profile_id == profile_id,
        )
    ).first()
    now = datetime.now(UTC)
    if state is None:
        state = FdaVersionEmbeddingState(
            fda_version_id=version_id,
            profile_id=profile_id,
        )
    state.expected_chunks = expected_chunks
    state.embedded_chunks = embedded_chunks
    state.status = status
    state.last_error = last_error
    state.updated_at = now
    if status in {"running", "complete", "failed"} and state.started_at is None:
        state.started_at = now
    state.completed_at = now if status == "complete" else None
    session.add(state)


def refresh_embedding_state(version_id: int, profile_id: str) -> None:
    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None:
            raise RuntimeError(f"FDA version vanished during embedding: {version_id}")
        embedded = embedded_count_in_session(session, version_id, profile_id)
        complete = version.chunk_count > 0 and embedded == version.chunk_count
        upsert_embedding_state(
            session,
            version_id,
            profile_id,
            expected_chunks=version.chunk_count,
            embedded_chunks=embedded,
            status="complete" if complete else "running",
        )


def version_ids_for_chunks(chunk_ids: list[str]) -> list[int]:
    if not chunk_ids:
        return []
    with session_scope() as session:
        return [
            int(value)
            for value in session.connection()
            .execute(
                sa_text(
                    "SELECT DISTINCT fda_version_id FROM chunk "
                    "WHERE id = ANY(CAST(:chunk_ids AS text[])) "
                    "AND fda_version_id IS NOT NULL"
                ),
                {"chunk_ids": chunk_ids},
            )
            .scalars()
        ]


def refresh_embedding_states_for_chunks(profile_id: str, chunk_ids: list[str]) -> None:
    """Refresh only versions touched by one durable embedding batch."""

    version_ids = version_ids_for_chunks(chunk_ids)
    for version_id in version_ids:
        refresh_embedding_state(version_id, profile_id)


def mark_embedding_failed(version_id: int, profile_id: str, exc: Exception) -> None:
    with session_scope() as session:
        version = session.get(FdaDocumentVersion, version_id)
        if version is None:
            return
        embedded = embedded_count_in_session(session, version_id, profile_id)
        upsert_embedding_state(
            session,
            version_id,
            profile_id,
            expected_chunks=version.chunk_count,
            embedded_chunks=embedded,
            status="failed",
            last_error=f"{type(exc).__name__}: {exc}"[:2_000],
        )
