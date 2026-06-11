"""Typed FDA source-handler contracts.

Source handlers return structured FDA evidence rows. They do not synthesize
answers and they do not call an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import httpx


class SourceKind(StrEnum):
    PSG = "psg"
    ORANGE_BOOK = "orange_book"
    DRUGSFDA = "drugsfda"
    SHORTAGE = "shortage"
    NDC = "ndc"
    REMS = "rems"
    DAILYMED = "dailymed"


@dataclass(frozen=True)
class SourceQuery:
    query_text: str = ""
    active_ingredient: str | None = None
    brand_name: str | None = None
    application_number: str | None = None
    ndc: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    limit: int = 10


@dataclass(frozen=True)
class SourceRecord:
    source: SourceKind
    title: str
    source_url: str
    identifiers: dict[str, str] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class SourceHandler(Protocol):
    source: SourceKind

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        """Return source-specific structured rows for `query`."""
