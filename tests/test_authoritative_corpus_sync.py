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
from regwatch.corpus.resolution import terminal_evidence_issues
from regwatch.corpus.sharding import corpus_shard_id
from regwatch.corpus.status import authoritative_corpus_coverage
from regwatch.corpus.sync import ArtifactPayload, parse_artifact, sync_manifest
from regwatch.ingest.pdf_parser import ParsedPdf, PdfParseError
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


def _network_artifact(canonical_id: str, source_url: str) -> CorpusArtifact:
    return CorpusArtifact(
        canonical_id=canonical_id,
        source_family=FdaSourceFamily.ACTION_PACKAGE,
        document_type=FdaDocumentType.CLINICAL_REVIEW,
        title=f"Test network artifact {canonical_id}",
        source_url=source_url,
        application_number="NDA020503",
        active_ingredient="ALBUTEROL SULFATE",
        normalized_name="albuterol sulfate",
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


def test_missing_source_becomes_terminal_only_after_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module

    monkeypatch.setenv("FDA_CORPUS_TERMINAL_ATTEMPTS", "2")
    settings_module.get_settings.cache_clear()
    artifact = _network_artifact(
        "drugs-at-fda:application-doc:missing",
        "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/missing.pdf",
    )
    stale_manifest = _manifest(artifact)
    manifest = CorpusManifest(
        artifacts=(artifact,),
        source_snapshots={"fixture": "sha256:new-manifest"},
        complete_universe=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            stale = sync_manifest(stale_manifest, defer_embeddings=True, client=client)
            first = sync_manifest(manifest, defer_embeddings=True, client=client)
            second = sync_manifest(manifest, defer_embeddings=True, client=client)

        assert stale.error_documents == 1
        assert stale.terminal_documents == 0
        assert first.error_documents == 1
        assert first.terminal_documents == 0
        assert first.succeeded is False
        assert second.error_documents == 0
        assert second.terminal_documents == 1
        assert second.succeeded is True

        readiness = shard_readiness(
            manifest,
            corpus_shard_id(artifact.canonical_id),
            profile_id="legacy",
        )
        assert readiness.ready is True
        assert readiness.chunked_documents == 0
        assert readiness.terminal_documents == 1
        assert readiness.missing_at_source_documents == 1

        with get_engine().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT is_current, content_hash_kind, chunk_status, "
                    "resolution_status, resolution_attempts, "
                    "resolution_evidence_json->>'http_status' "
                    "FROM fda_document_version"
                )
            ).one()
        assert tuple(row) == (
            True,
            "terminal_observation",
            "failed",
            "missing_at_source",
            2,
            "404",
        )

        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE fda_document_version SET resolution_evidence_json = '{}'::jsonb")
            )
        invalid = shard_readiness(
            manifest,
            corpus_shard_id(artifact.canonical_id),
            profile_id="legacy",
        )
        assert invalid.ready is False
        assert any("not bound to this exact manifest" in issue for issue in invalid.issues)
    finally:
        settings_module.get_settings.cache_clear()


def test_unparseable_terminal_requires_retained_bytes_and_can_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module

    monkeypatch.setenv("FDA_CORPUS_TERMINAL_ATTEMPTS", "2")
    settings_module.get_settings.cache_clear()
    artifact = _network_artifact(
        "drugs-at-fda:application-doc:unparseable",
        "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/unparseable.pdf",
    )
    manifest = _manifest(artifact)
    body = b"%PDF-1.7\nretained parser fixture"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/pdf"},
            request=request,
        )

    def fail_parse(_payload: ArtifactPayload) -> ParsedPdf:
        raise PdfParseError("synthetic exhausted parser")

    store = FilesystemArtifactStore(tmp_path / "durable")
    monkeypatch.setattr(sync_module, "parse_artifact", fail_parse)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = sync_manifest(
                manifest,
                defer_embeddings=True,
                client=client,
                artifact_store=store,
            )
            second = sync_manifest(
                manifest,
                defer_embeddings=True,
                client=client,
                artifact_store=store,
            )

            assert first.error_documents == 1
            assert first.terminal_documents == 0
            assert second.error_documents == 0
            assert second.terminal_documents == 1
            readiness = shard_readiness(
                manifest,
                corpus_shard_id(artifact.canonical_id),
                profile_id="legacy",
            )
            assert readiness.ready is True
            assert readiness.unparseable_documents == 1

            with get_engine().connect() as conn:
                terminal = conn.execute(
                    text(
                        "SELECT content_hash_kind, artifact_retained, resolution_status, "
                        "resolution_attempts FROM fda_document_version"
                    )
                ).one()
            assert tuple(terminal) == ("source_bytes", True, "unparseable", 2)

            monkeypatch.setattr(
                sync_module,
                "parse_artifact",
                lambda _payload: ParsedPdf(
                    text="Recovered authoritative FDA text.",
                    pages=["Recovered authoritative FDA text."],
                    engine="recovery-fixture",
                ),
            )
            recovered = sync_manifest(
                manifest,
                defer_embeddings=True,
                client=client,
                artifact_store=store,
            )

        assert recovered.added_documents == 1
        assert recovered.terminal_documents == 0
        with get_engine().connect() as conn:
            current = conn.execute(
                text(
                    "SELECT resolution_status, chunk_status, resolution_attempts, "
                    "resolution_error, resolution_evidence_json "
                    "FROM fda_document_version WHERE is_current"
                )
            ).one()
        assert current[0] == "indexed"
        assert current[1] == "complete"
        assert current[2] == 3
        assert current[3] is None
        assert current[4] == {}
    finally:
        settings_module.get_settings.cache_clear()


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


