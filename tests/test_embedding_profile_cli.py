"""Offline operator-contract tests for embedding profile backfill."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from regwatch.cli import app
from regwatch.store.embedding_profiles import PendingProfileChunk, ProfileEmbeddingCoverage

runner = CliRunner()
PROFILE_ID = "ep_" + ("b" * 32)


def test_backfill_batches_raw_text_and_preserves_source_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from regwatch import cli as cli_module
    from regwatch.process import embedder as embedder_module
    from regwatch.store import vector_store

    pages = [
        [
            PendingProfileChunk("chunk-a", " raw A ", "a" * 64),
            PendingProfileChunk("chunk-b", "raw\nB", "b" * 64),
        ],
        [PendingProfileChunk("chunk-c", "raw C", "c" * 64)],
        [],
    ]
    seen: dict[str, Any] = {"documents": [], "writes": []}
    profile = SimpleNamespace(profile_id=PROFILE_ID, dimension=32)
    provider = SimpleNamespace(dim=32)

    monkeypatch.setattr(cli_module, "init_db", lambda: None)
    monkeypatch.setattr(vector_store, "get_embedding_profile", lambda _profile_id: profile)
    monkeypatch.setattr(
        embedder_module,
        "get_embedding_provider_for_profile",
        lambda _profile: provider,
    )

    def fake_pending(
        profile_id: str,
        *,
        limit: int,
        after_chunk_id: str | None = None,
    ) -> list[PendingProfileChunk]:
        assert profile_id == PROFILE_ID
        assert limit == 2
        assert after_chunk_id is None
        return pages.pop(0)

    def fake_embed(_provider: object, texts: list[str]) -> list[list[float]]:
        seen["documents"].append(list(texts))
        return [[1.0, *([0.0] * 31)] for _ in texts]

    def fake_upsert(
        profile_id: str,
        chunk_ids: list[str],
        embeddings: list[list[float]],
        content_hashes: list[str],
        *,
        conn: object | None = None,
    ) -> None:
        assert conn is None
        seen["writes"].append((profile_id, list(chunk_ids), list(content_hashes), len(embeddings)))

    monkeypatch.setattr(vector_store, "pending_profile_chunks", fake_pending)
    monkeypatch.setattr(embedder_module, "embed_documents", fake_embed)
    monkeypatch.setattr(vector_store, "upsert_profile_embeddings", fake_upsert)
    monkeypatch.setattr(
        vector_store,
        "profile_embedding_coverage",
        lambda _profile_id: ProfileEmbeddingCoverage(
            profile_id=PROFILE_ID,
            total_chunks=3,
            embedded_chunks=3,
        ),
    )

    result = runner.invoke(
        app,
        [
            "embedding-profile-backfill",
            PROFILE_ID,
            "--batch-size",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["documents"] == [[" raw A ", "raw\nB"], ["raw C"]]
    assert seen["writes"] == [
        (PROFILE_ID, ["chunk-a", "chunk-b"], ["a" * 64, "b" * 64], 2),
        (PROFILE_ID, ["chunk-c"], ["c" * 64], 1),
    ]


def test_backfill_rejects_profile_config_mismatch_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from regwatch import cli as cli_module
    from regwatch.process import embedder as embedder_module
    from regwatch.store import vector_store

    monkeypatch.setattr(cli_module, "init_db", lambda: None)
    monkeypatch.setattr(
        vector_store,
        "get_embedding_profile",
        lambda _profile_id: SimpleNamespace(profile_id=PROFILE_ID, dimension=2560),
    )
    monkeypatch.setattr(
        embedder_module,
        "get_embedding_provider_for_profile",
        lambda _profile: (_ for _ in ()).throw(
            RuntimeError("configured Qwen endpoint does not match embedding profile")
        ),
    )
    monkeypatch.setattr(
        embedder_module,
        "embed_documents",
        lambda *_args, **_kwargs: pytest.fail("embedding must not start"),
    )

    result = runner.invoke(app, ["embedding-profile-backfill", PROFILE_ID])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "does not match embedding profile" in str(result.exception)
