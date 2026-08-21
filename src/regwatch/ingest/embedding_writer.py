"""Embedding preparation and transaction-safe profile writes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config.settings import get_settings
from sqlmodel import Session

from regwatch.common.logging import get_logger
from regwatch.process.embedder import (
    embed_documents,
    get_embedding_provider,
    get_embedding_provider_for_profile,
)
from regwatch.store.embedding_profiles import content_hash as embedding_content_hash
from regwatch.store.vector_store import get_embedding_profile, upsert_profile_embeddings

log = get_logger(__name__)


@dataclass(frozen=True)
class ProfileEmbeddingBatch:
    profile_id: str
    embeddings: list[list[float]]
    content_hashes: list[str]
    required: bool


def legacy_document_embeddings(texts: list[str]) -> Sequence[list[float] | None]:
    """Embed the legacy space only while it is the active retrieval arm."""
    if not texts:
        return []
    provider = get_embedding_provider()
    active_profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    if active_profile_id != "legacy":
        return [None] * len(texts)
    return embed_documents(provider, texts)


def profile_document_embeddings(texts: list[str]) -> list[ProfileEmbeddingBatch]:
    """Precompute active/shadow profile vectors before opening a transaction."""
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
    batches: list[ProfileEmbeddingBatch] = []
    for profile_id, required in targets:
        try:
            profile = get_embedding_profile(profile_id)
            provider = get_embedding_provider_for_profile(profile)
            embeddings = embed_documents(provider, texts)
        except Exception as exc:
            if required:
                raise
            log.warning(
                "shadow_embedding_skipped",
                profile_id=profile_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        batches.append(
            ProfileEmbeddingBatch(
                profile_id=profile_id,
                embeddings=embeddings,
                content_hashes=list(hashes),
                required=required,
            )
        )
    return batches


def write_profile_batches(
    session: Session,
    chunk_ids: list[str],
    batches: list[ProfileEmbeddingBatch],
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
