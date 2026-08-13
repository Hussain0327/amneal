from __future__ import annotations

from datetime import UTC, datetime
from threading import Barrier

import httpx
import pytest
from sqlalchemy import text

from regwatch.corpus import sync as sync_module
from regwatch.corpus.embeddings import (
    corpus_embedding_counts,
    pending_corpus_chunks,
    write_corpus_embeddings,
)
from regwatch.corpus.manifest import CorpusArtifact, CorpusManifest
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

    pending = pending_corpus_chunks("legacy", limit=128)
    vectors = embed_documents(get_embedding_provider(), [chunk.text for chunk in pending])
    write_corpus_embeddings("legacy", pending, vectors)

    after = corpus_embedding_counts("legacy")
    assert after.embedded_chunks == after.chunks
    assert after.pending_chunks == 0
    assert pending_corpus_chunks("legacy", limit=128) == []


def test_document_publish_rolls_back_as_one_unit(
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
    assert _counts() == (0, 0, 0)
    with get_engine().connect() as conn:
        run = conn.execute(
            text("SELECT status, error_documents FROM fda_corpus_run WHERE id = :id"),
            {"id": stats.run_id},
        ).one()
    assert tuple(run) == ("failed", 1)


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
    ) -> tuple[str, int]:
        del artifact, client, defer_embeddings
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
    assert rows == [(keep.canonical_id, True), (retire.canonical_id, False)]
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


def test_html_pages_are_reduced_to_visible_main_content() -> None:
    payload = ArtifactPayload(
        source_url="https://www.fda.gov/drugs/drugsafety/example",
        content=(
            b"<!doctype html><html><body><header>global navigation</header>"
            b"<main><h1>FDA regulatory action</h1><p>Authoritative safety text.</p>"
            b"<script>do_not_index()</script></main><footer>footer links</footer>"
            b"</body></html>"
        ),
        content_hash="a" * 64,
        mime_type="text/html; charset=utf-8",
        fetched_at=datetime.now(UTC),
    )
    parsed = parse_artifact(payload)
    assert parsed.engine == "selectolax-main-content"
    assert parsed.pages == ["FDA regulatory action\nAuthoritative safety text."]
    assert "navigation" not in parsed.text
    assert "do_not_index" not in parsed.text