def test_non_parser_failures_never_enter_the_terminal_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module

    monkeypatch.setenv("FDA_CORPUS_TERMINAL_ATTEMPTS", "2")
    settings_module.get_settings.cache_clear()
    artifact = _inline_artifact("orange-book:product:n:088888")

    def fail_chunk_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database publication failed")

    monkeypatch.setattr(sync_module, "add_chunks", fail_chunk_write)
    try:
        with _offline_client() as client:
            first = sync_manifest(_manifest(artifact), client=client)
            second = sync_manifest(_manifest(artifact), client=client)
        assert first.error_documents == 1
        assert second.error_documents == 1
        assert second.terminal_documents == 0
        with get_engine().connect() as conn:
            resolution = conn.execute(
                text(
                    "SELECT resolution_status, resolution_attempts "
                    "FROM fda_document_version WHERE is_current"
                )
            ).one()
        assert tuple(resolution) == ("pending", 2)
    finally:
        settings_module.get_settings.cache_clear()


def test_sync_uses_bounded_parallel_document_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tuple(_inline_artifact(f"orange-book:product:n:02050{index}") for index in range(3))
    rendezvous = Barrier(3, timeout=2)

    def synchronized_sync(
        artifact: CorpusArtifact,
        *,
        manifest_sha256: str,
        client: httpx.Client,
        defer_embeddings: bool,
        artifact_store: object,
    ) -> tuple[str, int]:
        del artifact, manifest_sha256, client, defer_embeddings, artifact_store
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


