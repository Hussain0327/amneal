"""Retired NDC source shim.

NDC is outside the authoritative corpus policy.  The class remains only to
give old internal callers a fail-closed migration error instead of performing
an unreviewed network request.
"""

from __future__ import annotations

import httpx

from regwatch.sources.policy import SourcePolicyError
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

NDC_DOC_URL = ""


class NdcHandler:
    source = SourceKind.NDC

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del query, client
        raise SourcePolicyError("NDC is outside the authoritative FDA corpus policy")
