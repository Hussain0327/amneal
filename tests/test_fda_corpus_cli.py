from __future__ import annotations

import pytest

import regwatch.cli as cli
from regwatch.corpus import discovery, embeddings, status
from regwatch.corpus.embeddings import CorpusEmbeddingCounts


def _record_init(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []

    def fake_init_db(*, assert_provider: bool = True) -> None:
        calls.append(assert_provider)

    monkeypatch.setattr(cli, "init_db", fake_init_db)
    return calls


def test_corpus_status_can_diagnose_incomplete_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_init(monkeypatch)

    class FakeCoverage:
        def as_dict(self) -> dict[str, int]:
            return {"pending_chunks": 347}

    monkeypatch.setattr(status, "authoritative_corpus_coverage", FakeCoverage)

    cli.cmd_authoritative_corpus_status()

    assert calls == [False]


def test_corpus_embedding_can_repair_incomplete_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_init(monkeypatch)
    monkeypatch.setattr(embeddings, "embed_pending_corpus", lambda *args, **kwargs: 347)
    monkeypatch.setattr(
        embeddings,
        "corpus_embedding_counts",
        lambda profile_id: CorpusEmbeddingCounts(profile_id, 5841, 5841),
    )

    cli.cmd_authoritative_corpus_embed(profile_id="profile", batch_size=128, limit=0)

    assert calls == [False]


def test_corpus_sync_can_resume_while_vectors_are_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_init(monkeypatch)

    def stop_after_init(*args: object, **kwargs: object) -> None:
        raise RuntimeError("stop after init")

    monkeypatch.setattr(discovery, "discover_authoritative_manifest", stop_after_init)

    with pytest.raises(RuntimeError, match="stop after init"):
        cli.cmd_authoritative_corpus_sync(
            families="",
            application=[],
            limit=0,
            defer_embeddings=True,
            workers=1,
        )

    assert calls == [False]
