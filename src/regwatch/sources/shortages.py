"""Retired drug-shortage source shim.

Drug-shortage feeds are outside this corpus.  Legacy callers fail closed; no
network endpoint or credential path remains.
"""

from __future__ import annotations

import httpx

from regwatch.sources.policy import SourcePolicyError
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

SHORTAGES_DOC_URL = ""


class ShortagesHandler:
    source = SourceKind.SHORTAGE

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del query, client
        raise SourcePolicyError("drug shortages are outside the authoritative FDA corpus policy")
