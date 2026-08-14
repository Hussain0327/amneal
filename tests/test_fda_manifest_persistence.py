from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from regwatch.corpus.artifact_store import FilesystemArtifactStore
from regwatch.corpus.manifest import (
    CorpusArtifact,
    CorpusManifest,
    load_manifest_gzip,
    write_manifest_gzip,
)
from regwatch.corpus.persisted_manifest import load_persisted_manifest, persist_manifest
from regwatch.sources.policy import FdaDocumentType, FdaSourceFamily
from regwatch.store.db import get_engine


def _manifest() -> CorpusManifest:
    return CorpusManifest(
        artifacts=(
            CorpusArtifact(
                canonical_id="orange-book:product:n:020503",
                source_family=FdaSourceFamily.ORANGE_BOOK,
                document_type=FdaDocumentType.ORANGE_BOOK_PRODUCT,
                title="Orange Book product",
                source_url=("https://www.accessdata.fda.gov/scripts/cder/ob/results_product.cfm"),
                inline_text="Deterministic FDA snapshot row",
                metadata={"te_code": "AB"},
            ),
        ),
        source_snapshots={"orange_book": "sha256:fixture"},
        complete_universe=True,
    )


def test_manifest_gzip_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    manifest = _manifest()
    first = tmp_path / "first.json.gz"
    second = tmp_path / "second.json.gz"

    first_hash = write_manifest_gzip(manifest, first)
    second_hash = write_manifest_gzip(manifest, second)

    assert first_hash == second_hash
    assert first.read_bytes() == second.read_bytes()
    assert load_manifest_gzip(first) == manifest


def test_persisted_manifest_round_trips_through_durable_store(tmp_path: Path) -> None:
    manifest = _manifest()
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    reference = persist_manifest(manifest, artifact_store=store)
    loaded = load_persisted_manifest(manifest.sha256, artifact_store=store)

    assert reference.manifest_sha256 == manifest.sha256
    assert reference.document_count == 1
    assert loaded == manifest
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT artifact_retained, document_count, complete_universe "
                "FROM fda_corpus_manifest WHERE sha256 = :sha256"
            ),
            {"sha256": manifest.sha256},
        ).one()
    assert tuple(row) == (True, 1, True)


def test_persisted_manifest_detects_durable_object_corruption(tmp_path: Path) -> None:
    manifest = _manifest()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    reference = persist_manifest(manifest, artifact_store=store)
    persisted_path = next((tmp_path / "artifacts").rglob("*.json.gz"))
    persisted_path.write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="artifact checksum mismatch"):
        load_persisted_manifest(reference.manifest_sha256, artifact_store=store)
