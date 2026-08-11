"""Small read helpers over the structured store.

Kept separate from `models.py` (schema) and `db.py` (engine/session) so the
query orchestration in `generate`/`retrieve` can ask narrow catalog questions
without re-implementing the same SQLModel selects.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, inspect
from sqlalchemy import select as sa_select
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


def count_documents(normalized_names: Iterable[str]) -> int:
    """Total ``psg_document`` rows across ``normalized_names`` -- ONE round trip.

    Aggregate sibling of the generator's per-product doc count: the meta answer
    needs the corpus-wide total, and a per-product COUNT loop is an N+1 (~1.4k
    sequential round trips against remote Postgres on the full catalog). An
    empty name set returns 0 without touching the DB.
    """
    names = {n for n in normalized_names if n}
    if not names:
        return 0
    with session_scope() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(PsgDocument)
                .where(col(PsgDocument.normalized_name).in_(names))
            )
            or 0
        )


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
        # Case-insensitive on both sides (works on SQLite and Postgres): the
        # catalog stores FDA listing casing ("Aerosol, Metered") while a
        # hand-typed UI filter arrives in whatever casing the user chose -- a
        # case-sensitive pin would silently enumerate zero combos.
        if dosage_form:
            stmt = stmt.where(func.lower(PsgDocument.dosage_form) == dosage_form.lower())
        if route:
            stmt = stmt.where(func.lower(PsgDocument.route) == route.lower())
        rows = s.execute(stmt).all()

    combos = {
        (str(form), str(rte))
        for form, rte in rows
        if form not in (None, "") and rte not in (None, "")
    }
    return sorted(combos)


@dataclass(frozen=True)
class PsgCatalogEntry:
    """One ``psg_document`` catalog row as the reference-library rail needs it.

    Plain values, not ORM rows, so callers never trip over session expiry
    (the same trap pipeline.py documents around detached instances).
    """

    id: int
    active_ingredient: str
    normalized_name: str
    dosage_form: str | None
    route: str | None
    appl_no: str | None
    psg_type: str
    recommended_date: str | None
    source_url: str


def list_psg_documents(*, limit: int, offset: int) -> list[PsgCatalogEntry]:
    """One deterministic page of the PSG catalog.

    ``coalesce`` pins NULL dosage_form/route ordering across dialects, and
    ``id`` is the final tiebreak so limit/offset paging is gapless and
    duplicate-free even under concurrent ingest inserts.
    """
    with session_scope() as s:
        rows = s.execute(
            # sqlalchemy's select over col()-wrapped attributes: sqlmodel's
            # typed select stops at four columns, and the bare class attributes
            # read as plain values to mypy.
            sa_select(
                col(PsgDocument.id),
                col(PsgDocument.active_ingredient),
                col(PsgDocument.normalized_name),
                col(PsgDocument.dosage_form),
                col(PsgDocument.route),
                col(PsgDocument.appl_no),
                col(PsgDocument.psg_type),
                col(PsgDocument.recommended_date),
                col(PsgDocument.source_url),
            )
            .order_by(
                col(PsgDocument.normalized_name).asc(),
                func.coalesce(PsgDocument.dosage_form, "").asc(),
                func.coalesce(PsgDocument.route, "").asc(),
                col(PsgDocument.psg_type).asc(),
                col(PsgDocument.id).asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
    return [
        PsgCatalogEntry(
            id=int(row[0]),
            active_ingredient=row[1],
            normalized_name=row[2],
            dosage_form=row[3],
            route=row[4],
            appl_no=row[5],
            psg_type=row[6],
            recommended_date=row[7],
            source_url=row[8],
        )
        for row in rows
    ]


def count_psg_documents() -> int:
    """Total ``psg_document`` rows -- the listing response's ``total``."""
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(PsgDocument)) or 0)


@dataclass(frozen=True)
class PsgDocumentDetail:
    """One ``psg_document`` row plus the id of its current version.

    "Current" is computed the same way every other reader computes it
    (captured_at DESC, id DESC) rather than stored, because there is no
    is_current column and inventing a second rule here would let the studio
    render a version retrieval does not quote. ``current_version_id`` is None
    for a document whose version row is missing, which the caller must treat
    as "nothing to render".
    """

    id: int
    active_ingredient: str
    dosage_form: str | None
    route: str | None
    appl_no: str | None
    rld_or_rs_number: str | None
    psg_type: str
    recommended_date: str | None
    source_url: str
    current_version_id: int | None


def fetch_psg_document_detail(doc_id: int) -> PsgDocumentDetail | None:
    """One PSG's descriptive fields and current version id, or None."""
    with session_scope() as s:
        row = s.execute(
            sa_select(
                col(PsgDocument.id),
                col(PsgDocument.active_ingredient),
                col(PsgDocument.dosage_form),
                col(PsgDocument.route),
                col(PsgDocument.appl_no),
                col(PsgDocument.rld_or_rs_number),
                col(PsgDocument.psg_type),
                col(PsgDocument.recommended_date),
                col(PsgDocument.source_url),
            ).where(col(PsgDocument.id) == doc_id)
        ).first()
        if row is None:
            return None
        version_id = s.execute(
            sa_select(col(PsgVersion.id))
            .where(col(PsgVersion.psg_document_id) == doc_id)
            .order_by(col(PsgVersion.captured_at).desc(), col(PsgVersion.id).desc())
            .limit(1)
        ).scalar()
    return PsgDocumentDetail(
        id=int(row[0]),
        active_ingredient=row[1],
        dosage_form=row[2],
        route=row[3],
        appl_no=row[4],
        rld_or_rs_number=row[5],
        psg_type=row[6],
        recommended_date=row[7],
        source_url=row[8],
        current_version_id=int(version_id) if version_id is not None else None,
    )


@dataclass(frozen=True)
class PsgPdfSource:
    """The fields the PDF-serving route needs from one ``psg_document`` row."""

    id: int
    appl_no: str | None
    source_url: str
    pdf_path: str | None
    content_hash: str


def fetch_psg_pdf_source(doc_id: int) -> PsgPdfSource | None:
    """The PDF-locating fields for one document, or None when the id is unknown."""
    with session_scope() as s:
        row = s.execute(
            sa_select(
                col(PsgDocument.id),
                col(PsgDocument.appl_no),
                col(PsgDocument.source_url),
                col(PsgDocument.pdf_path),
                col(PsgDocument.content_hash),
            ).where(col(PsgDocument.id) == doc_id)
        ).first()
    if row is None:
        return None
    return PsgPdfSource(
        id=int(row[0]),
        appl_no=row[1],
        source_url=row[2],
        pdf_path=row[3],
        content_hash=row[4],
    )
