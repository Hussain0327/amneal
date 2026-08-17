from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from sqlalchemy import text

from regwatch.corpus import sync as sync_module
from regwatch.corpus.acceptance import (
    finalize_orchestrated_manifest,
    manifest_readiness,
    shard_readiness,
)
from regwatch.corpus.artifact_store import FilesystemArtifactStore
from regwatch.corpus.embeddings import (
    corpus_embedding_counts,
    pending_corpus_chunks,
    write_corpus_embeddings,
)
from regwatch.corpus.manifest import CorpusArtifact, CorpusManifest
from regwatch.corpus.sharding import corpus_shard_id
from regwatch.corpus.status import authoritative_corpus_coverage
from regwatch.corpus.sync import ArtifactPayload, parse_artifact, sync_manifest
from regwatch.process.embedder import embed_documents, get_embedding_provider
from regwatch.sources.policy import FdaDocumentType, FdaSourceFamily
from regwatch.store.db import get_engine


def _offline_client() -> httpx.Client:
    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"inline artifact unexpectedly requested {request.url}")

    return httpx.Client(transport=httpx.MockTransport(no_network))


def _inline_artifact(
    canonical_id: str,
    *,
    family: FdaSourceFamily = FdaSourceFamily.ORANGE_BOOK,
    document_type: FdaDocumentType = FdaDocumentType.ORANGE_BOOK_PRODUCT,
    text_value: str = "FDA authoritative record with enough citable text for indexing.",
) -> CorpusArtifact:
    urls = {
        FdaSourceFamily.DRUGS_AT_FDA: (
            "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?ApplNo=020503"
        ),
        FdaSourceFamily.ACTION_PACKAGE: (
            "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/review.pdf"
        ),
        FdaSourceFamily.PSG: ("https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf"),
        FdaSourceFamily.FDA_BE_GUIDANCE: "https://www.fda.gov/media/165049/download",
        FdaSourceFamily.ORANGE_BOOK: (
            "https://www.accessdata.fda.gov/scripts/cder/ob/results_product.cfm"
        ),
    }
    return CorpusArtifact(
        canonical_id=canonical_id,
        source_family=family,
        document_type=document_type,
        title=f"Test artifact {canonical_id}",
        source_url=urls[family],
        application_number="NDA020503",
        active_ingredient="ALBUTEROL SULFATE",
        normalized_name="albuterol sulfate",
        inline_text=text_value,
    )


def _manifest(*artifacts: CorpusArtifact, complete: bool = False) -> CorpusManifest:
    return CorpusManifest(
        artifacts=tuple(artifacts),
        source_snapshots={"fixture": "sha256:test"},
        complete_universe=complete,
    )


def _counts() -> tuple[int, int, int]:
    with get_engine().connect() as conn:
        return tuple(
            int(value)
            for value in conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM fda_document), "
                    "(SELECT count(*) FROM fda_document_version), "
                    "(SELECT count(*) FROM chunk WHERE fda_document_id IS NOT NULL)"
                )
            ).one()
        )  # type: ignore[return-value]


def test_inline_sync_is_idempotent_and_revision_safe() -> None:
    original = _inline_artifact("orange-book:product:n:020503")
    with _offline_client() as client:
        first = sync_manifest(_manifest(original), client=client)
        second = sync_manifest(_manifest(original), client=client)

    assert first.added_documents == 1
    assert first.error_documents == 0
    assert first.chunks_written > 0
    assert second.unchanged_documents == 1
    assert second.chunks_written == 0
    assert _counts() == (1, 1, first.chunks_written)

    revised = _inline_artifact(
        original.canonical_id,
        text_value="A revised authoritative FDA record with materially different citable text.",
    )
    with _offline_client() as client:
        third = sync_manifest(_manifest(revised), client=client)
    assert third.revised_documents == 1
    documents, versions, chunks = _counts()
    assert (documents, versions) == (1, 2)
    assert chunks == third.chunks_written
    with get_engine().connect() as conn:
        served = conn.execute(
            text("SELECT text FROM chunk WHERE fda_document_id IS NOT NULL")
        ).scalar_one()
    assert "revised authoritative" in served.lower()


