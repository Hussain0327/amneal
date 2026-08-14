from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

import pytest

from regwatch.corpus.artifact_store import (
    DiscardArtifactStore,
    FilesystemArtifactStore,
    S3ArtifactStore,
)


class _MissingObject(Exception):
    response: ClassVar[dict[str, dict[str, int]]] = {"ResponseMetadata": {"HTTPStatusCode": 404}}


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            body, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _MissingObject from exc
        return {"ContentLength": len(body), "Metadata": metadata}

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, Any],
    ) -> None:
        self.objects[(bucket, key)] = (
            Path(filename).read_bytes(),
            dict(ExtraArgs["Metadata"]),
        )

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[(bucket, key)][0])


def _source(tmp_path: Path, body: bytes = b"authoritative FDA bytes") -> tuple[Path, str]:
    path = tmp_path / "staged.part"
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


def test_discard_store_records_checksum_uri_without_retention(tmp_path: Path) -> None:
    source, digest = _source(tmp_path)

    reference = DiscardArtifactStore().put_file(
        source,
        content_hash=digest,
        namespace="documents/drugs_at_fda",
        suffix=".pdf",
    )

    assert reference.retained is False
    assert reference.uri.endswith(f"/{digest}.pdf")
    assert source.exists()


def test_filesystem_store_is_content_addressed_and_materializable(tmp_path: Path) -> None:
    source, digest = _source(tmp_path)
    root = tmp_path / "durable artifacts"
    store = FilesystemArtifactStore(root)

    first = store.put_file(
        source,
        content_hash=digest,
        namespace="documents/action_package",
        suffix=".pdf",
    )
    second = store.put_file(
        source,
        content_hash=digest,
        namespace="documents/action_package",
        suffix=".pdf",
    )
    materialized = tmp_path / "restored.pdf"
    store.materialize(first.uri, materialized)

    assert first == second
    assert first.retained is True
    assert materialized.read_bytes() == source.read_bytes()


def test_filesystem_store_rejects_corrupt_existing_object(tmp_path: Path) -> None:
    source, digest = _source(tmp_path)
    store = FilesystemArtifactStore(tmp_path / "durable")
    reference = store.put_file(
        source,
        content_hash=digest,
        namespace="documents/psg",
        suffix=".pdf",
    )
    Path(reference.uri.removeprefix("file://")).write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="artifact is corrupt"):
        store.put_file(
            source,
            content_hash=digest,
            namespace="documents/psg",
            suffix=".pdf",
        )


def test_s3_store_verifies_metadata_and_round_trips(tmp_path: Path) -> None:
    source, digest = _source(tmp_path)
    client = _FakeS3()
    store = S3ArtifactStore(
        client=client,
        bucket="regwatch-corpus",
        prefix="production/fda",
        sse="AES256",
        kms_key_id=None,
    )

    reference = store.put_file(
        source,
        content_hash=digest,
        namespace="documents/orange_book",
        suffix=".txt",
    )
    restored = tmp_path / "restored.txt"
    store.materialize(reference.uri, restored)

    assert reference.uri.startswith("s3://regwatch-corpus/production/fda/")
    assert reference.retained is True
    assert restored.read_bytes() == source.read_bytes()
