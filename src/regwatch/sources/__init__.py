"""FDA source types plus lazy routing entry points.

The corpus manifest imports the policy module while the router imports corpus-
backed handlers.  Keeping router imports lazy makes that dependency one-way and
also keeps lightweight policy/manifest tooling from initializing every source
adapter merely by importing ``regwatch.sources``.
"""

from collections.abc import Iterable

import httpx

from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord


def route_sources(
    query: SourceQuery,
    *,
    requested: Iterable[SourceKind] | None = None,
) -> list[SourceKind]:
    from regwatch.sources.router import route_sources as _route_sources

    return _route_sources(query, requested=requested)


def search_sources(
    query: SourceQuery,
    *,
    sources: Iterable[SourceKind] | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[SourceKind], list[SourceRecord]]:
    from regwatch.sources.router import search_sources as _search_sources

    return _search_sources(query, sources=sources, client=client)


__all__ = ["SourceKind", "SourceQuery", "SourceRecord", "route_sources", "search_sources"]