def test_deferred_sync_is_resumable_and_embeddings_checkpoint_in_batches() -> None:
    artifact = _inline_artifact("orange-book:product:n:020503")
    with _offline_client() as client:
        first = sync_manifest(_manifest(artifact), defer_embeddings=True, client=client)
        second = sync_manifest(_manifest(artifact), defer_embeddings=True, client=client)

    assert first.added_documents == 1
    assert first.embeddings_deferred is True
    assert second.unchanged_documents == 1
    assert second.chunks_written == 0
    before = corpus_embedding_counts("legacy")
    assert before.chunks == first.chunks_written
    assert before.embedded_chunks == 0
    with get_engine().connect() as conn:
        lifecycle = conn.execute(
            text(
                "SELECT v.chunk_status, s.status, s.expected_chunks, s.embedded_chunks "
                "FROM fda_document_version v JOIN fda_version_embedding_state s "
                "ON s.fda_version_id = v.id AND s.profile_id = 'legacy'"
            )
        ).one()
    assert tuple(lifecycle) == ("complete", "pending", first.chunks_written, 0)

    pending = pending_corpus_chunks("legacy", limit=128)
    vectors = embed_documents(get_embedding_provider(), [chunk.text for chunk in pending])
    write_corpus_embeddings("legacy", pending, vectors)

    after = corpus_embedding_counts("legacy")
    assert after.embedded_chunks == after.chunks
    assert after.pending_chunks == 0
    assert pending_corpus_chunks("legacy", limit=128) == []
    with get_engine().connect() as conn:
        state = conn.execute(
            text(
                "SELECT status, embedded_chunks, completed_at IS NOT NULL "
                "FROM fda_version_embedding_state WHERE profile_id = 'legacy'"
            )
        ).one()
    assert tuple(state) == ("complete", after.chunks, True)


