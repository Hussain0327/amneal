"""Typed, deterministic manifest for the authoritative FDA corpus."""

from __future__ import annotations

import hashlib
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