def test_activation_accepts_only_audited_terminal_manifest_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module

    monkeypatch.setenv("FDA_CORPUS_TERMINAL_ATTEMPTS", "2")
    settings_module.get_settings.cache_clear()
    indexed = (
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
    missing = _network_artifact(
        "drugs-at-fda:application-doc:missing-review",
        "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/missing-review.pdf",
    )
    manifest = _manifest(*indexed, missing, complete=True)

    source_available = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_available
        assert str(request.url) == missing.source_url
        if source_available:
            return httpx.Response(
                200,
                content=b"%PDF-1.7\nrecovered source fixture",
                headers={"content-type": "application/pdf"},
                request=request,
            )
        return httpx.Response(404, request=request)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = sync_manifest(manifest, client=client)
            second = sync_manifest(manifest, client=client)

        assert first.succeeded is False
        assert second.succeeded is True
        assert second.unchanged_documents == len(indexed)
        assert second.terminal_documents == 1
        coverage = authoritative_corpus_coverage()
        assert coverage.documents == len(indexed)
        assert coverage.terminal_documents == 1
        assert coverage.missing_at_source_documents == 1
        assert coverage.unparseable_documents == 0
        assert coverage.activation_ready is True
        assert coverage.activation_blockers == ()
        with get_engine().connect() as conn:
            run = conn.execute(
                text(
                    "SELECT expected_documents, unchanged_documents, terminal_documents, "
                    "error_documents FROM fda_corpus_run WHERE id = :id"
                ),
                {"id": second.run_id},
            ).one()
        assert tuple(run) == (len(indexed) + 1, len(indexed), 1, 0)

        terminal_run_id, terminal_readiness = finalize_orchestrated_manifest(
            manifest,
            profile_id="legacy",
        )
        assert terminal_readiness.terminal_documents == 1

        # A later complete manifest may retire a terminal record. Coverage and
        # activation count only active documents, not its retained audit row.
        with _offline_client() as client:
            retired = sync_manifest(_manifest(*indexed, complete=True), client=client)
        assert retired.retired_documents == 1
        retired_coverage = authoritative_corpus_coverage()
        assert retired_coverage.terminal_documents == 0
        assert retired_coverage.activation_ready is True

        # Reintroducing the original exact manifest can recover the source. A
        # fresh acceptance run must replace the prior terminal counts instead
        # of reusing its now-stale orchestrated run.
        source_available = True
        monkeypatch.setattr(
            sync_module,
            "parse_artifact",
            lambda _payload: ParsedPdf(
                text="Recovered authoritative FDA review.",
                pages=["Recovered authoritative FDA review."],
                engine="recovery-fixture",
            ),
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            recovered = sync_manifest(manifest, client=client)
        assert recovered.succeeded is True
        assert recovered.terminal_documents == 0

        recovered_run_id, recovered_readiness = finalize_orchestrated_manifest(
            manifest,
            profile_id="legacy",
        )
        assert recovered_run_id != terminal_run_id
        assert recovered_readiness.chunked_documents == len(indexed) + 1
        assert recovered_readiness.terminal_documents == 0
        recovered_coverage = authoritative_corpus_coverage()
        assert recovered_coverage.terminal_documents == 0
        assert recovered_coverage.activation_ready is True
        with get_engine().connect() as conn:
            accepted = conn.execute(
                text(
                    "SELECT id, unchanged_documents, terminal_documents "
                    "FROM fda_corpus_run WHERE id IN (:terminal_id, :recovered_id) "
                    "ORDER BY terminal_documents DESC"
                ),
                {
                    "terminal_id": terminal_run_id,
                    "recovered_id": recovered_run_id,
                },
            ).all()
        assert [tuple(row) for row in accepted] == [
            (terminal_run_id, len(indexed), 1),
            (recovered_run_id, len(indexed) + 1, 0),
        ]
    finally:
        settings_module.get_settings.cache_clear()


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
    repeated_run_id, repeated_readiness = finalize_orchestrated_manifest(
        manifest,
        profile_id="legacy",
    )
    assert repeated_run_id == run_id
    assert repeated_readiness == readiness


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


def test_pre_ledger_writer_rows_self_heal_instead_of_blocking_their_shard() -> None:
    """A version completed by pre-0025 worker code must not freeze its shard.

    Migration 0025's backfill is a one-time statement: workers still running
    the previous code write completed versions whose ledger columns hold the
    ADD COLUMN defaults (resolution_status='pending', is_current=false).
    Without the self-heal in _record_acquired_version, the next sync reports
    such a document "unchanged" while readiness demands resolution_status=
    'indexed', so the shard fails forever with no repair path.
    """
    artifact = _inline_artifact("orange-book:product:n:910001")
    manifest = _manifest(artifact)
    with _offline_client() as client:
        first = sync_manifest(manifest, client=client)
    assert first.added_documents == 1

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE fda_document_version SET resolution_status = 'pending', "
                "resolved_at = NULL, is_current = false"
            )
        )

    with _offline_client() as client:
        resumed = sync_manifest(manifest, client=client)
    assert resumed.unchanged_documents == 1
    assert resumed.error_documents == 0

    readiness = shard_readiness(
        manifest,
        corpus_shard_id(artifact.canonical_id),
        profile_id="legacy",
    )
    assert readiness.issues == ()
    assert readiness.ready is True
    assert readiness.chunked_documents == 1

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT resolution_status, is_current, resolved_at IS NOT NULL FROM fda_document_version"
            )
        ).one()
    assert tuple(row) == ("indexed", True, True)


