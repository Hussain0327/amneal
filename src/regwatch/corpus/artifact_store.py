"""Content-addressed artifact storage for the authoritative FDA corpus.

The worker always stages one bounded document locally. This module decides
whether those bytes are discarded after processing, retained on a development
filesystem, or copied to durable S3-compatible object storage. No provider is
allowed to change the content-addressed key derived from the verified SHA-256.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from config.settings import Settings, get_settings

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SEGMENT_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


@dataclass(frozen=True)
class ArtifactReference:
    uri: str
    retained: bool


class ArtifactStore(Protocol):
    def put_file(
        self,
        path: Path,
        *,
        content_hash: str,
        namespace: str,
        suffix: str,
    ) -> ArtifactReference: ...

    def materialize(self, uri: str, destination: Path) -> None: ...


def _object_key(content_hash: str, namespace: str, suffix: str) -> str:
    digest = content_hash.lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("artifact content_hash must be a lowercase SHA-256")
    parts = [part for part in namespace.strip("/").split("/") if part]
    if not parts or any(_SEGMENT_RE.fullmatch(part) is None for part in parts):
        raise ValueError("artifact namespace contains an unsafe path segment")
    if not suffix.startswith(".") or _SEGMENT_RE.fullmatch(suffix[1:]) is None:
        raise ValueError("artifact suffix must be a safe extension")
    return "/".join((*parts, "sha256", digest[:2], digest[2:4], f"{digest}{suffix}"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class DiscardArtifactStore:
    """Checksum-only mode: the caller's staged file is never retained."""

    def put_file(
        self,
        path: Path,
        *,
        content_hash: str,
        namespace: str,
        suffix: str,
    ) -> ArtifactReference:
        del path
        key = _object_key(content_hash, namespace, suffix)
        return ArtifactReference(uri=f"discard://{key}", retained=False)

    def materialize(self, uri: str, destination: Path) -> None:
        del uri, destination
        raise RuntimeError("discarded artifacts cannot be materialized")


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def put_file(
        self,
        path: Path,
        *,
        content_hash: str,
        namespace: str,
        suffix: str,
    ) -> ArtifactReference:
        key = _object_key(content_hash, namespace, suffix)
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _sha256(destination) != content_hash:
                raise RuntimeError(f"content-addressed artifact is corrupt: {destination}")
            return ArtifactReference(uri=destination.as_uri(), retained=True)

        fd, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, path.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _sha256(temporary) != content_hash:
                raise RuntimeError("artifact changed between checksum and durable copy")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return ArtifactReference(uri=destination.as_uri(), retained=True)

    def materialize(self, uri: str, destination: Path) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme != "file":
            raise ValueError("filesystem artifact URI must use file://")
        source = Path(unquote(parsed.path)).resolve()
        if not source.is_relative_to(self.root):
            raise ValueError("filesystem artifact URI escapes the configured root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


class S3ArtifactStore:
    """Minimal S3-compatible store; boto3 is loaded only in the worker extra."""

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str,
        sse: str | None,
        kms_key_id: str | None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("FDA_ARTIFACT_S3_BUCKET is required for the s3 store")
        self.client = client
        self.bucket = bucket.strip()
        self.prefix = prefix.strip("/")
        self.sse = sse
        self.kms_key_id = kms_key_id

    def put_file(
        self,
        path: Path,
        *,
        content_hash: str,
        namespace: str,
        suffix: str,
    ) -> ArtifactReference:
        relative = _object_key(content_hash, namespace, suffix)
        key = f"{self.prefix}/{relative}" if self.prefix else relative
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404:
                raise
        else:
            metadata = existing.get("Metadata", {})
            if (
                metadata.get("sha256") != content_hash
                or int(existing["ContentLength"]) != path.stat().st_size
            ):
                raise RuntimeError(
                    "S3 content-addressed artifact metadata mismatch: " f"s3://{self.bucket}/{key}"
                )
            return ArtifactReference(uri=f"s3://{self.bucket}/{key}", retained=True)

        extra: dict[str, Any] = {"Metadata": {"sha256": content_hash}}
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
        if self.sse == "aws:kms" and self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        verified = self.client.head_object(Bucket=self.bucket, Key=key)
        if (
            verified.get("Metadata", {}).get("sha256") != content_hash
            or int(verified["ContentLength"]) != path.stat().st_size
        ):
            raise RuntimeError("S3 artifact upload did not preserve the SHA-256 metadata")
        return ArtifactReference(uri=f"s3://{self.bucket}/{key}", retained=True)

    def materialize(self, uri: str, destination: Path) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            raise ValueError("S3 artifact URI is outside the configured bucket")
        key = parsed.path.lstrip("/")
        required_prefix = f"{self.prefix}/" if self.prefix else ""
        if required_prefix and not key.startswith(required_prefix):
            raise ValueError("S3 artifact URI is outside the configured prefix")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(destination))


def build_artifact_store(settings: Settings | None = None) -> ArtifactStore:
    selected = settings or get_settings()
    if selected.fda_artifact_store == "discard":
        return DiscardArtifactStore()
    if selected.fda_artifact_store == "filesystem":
        return FilesystemArtifactStore(selected.fda_artifact_dir)

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - worker image installs the extra
        raise RuntimeError(
            "s3 artifact storage requires the corpus-worker dependency extra"
        ) from exc
    client = boto3.client(
        "s3",
        endpoint_url=selected.fda_artifact_s3_endpoint_url,
        region_name=selected.fda_artifact_s3_region,
        aws_access_key_id=selected.fda_artifact_s3_access_key_id,
        aws_secret_access_key=selected.fda_artifact_s3_secret_access_key,
        aws_session_token=selected.fda_artifact_s3_session_token,
    )
    return S3ArtifactStore(
        client=client,
        bucket=selected.fda_artifact_s3_bucket or "",
        prefix=selected.fda_artifact_s3_prefix,
        sse=selected.fda_artifact_s3_sse,
        kms_key_id=selected.fda_artifact_s3_kms_key_id,
    )
