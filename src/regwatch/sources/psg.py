"""Local PSG source handler."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import desc, or_, select
from sqlmodel import col

from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.sources._utils import APPLICATION_PREFIXES, clean_application_number
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument, PsgVersion


def _bare_app_no(value: str | None) -> str | None:
    """The store's bare-digit form of an application number.

    ``PsgDocument.appl_no``/``rld_or_rs_number`` hold bare six-digit values
    (the crawler extracts digits only), so a prefixed query — "NDA020503" or
    the advertised "N020503", which ``clean_application_number`` normalizes to
    "NDA020503" — must be stripped back to digits or it silently matches
    nothing. Mirrors ``orange_book._orange_book_app_no``.
    """
    cleaned = clean_application_number(value)
    if cleaned is None:
        return None
    for prefix in APPLICATION_PREFIXES:
        if cleaned.startswith(prefix):
            return cleaned.removeprefix(prefix)
    return cleaned


class PsgHandler:
    source = SourceKind.PSG

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del client
        app_no = _bare_app_no(query.application_number)
        ingredient = canonical_name(query.active_ingredient or query.query_text)
        ingredient_stripped = stripped_name(query.active_ingredient or query.query_text)
        records: list[SourceRecord] = []
        with session_scope() as s:
            # One round trip: join each document to its latest version via a
            # correlated scalar subquery (ordered by captured_at, id — both
            # served by the psg_document_id index). On hosted Postgres the
            # old two-pass fetch (docs, then every version row for those
            # docs) pays a second network RTT and transfers the full version
            # history; this folds it into a single indexed query.
            latest_version_id = (
                select(col(PsgVersion.id))
                .where(col(PsgVersion.psg_document_id) == col(PsgDocument.id))
                .order_by(desc(col(PsgVersion.captured_at)), desc(col(PsgVersion.id)))
                .limit(1)
                .correlate(PsgDocument)
                .scalar_subquery()
            )
            stmt = select(PsgDocument, PsgVersion).outerjoin(
                PsgVersion, col(PsgVersion.id) == latest_version_id
            )
            if app_no:
                stmt = stmt.where(
                    or_(
                        col(PsgDocument.appl_no) == app_no,
                        col(PsgDocument.rld_or_rs_number).contains(app_no),
                    )
                )
            if query.active_ingredient:
                stmt = stmt.where(
                    or_(
                        col(PsgDocument.normalized_name) == ingredient,
                        col(PsgDocument.active_ingredient).ilike(f"%{query.active_ingredient}%"),
                    )
                )
            if query.dosage_form:
                stmt = stmt.where(
                    or_(
                        col(PsgDocument.dosage_form).is_(None),
                        col(PsgDocument.dosage_form).ilike(f"%{query.dosage_form}%"),
                    )
                )
            pairs: list[tuple[PsgDocument, PsgVersion | None]] = [
                (row[0], row[1]) for row in s.execute(stmt.limit(query.limit * 5))
            ]
            for doc, version in pairs:
                if app_no and app_no not in (doc.rld_or_rs_number or "") and doc.appl_no != app_no:
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
    if doc.appl_no:
        identifiers["appl_no"] = doc.appl_no
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
