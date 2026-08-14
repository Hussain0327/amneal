"""Typed, deterministic manifest for the authoritative FDA corpus."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from regwatch.sources.policy import (
    FdaDocumentType,
    FdaSourceFamily,
    normalize_authoritative_url,
)


@dataclass(frozen=True)
class CorpusArtifact:
    """One stable FDA document identity discovered for corpus synchronization."""

    canonical_id: str
    source_family: FdaSourceFamily
    document_type: FdaDocumentType
    title: str
    source_url: str
    application_number: str | None = None
    product_number: str | None = None
    active_ingredient: str | None = None
    normalized_name: str | None = None
    brand_name: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    source_updated_at: str | None = None
    inline_text: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical_id = self.canonical_id.strip()
        title = self.title.strip()
        if not canonical_id or len(canonical_id) > 512:
            raise ValueError("corpus canonical_id must contain 1-512 characters")
        if not title:
            raise ValueError("corpus artifact title must not be empty")
        canonical_url = normalize_authoritative_url(self.source_url, self.source_family)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "source_url", canonical_url)
        if self.inline_text is not None and not self.inline_text.strip():
            raise ValueError("inline corpus text must not be blank")

    @property
    def inline_sha256(self) -> str | None:
        if self.inline_text is None:
            return None
        return hashlib.sha256(self.inline_text.encode("utf-8")).hexdigest()

    def fingerprint_record(self) -> dict[str, Any]:
        """Stable discovery facts only; fetch time and artifact bytes are excluded."""
        return {
            "canonical_id": self.canonical_id,
            "source_family": self.source_family.value,
            "document_type": self.document_type.value,
            "title": self.title,
            "source_url": self.source_url,
            "application_number": self.application_number,
            "product_number": self.product_number,
            "active_ingredient": self.active_ingredient,
            "normalized_name": self.normalized_name,
            "brand_name": self.brand_name,
            "dosage_form": self.dosage_form,
            "route": self.route,
            "source_updated_at": self.source_updated_at,
            "inline_sha256": self.inline_sha256,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class CorpusManifest:
    """A duplicate-free, sorted set of corpus artifacts."""

    artifacts: tuple[CorpusArtifact, ...]
    source_snapshots: dict[str, str]
    # True only when discovery covered every approved source family without an
    # application filter. Activation must never mistake a developer's scoped
    # or limited sync for the complete serving corpus.
    complete_universe: bool = False

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.artifacts, key=lambda artifact: artifact.canonical_id))
        ids = [artifact.canonical_id for artifact in ordered]
        duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate corpus canonical ids: {duplicates[:10]}")
        object.__setattr__(self, "artifacts", ordered)

    @property
    def sha256(self) -> str:
        payload = {
            "schema_version": 1,
            "complete_universe": self.complete_universe,
            "source_snapshots": self.source_snapshots,
            "artifacts": [artifact.fingerprint_record() for artifact in self.artifacts],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def counts_by_family(self) -> dict[str, int]:
        counts = {family.value: 0 for family in FdaSourceFamily}
        for artifact in self.artifacts:
            counts[artifact.source_family.value] += 1
        return counts

    def counts_by_document_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for artifact in self.artifacts:
            key = artifact.document_type.value
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


def guidance_manifest_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "fda_be_guidance_manifest.json"


def load_be_guidance_artifacts(path: Path | None = None) -> list[CorpusArtifact]:
    """Load the reviewed FDA BE guidance allowlist; reject unknown schema."""
    manifest_path = path or guidance_manifest_path()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("FDA BE guidance manifest must use schema_version 1")
    documents = raw.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("FDA BE guidance manifest must contain documents")
    required = {
        "canonical_id",
        "title",
        "source_url",
        "landing_url",
        "issued",
        "status",
        "docket_number",
    }
    artifacts: list[CorpusArtifact] = []
    for index, item in enumerate(documents):
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError(f"invalid FDA BE guidance manifest item at index {index}")
        if item["status"] not in {"draft", "final"}:
            raise ValueError(f"invalid FDA BE guidance status at index {index}")
        landing_url = normalize_authoritative_url(
            str(item["landing_url"]), FdaSourceFamily.FDA_BE_GUIDANCE
        )
        artifacts.append(
            CorpusArtifact(
                canonical_id=str(item["canonical_id"]),
                source_family=FdaSourceFamily.FDA_BE_GUIDANCE,
                document_type=FdaDocumentType.BIOEQUIVALENCE_GUIDANCE,
                title=str(item["title"]),
                source_url=str(item["source_url"]),
                source_updated_at=str(item["issued"]),
                metadata={
                    "landing_url": landing_url,
                    "guidance_status": str(item["status"]),
                    "docket_number": str(item["docket_number"]),
                    "manifest_schema_version": 1,
                },
            )
        )
    return artifacts


def write_manifest_gzip(manifest: CorpusManifest, path: Path) -> str:
    """Write a deterministic, complete manifest and return its file SHA-256."""

    payload = {
        "schema_version": 1,
        "manifest_sha256": manifest.sha256,
        "complete_universe": manifest.complete_universe,
        "source_snapshots": manifest.source_snapshots,
        "artifacts": [
            artifact.fingerprint_record() | {"inline_text": artifact.inline_text}
            for artifact in manifest.artifacts
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text,
    ):
        json.dump(payload, text, sort_keys=True, separators=(",", ":"))
        text.write("\n")
    return _file_sha256(path)


def load_manifest_gzip(path: Path) -> CorpusManifest:
    """Load and integrity-check one persisted exact corpus manifest."""

    with gzip.open(path, mode="rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "manifest_sha256",
        "complete_universe",
        "source_snapshots",
        "artifacts",
    }:
        raise ValueError("persisted FDA manifest has an unknown schema")
    if raw["schema_version"] != 1:
        raise ValueError("persisted FDA manifest must use schema_version 1")
    if not isinstance(raw["source_snapshots"], dict) or not isinstance(raw["artifacts"], list):
        raise ValueError("persisted FDA manifest fields have invalid types")

    expected_artifact_fields = {
        "canonical_id",
        "source_family",
        "document_type",
        "title",
        "source_url",
        "application_number",
        "product_number",
        "active_ingredient",
        "normalized_name",
        "brand_name",
        "dosage_form",
        "route",
        "source_updated_at",
        "inline_sha256",
        "inline_text",
        "metadata",
    }
    artifacts: list[CorpusArtifact] = []
    for index, item in enumerate(raw["artifacts"]):
        if not isinstance(item, dict) or set(item) != expected_artifact_fields:
            raise ValueError(f"invalid persisted FDA manifest artifact at index {index}")
        if not isinstance(item["metadata"], dict):
            raise ValueError(f"invalid persisted FDA manifest metadata at index {index}")
        artifact = CorpusArtifact(
            canonical_id=str(item["canonical_id"]),
            source_family=FdaSourceFamily(str(item["source_family"])),
            document_type=FdaDocumentType(str(item["document_type"])),
            title=str(item["title"]),
            source_url=str(item["source_url"]),
            application_number=_optional_string(item["application_number"]),
            product_number=_optional_string(item["product_number"]),
            active_ingredient=_optional_string(item["active_ingredient"]),
            normalized_name=_optional_string(item["normalized_name"]),
            brand_name=_optional_string(item["brand_name"]),
            dosage_form=_optional_string(item["dosage_form"]),
            route=_optional_string(item["route"]),
            source_updated_at=_optional_string(item["source_updated_at"]),
            inline_text=_optional_string(item["inline_text"]),
            metadata=dict(item["metadata"]),
        )
        if artifact.inline_sha256 != item["inline_sha256"]:
            raise ValueError(f"persisted FDA inline hash mismatch at index {index}")
        artifacts.append(artifact)
    manifest = CorpusManifest(
        artifacts=tuple(artifacts),
        source_snapshots={str(key): str(value) for key, value in raw["source_snapshots"].items()},
        complete_universe=bool(raw["complete_universe"]),
    )
    if manifest.sha256 != raw["manifest_sha256"]:
        raise ValueError("persisted FDA manifest fingerprint mismatch")
    return manifest


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("persisted FDA manifest optional fields must be strings or null")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
