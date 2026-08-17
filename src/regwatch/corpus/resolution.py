"""Audited terminal outcomes for exact-manifest FDA source records.

Terminal does not mean "ignored".  It is a positive, evidence-bearing
resolution of one manifest record after the retry budget is exhausted.  The
acceptance gate revalidates every field below before allowing that record to
stand in place of an indexed version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from regwatch.corpus.manifest import CorpusArtifact


class ResolutionStatus(StrEnum):
    PENDING = "pending"
    INDEXED = "indexed"
    MISSING_AT_SOURCE = "missing_at_source"
    UNPARSEABLE = "unparseable"


SOURCE_BYTES_HASH_KIND = "source_bytes"
TERMINAL_OBSERVATION_HASH_KIND = "terminal_observation"
TERMINAL_OBSERVATION_MIME_TYPE = "application/vnd.regwatch.source-observation+json"
TERMINAL_RESOLUTION_STATUSES = frozenset(
    {ResolutionStatus.MISSING_AT_SOURCE.value, ResolutionStatus.UNPARSEABLE.value}
)
_PARSE_ERROR_TYPES = frozenset({"PdfParseError", "PdfParseTimeoutError", "PdfPageLimitError"})
_MISSING_OBSERVATION_FINGERPRINT = hashlib.sha256(
    b"regwatch:fda-missing-source-observation:v1"
).hexdigest()


def missing_observation_fingerprint() -> str:
    """Return the stable processing identity for a no-bytes 404 observation."""

    return _MISSING_OBSERVATION_FINGERPRINT


def missing_observation_content_hash(artifact: CorpusArtifact) -> str:
    """Hash immutable observation identity without pretending it is source bytes."""

    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": ResolutionStatus.MISSING_AT_SOURCE.value,
            "canonical_id": artifact.canonical_id,
            "source_url": artifact.source_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def terminal_evidence_issues(
    row: Mapping[str, Any],
    artifact: CorpusArtifact,
    *,
    manifest_sha256: str,
    minimum_attempts: int,
) -> tuple[str, ...]:
    """Validate one current terminal row against the exact manifest contract."""

    status = str(row.get("resolution_status") or "")
    if status not in TERMINAL_RESOLUTION_STATUSES:
        return (f"unsupported terminal resolution status: {status or '<missing>'}",)

    issues: list[str] = []
    attempts = int(row.get("resolution_attempts") or 0)
    if attempts < minimum_attempts:
        issues.append(f"terminal resolution has {attempts} attempts; requires {minimum_attempts}")
    if row.get("resolved_at") is None:
        issues.append("terminal resolution is missing resolved_at")
    if not str(row.get("resolution_error") or "").strip():
        issues.append("terminal resolution is missing its error summary")

    evidence_value = row.get("resolution_evidence_json")
    evidence = evidence_value if isinstance(evidence_value, Mapping) else {}
    if evidence.get("manifest_sha256") != manifest_sha256:
        issues.append("terminal evidence is not bound to this exact manifest")
    if evidence.get("canonical_id") != artifact.canonical_id:
        issues.append("terminal evidence canonical_id does not match the manifest")
    if evidence.get("source_url") != artifact.source_url:
        issues.append("terminal evidence source_url does not match the manifest")
    if int(evidence.get("attempts") or 0) != attempts:
        issues.append("terminal evidence attempt count does not match the lifecycle row")

    hash_kind = str(row.get("content_hash_kind") or "")
    if status == ResolutionStatus.MISSING_AT_SOURCE.value:
        if hash_kind != TERMINAL_OBSERVATION_HASH_KIND:
            issues.append("missing-at-source resolution is not an observation hash")
        if int(evidence.get("http_status") or 0) != 404:
            issues.append("missing-at-source resolution lacks an exact HTTP 404 observation")
    elif status == ResolutionStatus.UNPARSEABLE.value:
        if hash_kind != SOURCE_BYTES_HASH_KIND:
            issues.append("unparseable resolution is not tied to captured source bytes")
        if not bool(row.get("artifact_retained")) or not str(row.get("artifact_uri") or "").strip():
            issues.append("unparseable resolution lacks a retained source artifact")
        if str(evidence.get("error_type") or "") not in _PARSE_ERROR_TYPES:
            issues.append("unparseable resolution was not produced by a reviewed parser error")

    return tuple(issues)
