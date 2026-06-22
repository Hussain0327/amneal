"""Small read helpers over the structured store.

Kept separate from `models.py` (schema) and `db.py` (engine/session) so the
query orchestration in `generate`/`retrieve` can ask narrow catalog questions
without re-implementing the same SQLModel selects.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect
from sqlmodel import col, select

from regwatch.common.logging import get_logger
from regwatch.store.db import get_engine, session_scope
from regwatch.store.models import PsgDocument, PsgVersion

log = get_logger(__name__)


@dataclass(frozen=True)
class CitationRecency:
    """FDA recency context for one cited PSG version/document.

    Both fields are raw DB values (recommended_date is the stored ISO string),
    left for the boundary to parse/serialize. Either may be None when the
    column is null or no matching row exists.
    """

    recommended_date: str | None
    diff_summary: str | None


@dataclass(frozen=True)
class RecencyIndex:
    """Result of one batched recency lookup, resolvable per citation.

    Keeps the version-level and document-level lookups separate so the boundary
    can resolve each citation by ``version_id`` first and fall back to
    ``doc_id`` — including when the version row is entirely absent.
    """

    by_version: dict[int, CitationRecency]
    doc_dates: dict[int, str | None]

    def resolve(self, version_id: int, doc_id: int) -> CitationRecency:
        """Recency for one citation: version row wins, else document fallback."""
        version_hit = self.by_version.get(version_id)
        diff = version_hit.diff_summary if version_hit else None
        recommended = version_hit.recommended_date if version_hit else None
        if recommended in (None, ""):
            recommended = self.doc_dates.get(doc_id)
        return CitationRecency(recommended_date=recommended, diff_summary=diff)


def fetch_citation_recency(version_ids: list[int], doc_ids: list[int]) -> RecencyIndex:
    """Batched recency lookup for cited passages — no N+1.

    A single round trip per table: one SELECT over ``psg_version`` for the
    requested version ids and one over ``psg_document`` for the requested doc
    ids. The returned :class:`RecencyIndex` resolves each citation by
    version_id, falling back to the document's ``recommended_date``.

    Failure behavior (CLAUDE.md: every DB call gets a defined failure): the
    surrounding ``session_scope`` already carries the per-connection
    ``statement_timeout`` on Postgres, so a stalled query self-cancels. ANY
    exception (missing tables on a fresh DB, timeout, connectivity) is swallowed
    to an empty index and logged — recency is best-effort context and must
    never raise into, or block, an already-validated answer.
    """
    empty = RecencyIndex(by_version={}, doc_dates={})
    if not version_ids and not doc_ids:
        return empty
    try:
        engine = get_engine()
        inspector = inspect(engine)
        if not inspector.has_table("psg_version") or not inspector.has_table("psg_document"):
            return empty
        doc_dates: dict[int, str | None] = {}
        by_version: dict[int, CitationRecency] = {}
        with session_scope() as s:
            if doc_ids:
                doc_dates = {
                    int(did): rec
                    for did, rec in s.execute(
                        select(PsgDocument.id, PsgDocument.recommended_date).where(
                            col(PsgDocument.id).in_(set(doc_ids))
                        )
                    ).all()
                }
            if version_ids:
                by_version = {
                    int(vid): CitationRecency(recommended_date=rec, diff_summary=diff)
                    for vid, rec, diff in s.execute(
                        select(
                            PsgVersion.id,
                            PsgVersion.recommended_date,
                            PsgVersion.diff_summary,
                        ).where(col(PsgVersion.id).in_(set(version_ids)))
                    ).all()
                }
    except Exception:
        log.warning("citation_recency_lookup_failed", exc_info=True)
        return empty

    return RecencyIndex(by_version=by_version, doc_dates=doc_dates)


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
            # "document has >= 1 version" — a non-correlated DISTINCT is clearer
            # than the self-correlated IN and lets the DB do the de-dup.
            PsgDocument.id.in_(  # type: ignore[union-attr]
                select(PsgVersion.psg_document_id).distinct()
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