def test_parse_failure_retains_raw_artifact_and_unlinks_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module

    staging = tmp_path / "staging"
    durable = tmp_path / "durable"
    monkeypatch.setenv("FDA_CORPUS_TEMP_DIR", str(staging))
    settings_module.get_settings.cache_clear()
    artifact = CorpusArtifact(
        canonical_id="drugs-at-fda:application-doc:bad-pdf",
        source_family=FdaSourceFamily.ACTION_PACKAGE,
        document_type=FdaDocumentType.CLINICAL_REVIEW,
        title="FDA malformed PDF fixture",
        source_url="https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/review.pdf",
    )
    body = b"%PDF-not-a-valid-document"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/pdf"},
            request=request,
        )

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            stats = sync_manifest(
                _manifest(artifact),
                defer_embeddings=True,
                client=client,
                artifact_store=FilesystemArtifactStore(durable),
            )
    finally:
        settings_module.get_settings.cache_clear()

    assert stats.error_documents == 1
    assert list(staging.iterdir()) == []
    retained = list(durable.rglob("*.pdf"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == body
    with get_engine().connect() as conn:
        version = conn.execute(
            text(
                "SELECT chunk_status, artifact_retained, artifact_uri " "FROM fda_document_version"
            )
        ).one()
    assert version[0] == "failed"
    assert version[1] is True
    assert str(version[2]).startswith("file://")


def test_failed_chunk_publish_preserves_acquisition_without_partial_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _inline_artifact("orange-book:product:n:099999")

    def fail_chunk_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic chunk write failure")

    monkeypatch.setattr(sync_module, "add_chunks", fail_chunk_write)
    with _offline_client() as client:
        stats = sync_manifest(_manifest(artifact), client=client)

    assert stats.error_documents == 1
    assert stats.succeeded is False
    assert _counts() == (1, 1, 0)
    with get_engine().connect() as conn:
        run = conn.execute(
            text("SELECT status, error_documents FROM fda_corpus_run WHERE id = :id"),
            {"id": stats.run_id},
        ).one()
        version = conn.execute(
            text("SELECT chunk_status, chunk_error FROM fda_document_version")
        ).one()
    assert tuple(run) == ("failed", 1)
    assert version[0] == "failed"
    assert "synthetic chunk write failure" in str(version[1])


def test_sync_uses_bounded_parallel_document_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tuple(_inline_artifact(f"orange-book:product:n:02050{index}") for index in range(3))
    rendezvous = Barrier(3, timeout=2)

    def synchronized_sync(
        artifact: CorpusArtifact,
        *,
        client: httpx.Client,
        defer_embeddings: bool,
        artifact_store: object,
    ) -> tuple[str, int]:
        del artifact, client, defer_embeddings, artifact_store
        rendezvous.wait()
        return "added", 1

    monkeypatch.setattr(sync_module, "sync_artifact", synchronized_sync)
    with _offline_client() as client:
        stats = sync_manifest(
            _manifest(*artifacts),
            defer_embeddings=True,
            workers=3,
            client=client,
        )
    assert stats.added_documents == 3
    assert stats.chunks_written == 3
    assert stats.workers == 3


def test_successful_complete_manifest_retires_missing_documents() -> None:
    keep = _inline_artifact("orange-book:product:n:020503")
    retire = _inline_artifact("orange-book:product:n:099999")
    with _offline_client() as client:
        initial = sync_manifest(_manifest(keep, retire), client=client)
        final = sync_manifest(_manifest(keep, complete=True), client=client)

    assert initial.added_documents == 2
    assert final.unchanged_documents == 1
    assert final.retired_documents == 1
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT canonical_id, is_active FROM fda_document ORDER BY canonical_id")
        ).all()
        searchable = conn.execute(
            text("SELECT count(*) FROM chunk WHERE fda_document_id IS NOT NULL")
        ).scalar_one()
    assert [(row[0], row[1]) for row in rows] == [
        (keep.canonical_id, True),
        (retire.canonical_id, False),
    ]
    assert searchable > 0


def test_activation_requires_all_families_and_a_full_covered_run() -> None:
    artifacts = (
        _inline_artifact(
            "drugs-at-fda:application:nda020503",
            family=FdaSourceFamily.DRUGS_AT_FDA,
            document_type=FdaDocumentType.APPLICATION_METADATA,
        ),
        _inline_artifact(
            "drugs-at-fda:application-doc:review-1",
            family=FdaSourceFamily.ACTION_PACKAGE,
            document_type=FdaDocumentType.CLINICAL_REVIEW,
        ),
        _inline_artifact(
            "psg:020503",
            family=FdaSourceFamily.PSG,
            document_type=FdaDocumentType.PRODUCT_SPECIFIC_GUIDANCE,
        ),
        _inline_artifact(
            "fda-be-guidance:test",
            family=FdaSourceFamily.FDA_BE_GUIDANCE,
            document_type=FdaDocumentType.BIOEQUIVALENCE_GUIDANCE,
        ),
        _inline_artifact("orange-book:product:n:020503"),
    )
    with _offline_client() as client:
        stats = sync_manifest(_manifest(*artifacts, complete=True), client=client)
    assert stats.succeeded is True

    coverage = authoritative_corpus_coverage()
    assert coverage.documents == len(artifacts)
    assert coverage.chunks == coverage.embedded_chunks
    assert coverage.pending_chunks == 0
    assert coverage.coverage_percent == 100.0
    assert coverage.policy_violations == 0
    assert coverage.activation_ready is True
    assert coverage.activation_blockers == ()


def test_sharded_sync_embedding_and_acceptance_form_one_full_run() -> None:
    artifacts = (
        _inline_artifact(
            "drugs-at-fda:application:nda020503",
            family=FdaSourceFamily.DRUGS_AT_FDA,
            document_type=FdaDocumentType.APPLICATION_METADATA,
        ),
        _inline_artifact(
            "drugs-at-fda:application-doc:review-1",
            family=FdaSourceFamily.ACTION_PACKAGE,
            document_type=FdaDocumentType.CLINICAL_REVIEW,
        ),
        _inline_artifact(
            "psg:020503",
            family=FdaSourceFamily.PSG,
            document_type=FdaDocumentType.PRODUCT_SPECIFIC_GUIDANCE,
        ),
        _inline_artifact(
            "fda-be-guidance:test",
            family=FdaSourceFamily.FDA_BE_GUIDANCE,
            document_type=FdaDocumentType.BIOEQUIVALENCE_GUIDANCE,
        ),
        _inline_artifact("orange-book:product:n:020503"),
    )
    manifest = _manifest(*artifacts, complete=True)
    shard_ids = sorted({corpus_shard_id(artifact.canonical_id) for artifact in artifacts})
    with _offline_client() as client:
        for shard_id in shard_ids:
            result = sync_manifest(
                manifest,
                shard_id=shard_id,
                defer_embeddings=True,
                client=client,
            )
            assert result.succeeded is True
    before = manifest_readiness(manifest, profile_id="legacy")
    assert before.chunked_documents == len(artifacts)
    assert before.embedded_documents == 0
    one_shard = shard_readiness(manifest, shard_ids[0], profile_id="legacy")
    assert one_shard.chunked_documents == one_shard.expected_documents
    assert one_shard.ready is False

    pending = pending_corpus_chunks("legacy", limit=128)
    vectors = embed_documents(get_embedding_provider(), [chunk.text for chunk in pending])
    write_corpus_embeddings("legacy", pending, vectors)
    run_id, readiness = finalize_orchestrated_manifest(manifest, profile_id="legacy")

    assert readiness.ready is True
    assert readiness.embedded_documents == len(artifacts)
    with get_engine().connect() as conn:
        run = conn.execute(
            text(
                "SELECT status, expected_documents, discovered_documents, "
                "stats_json->>'orchestrated' FROM fda_corpus_run WHERE id = :id"
            ),
            {"id": run_id},
        ).one()
    assert tuple(run) == ("succeeded", len(artifacts), len(artifacts), "true")


def test_html_pages_are_reduced_to_visible_main_content(tmp_path: Path) -> None:
    content = (
        b"<!doctype html><html><body><header>global navigation</header>"
        b"<main><h1>FDA regulatory action</h1><p>Authoritative safety text.</p>"
        b"<script>do_not_index()</script></main><footer>footer links</footer>"
        b"</body></html>"
    )
    path = tmp_path / "artifact.html"
    path.write_bytes(content)
    payload = ArtifactPayload(
        source_url="https://www.fda.gov/drugs/drugsafety/example",
        path=path,
        content_hash="a" * 64,
        byte_size=len(content),
        mime_type="text/html; charset=utf-8",
        fetched_at=datetime.now(UTC),
    )
    parsed = parse_artifact(payload)
    assert parsed.engine == "selectolax-main-content"
    assert parsed.pages == ["FDA regulatory action\nAuthoritative safety text."]
    assert "navigation" not in parsed.text
    assert "do_not_index" not in parsed.text


# ---------------------------------------------------------------------------
# Blast-radius guards + write-config preflight (2026-08-14 postmortem)
# ---------------------------------------------------------------------------


def _stub_sync_artifact(monkeypatch: pytest.MonkeyPatch, fail_ids: set[str]) -> list[str]:
    """Replace per-document work with a deterministic pass/fail stub."""
    calls: list[str] = []

    def stub(
        artifact: CorpusArtifact,
        *,
        client: httpx.Client,
        defer_embeddings: bool = False,
        artifact_store: object = None,
    ) -> tuple[str, int]:
        calls.append(artifact.canonical_id)
        if artifact.canonical_id in fail_ids:
            raise RuntimeError("simulated systemic document failure")
        return "added", 1

    monkeypatch.setattr(sync_module, "sync_artifact", stub)
    return calls


def _guard_env(monkeypatch: pytest.MonkeyPatch, *, canary: int, consecutive: int) -> None:
    import config.settings as cs

    monkeypatch.setenv("FDA_CORPUS_CANARY_DOCUMENTS", str(canary))
    monkeypatch.setenv("FDA_CORPUS_MAX_CONSECUTIVE_FAILURES", str(consecutive))
    cs.get_settings.cache_clear()


def _run_row(run_id: str) -> dict:
    with get_engine().connect() as conn:
        row = (
            conn.execute(
                text("SELECT status, stats_json FROM fda_corpus_run WHERE id = :id"),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_canary_batch_all_failing_aborts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """295 documents must never again pay fetch/parse/OCR for one shared bug."""
    _guard_env(monkeypatch, canary=5, consecutive=10)
    artifacts = [_inline_artifact(f"orange-book:product:fail:{i:03d}") for i in range(20)]
    calls = _stub_sync_artifact(monkeypatch, {a.canonical_id for a in artifacts})
    with _offline_client() as client:
        stats = sync_manifest(_manifest(*artifacts), client=client, workers=1)

    assert stats.aborted_reason is not None and "canary" in stats.aborted_reason
    assert stats.error_documents == 5
    assert not stats.succeeded
    # Blast radius: the queue depth (workers * 4) bounds extra submissions.
    assert len(calls) < len(artifacts)
    row = _run_row(stats.run_id)
    assert row["status"] == "failed"
    assert row["stats_json"]["aborted_reason"] == stats.aborted_reason


def test_isolated_failures_do_not_trip_either_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _guard_env(monkeypatch, canary=5, consecutive=3)
    artifacts = [_inline_artifact(f"orange-book:product:iso:{i:03d}") for i in range(10)]
    _stub_sync_artifact(monkeypatch, {artifacts[2].canonical_id, artifacts[6].canonical_id})
    with _offline_client() as client:
        stats = sync_manifest(_manifest(*artifacts), client=client, workers=1)

    assert stats.aborted_reason is None
    assert stats.error_documents == 2
    assert stats.added_documents == 8
    assert _run_row(stats.run_id)["status"] == "failed"  # errors still fail the run


def test_consecutive_failures_abort_a_run_that_breaks_mid_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_env(monkeypatch, canary=2, consecutive=3)
    # The manifest sorts artifacts by canonical_id, so the ids must sort the
    # successes FIRST for the canary window to pass before the failures start.
    ok = [_inline_artifact(f"orange-book:product:a-ok:{i:03d}") for i in range(2)]
    bad = [_inline_artifact(f"orange-book:product:z-bad:{i:03d}") for i in range(10)]
    _stub_sync_artifact(monkeypatch, {a.canonical_id for a in bad})
    with _offline_client() as client:
        stats = sync_manifest(_manifest(*ok, *bad), client=client, workers=1)

    assert stats.aborted_reason is not None and "consecutive" in stats.aborted_reason
    assert stats.added_documents == 2
    assert stats.error_documents == 3
    assert _run_row(stats.run_id)["stats_json"]["aborted_reason"] == stats.aborted_reason


def test_zero_disables_both_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    _guard_env(monkeypatch, canary=0, consecutive=0)
    artifacts = [_inline_artifact(f"orange-book:product:off:{i:03d}") for i in range(8)]
    _stub_sync_artifact(monkeypatch, {a.canonical_id for a in artifacts})
    with _offline_client() as client:
        stats = sync_manifest(_manifest(*artifacts), client=client, workers=1)

    assert stats.aborted_reason is None
    assert stats.error_documents == 8


class _Stub384Provider:
    name = "stub-384"
    dim = 384


def test_embed_during_sync_preflights_before_any_document_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup compatibility test: refuse before fetch, not per document."""
    from regwatch.store import pgvector_store

    monkeypatch.setattr(sync_module, "get_embedding_provider", lambda: _Stub384Provider())
    monkeypatch.setattr(pgvector_store, "get_embedding_provider", lambda: _Stub384Provider())

    def no_document_work(*args: object, **kwargs: object) -> tuple[str, int]:
        raise AssertionError("preflight must refuse before any document is processed")

    monkeypatch.setattr(sync_module, "sync_artifact", no_document_work)
    artifact = _inline_artifact("orange-book:product:preflight:001")
    with get_engine().connect() as conn:
        runs_before = int(conn.execute(text("SELECT count(*) FROM fda_corpus_run")).scalar() or 0)
    with _offline_client() as client, pytest.raises(RuntimeError, match="384"):
        sync_manifest(_manifest(artifact), client=client, defer_embeddings=False)
    with get_engine().connect() as conn:
        runs_after = int(conn.execute(text("SELECT count(*) FROM fda_corpus_run")).scalar() or 0)
    # A refused preflight attempted nothing: no run row was ever created.
    assert runs_after == runs_before


def test_deferred_sync_skips_the_embed_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunk-only runs must not run the embed-during-sync preflight.

    (The store's own lazy dim assert may still resolve the provider on first
    write -- that guard predates the preflight and is covered elsewhere; here
    only the sync-level preflight seams are fenced.)
    """

    def no_preflight(*args: object, **kwargs: object) -> object:
        raise AssertionError("a deferred sync must not preflight the embed write config")

    monkeypatch.setattr(sync_module, "get_embedding_provider", no_preflight)
    monkeypatch.setattr(sync_module, "assert_embedding_write_config", no_preflight)
    artifact = _inline_artifact("orange-book:product:deferred:001")
    with _offline_client() as client:
        stats = sync_manifest(_manifest(artifact), client=client, defer_embeddings=True)
    assert stats.succeeded


def test_embed_pending_corpus_preflights_the_target_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from regwatch.corpus.embeddings import embed_pending_corpus
    from regwatch.store import pgvector_store

    monkeypatch.setattr(pgvector_store, "get_embedding_provider", lambda: _Stub384Provider())
    with pytest.raises(RuntimeError, match="384"):
        embed_pending_corpus("legacy")
