"""Reviewed FDA bioequivalence-guidance manifest handler."""

from __future__ import annotations

import httpx

from regwatch.corpus.manifest import load_be_guidance_artifacts
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord


class FdaBeGuidanceHandler:
    source = SourceKind.FDA_BE_GUIDANCE

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del client
        terms = {
            token for token in query.query_text.lower().replace("-", " ").split() if len(token) > 2
        }
        artifacts = load_be_guidance_artifacts()
        ranked = sorted(
            artifacts,
            key=lambda artifact: (
                -sum(term in artifact.title.lower() for term in terms),
                artifact.title,
            ),
        )
        return [
            SourceRecord(
                source=self.source,
                title=artifact.title,
                source_url=artifact.source_url,
                identifiers={"canonical_id": artifact.canonical_id},
                fields={
                    **artifact.metadata,
                    "issued": artifact.source_updated_at,
                },
            )
            for artifact in ranked[: query.limit]
            if not terms or any(term in artifact.title.lower() for term in terms)
        ]
