"""Small read helpers over the structured store.

Kept separate from `models.py` (schema) and `db.py` (engine/session) so the
query orchestration in `generate`/`retrieve` can ask narrow catalog questions
without re-implementing the same SQLModel selects.
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlmodel import select

from regwatch.store.db import get_engine, session_scope
from regwatch.store.models import PsgDocument, PsgVersion


def current_dosage_form_routes(
    normalized_name: str,
    *,
    dosage_form: str | None = None,
    route: str | None = None,
) -> list[tuple[str, str]]:
    """Distinct (dosage_form, route) combos for a product's CURRENT documents.

    A "current document" is a `psg_document` row that has at least one
    `psg_version` (i.e. has actually been ingested/indexed) — mirroring the
    retriever, which only ever surfaces chunks for versioned docs, so a combo we
    cannot answer about never appears as a clarify option. Any `dosage_form` /
    `route` already pinned by the caller narrows the enumeration, so once a
    combo is selected the guard collapses to one and stops re-clarifying.

    Combos with a missing dosage_form OR route are skipped: a guard keyed on a
    half-known combo would split same-drug docs that are actually answerable
    together. Returns a sorted, de-duplicated list (deterministic options).
    """
    engine = get_engine()
    inspector = inspect(engine)
    if not inspector.has_table("psg_document") or not inspector.has_table("psg_version"):
        return []

    with session_scope() as s:
        stmt = select(PsgDocument.dosage_form, PsgDocument.route).where(
            PsgDocument.normalized_name == normalized_name,
            PsgDocument.id.in_(  # type: ignore[union-attr]
                select(PsgVersion.psg_document_id).where(
                    PsgVersion.psg_document_id == PsgDocument.id
                )
            ),
        )
        if dosage_form:
            stmt = stmt.where(PsgDocument.dosage_form == dosage_form)
        if route:
            stmt = stmt.where(PsgDocument.route == route)
        rows = s.execute(stmt).all()

    combos = {
        (str(form), str(rte))
        for form, rte in rows
        if form not in (None, "") and rte not in (None, "")
    }
    return sorted(combos)
