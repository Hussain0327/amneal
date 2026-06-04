"""Local PSG source handler."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import desc
from sqlmodel import select

from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.sources._utils import clean_application_number
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument, PsgVersion


class PsgHandler:
    source = SourceKind.PSG

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del client
        app_no = clean_application_number(query.application_number)
        ingredient = canonical_name(query.active_ingredient or query.query_text)
        ingredient_stripped = stripped_name(query.active_ingredient or query.query_text)
        records: list[SourceRecord] = []
        with session_scope() as s:
            rows = list(s.scalars(select(PsgDocument)))
            for doc in rows:
                if app_no and app_no not in (doc.rld_or_rs_number or ""):
                    continue
                if query.active_ingredient and not (
                    doc.normalized_name == ingredient
                    or stripped_name(doc.active_ingredient or "") == ingredient_stripped
                ):
                    continue
                if (
                    query.dosage_form
                    and doc.dosage_form
                    and query.dosage_form.lower() not in doc.dosage_form.lower()
                ):
                    continue
                version = s.scalars(
                    select(PsgVersion)
                    .where(PsgVersion.psg_document_id == doc.id)
                    .order_by(desc(PsgVersion.captured_at), desc(PsgVersion.id))  # type: ignore[arg-type]
                    .limit(1)
                ).first()
                records.append(_record_from_doc(doc, version))
                if len(records) >= query.limit:
                    break
        return records


def _record_from_doc(doc: PsgDocument, version: PsgVersion | None) -> SourceRecord:
    identifiers: dict[str, str] = {}
    if doc.id is not None:
        identifiers["psg_document_id"] = str(doc.id)
    if version and version.id is not None:
        identifiers["psg_version_id"] = str(version.id)
    if doc.rld_or_rs_number:
        identifiers["rld_or_rs_number"] = doc.rld_or_rs_number
    fields: dict[str, Any] = {
        "active_ingredient": doc.active_ingredient,
        "normalized_name": doc.normalized_name,
        "dosage_form": doc.dosage_form,
        "route": doc.route,
        "psg_type": doc.psg_type,
        "recommended_date": doc.recommended_date,
        "content_hash": doc.content_hash,
    }
    if version:
        fields["latest_version_captured_at"] = version.captured_at.isoformat()
        fields["latest_diff_summary"] = version.diff_summary
    return SourceRecord(
        source=SourceKind.PSG,
        title=f"PSG: {doc.active_ingredient}",
        source_url=doc.source_url,
        identifiers=identifiers,
        fields=fields,
    )