def test_every_terminal_evidence_field_is_independently_validated() -> None:
    """Each check in terminal_evidence_issues must fail on its own.

    The acceptance gate trusts this function to revalidate terminal rows; a
    silently deleted check would let unaudited rows stand in for indexed
    documents, so every field gets its own corruption and its own assertion.
    """
    artifact = _network_artifact(
        "drugs-at-fda:application-doc:evidence",
        "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/evidence.pdf",
    )
    sha = "a" * 64
    resolved_at = datetime.now(UTC)

    def missing_row() -> dict[str, object]:
        return {
            "resolution_status": "missing_at_source",
            "resolution_attempts": 2,
            "resolved_at": resolved_at,
            "resolution_error": "HTTP 404 after retry exhaustion",
            "content_hash_kind": "terminal_observation",
            "resolution_evidence_json": {
                "manifest_sha256": sha,
                "canonical_id": artifact.canonical_id,
                "source_url": artifact.source_url,
                "attempts": 2,
                "http_status": 404,
            },
        }

    def unparseable_row() -> dict[str, object]:
        return {
            "resolution_status": "unparseable",
            "resolution_attempts": 2,
            "resolved_at": resolved_at,
            "resolution_error": "PdfParseError: exhausted parser",
            "content_hash_kind": "source_bytes",
            "artifact_retained": True,
            "artifact_uri": "s3://bucket/documents/sha256/ab/cd/abcd",
            "resolution_evidence_json": {
                "manifest_sha256": sha,
                "canonical_id": artifact.canonical_id,
                "source_url": artifact.source_url,
                "attempts": 2,
                "error_type": "PdfParseError",
            },
        }

    def issues(row: dict[str, object]) -> tuple[str, ...]:
        return terminal_evidence_issues(row, artifact, manifest_sha256=sha, minimum_attempts=2)

    assert issues(missing_row()) == ()
    assert issues(unparseable_row()) == ()

    def corrupted(base: dict[str, object], **changes: object) -> dict[str, object]:
        row = dict(base)
        base_evidence = row["resolution_evidence_json"]
        assert isinstance(base_evidence, dict)
        evidence: dict[str, object] = dict(base_evidence)
        for key, value in changes.items():
            if key in evidence or key in ("http_status", "error_type"):
                evidence[key] = value
            else:
                row[key] = value
        row["resolution_evidence_json"] = evidence
        return row

    cases: list[tuple[dict[str, object], str]] = [
        (corrupted(missing_row(), resolution_status="pending"), "unsupported terminal"),
        (corrupted(missing_row(), resolution_attempts=1), "requires 2"),
        (corrupted(missing_row(), resolved_at=None), "missing resolved_at"),
        (corrupted(missing_row(), resolution_error="  "), "missing its error summary"),
        (corrupted(missing_row(), manifest_sha256="b" * 64), "not bound to this exact manifest"),
        (corrupted(missing_row(), canonical_id="other:id"), "canonical_id does not match"),
        (
            corrupted(missing_row(), source_url="https://www.fda.gov/other"),
            "source_url does not match",
        ),
        (corrupted(missing_row(), attempts=3), "attempt count does not match"),
        (corrupted(missing_row(), content_hash_kind="source_bytes"), "not an observation hash"),
        (corrupted(missing_row(), http_status=403), "lacks an exact HTTP 404"),
        (
            corrupted(unparseable_row(), content_hash_kind="terminal_observation"),
            "not tied to captured source bytes",
        ),
        (corrupted(unparseable_row(), artifact_retained=False), "lacks a retained source artifact"),
        (corrupted(unparseable_row(), artifact_uri="  "), "lacks a retained source artifact"),
        (
            corrupted(unparseable_row(), error_type="ValueError"),
            "not produced by a reviewed parser error",
        ),
    ]
    for row, expected in cases:
        found = issues(row)
        assert any(expected in issue for issue in found), (expected, found)


def test_unparseable_attempts_reset_when_the_manifest_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse-failure attempts must be manifest-scoped, like missing-source ones.

    Attempts accumulated against a stale manifest must not count toward
    terminalizing a record under a new manifest; otherwise one pre-freeze
    failure plus one post-freeze failure terminalizes a document the new
    manifest was never given a full retry budget for.
    """
    import config.settings as settings_module

    monkeypatch.setenv("FDA_CORPUS_TERMINAL_ATTEMPTS", "2")
    settings_module.get_settings.cache_clear()
    artifact = _network_artifact(
        "drugs-at-fda:application-doc:unparseable-reset",
        "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/unparseable-reset.pdf",
    )
    stale_manifest = _manifest(artifact)
    manifest = CorpusManifest(
        artifacts=(artifact,),
        source_snapshots={"fixture": "sha256:new-manifest"},
        complete_universe=False,
    )
    body = b"%PDF-1.7\nretained parser fixture"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/pdf"},
            request=request,
        )

    def fail_parse(_payload: ArtifactPayload) -> ParsedPdf:
        raise PdfParseError("synthetic exhausted parser")

    store = FilesystemArtifactStore(tmp_path / "durable")
    monkeypatch.setattr(sync_module, "parse_artifact", fail_parse)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            stale = sync_manifest(
                stale_manifest, defer_embeddings=True, client=client, artifact_store=store
            )
            first = sync_manifest(
                manifest, defer_embeddings=True, client=client, artifact_store=store
            )
            second = sync_manifest(
                manifest, defer_embeddings=True, client=client, artifact_store=store
            )
        assert stale.error_documents == 1
        assert stale.terminal_documents == 0
        assert first.error_documents == 1
        assert first.terminal_documents == 0
        assert second.error_documents == 0
        assert second.terminal_documents == 1
    finally:
        settings_module.get_settings.cache_clear()
