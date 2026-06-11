"""Write-through persistence for White-Paper structured sources.

The populator fetches Orange Book rows and the DailyMed SPL resolution, then
writes them through here so every populated cell can cite a durable row that
carries ``last_fetched_at`` as source freshness (INV-5). Tables hold raw rows
only — paragraph classification and eligibility never persist (INV-3).

Write-through semantics:

- Orange Book rows REPLACE the previous snapshot for one APPLICATION — number
  AND type. NDA and ANDA rows sharing the same six digits are different
  applications, so a typed replace never wipes the other type's snapshot.
  Legacy rows persisted before the type column existed (``appl_type IS NULL``)
  are retired by the first typed replace for their number.
- SPL documents UPSERT by ``setid`` (the natural key DailyMed assigns).
- :func:`persist_whitepaper_snapshot` runs every requested replace/upsert in
  ONE transaction: either the full snapshot lands or none of it does — a
  mid-write failure can never leave half a snapshot as durable evidence.

Application numbers are stored in the Orange Book 6-digit form (``020503``),
matching ``psg_document.appl_no`` and the OB files themselves; application
types are stored as the full ``NDA``/``ANDA``/``BLA`` prefix.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_
from sqlmodel import Session, col, select

from regwatch.common.text_normalize import canonical_name
from regwatch.store.db import session_scope
from regwatch.store.models import ObExclusivity, ObPatent, ObProduct, SplDocument

_APP_PREFIX = re.compile(r"^(?:NDA|ANDA|BLA)", re.IGNORECASE)

# Orange Book Appl_Type column letters -> the full application-type prefix.
_TYPE_BY_LETTER = {"N": "NDA", "A": "ANDA", "B": "BLA"}


def normalize_appl_no(value: str) -> str:
    """Normalize any accepted form (``NDA 020503``, ``N020503``) to 6 digits.

    Raises on an unparseable value instead of silently storing nothing —
    "no rows" must always mean "queried and absent" (INV-5).
    """
    digits = re.sub(r"\D", "", _APP_PREFIX.sub("", value.strip()))
    if not digits:
        raise ValueError(f"unparseable application number: {value!r}")
    return digits.zfill(6)


def normalize_appl_type(value: str) -> str:
    """Normalize an application type (``N``/``NDA``/``nda``) to the full prefix.

    Raises on an unparseable value — an untyped replace-snapshot could wipe a
    same-digit application of another type (the A4 failure mode).
    """
    cleaned = value.strip().upper()
    if cleaned in ("NDA", "ANDA", "BLA"):
        return cleaned
    full = _TYPE_BY_LETTER.get(cleaned)
    if full is None:
        raise ValueError(f"unparseable application type: {value!r}")
    return full


def _row_appl_type(raw: str | None, fallback: str) -> str:
    """The row's own normalized type when parseable, else the snapshot's."""
    if raw:
        try:
            return normalize_appl_type(raw)
        except ValueError:
            return fallback
    return fallback


@dataclass(frozen=True)
class ObSnapshot:
    """One application's Orange Book rows with per-rowset freshness (INV-5).

    ``patent_rows``/``exclusivity_rows`` may be ``None`` when that rowset's
    fetch degraded (the file was absent from the downloaded ZIP): the replace
    for that rowset is SKIPPED and the previous durable snapshot survives — a
    degraded fetch never wipes durable provenance rows. An empty sequence, by
    contrast, means "queried and absent" and replaces as usual.
    """

    application_number: str
    appl_type: str
    product_rows: Sequence[Mapping[str, str]]
    patent_rows: Sequence[Mapping[str, str]] | None
    exclusivity_rows: Sequence[Mapping[str, str]] | None
    products_fetched_at: datetime
    patents_fetched_at: datetime
    exclusivities_fetched_at: datetime
    source_url: str | None = None


@dataclass(frozen=True)
class SplSnapshot:
    """The DailyMed SPL resolution to upsert by ``setid``."""

    setid: str
    appl_no: str | None
    title: str | None
    published: str | None
    source_url: str | None
    fetched_at: datetime


def persist_whitepaper_snapshot(
    *, ob: ObSnapshot | None = None, spl: SplSnapshot | None = None
) -> None:
    """Persist the requested snapshots in ONE transaction (all-or-nothing).

    A mid-write failure rolls back every replace/upsert in this call — the
    previous durable snapshot survives intact and no partial rows are stored.
    """
    if ob is None and spl is None:
        return
    with session_scope() as s:
        s.expire_on_commit = False
        if ob is not None:
            appl_no = normalize_appl_no(ob.application_number)
            appl_type = normalize_appl_type(ob.appl_type)
            _replace_ob_products(
                s,
                appl_no,
                appl_type,
                ob.product_rows,
                fetched_at=ob.products_fetched_at,
                source_url=ob.source_url,
            )
            # None = that rowset's fetch degraded (file absent from the ZIP):
            # skip its replace so the previous durable snapshot survives.
            if ob.patent_rows is not None:
                _replace_ob_patents(
                    s,
                    appl_no,
                    appl_type,
                    ob.patent_rows,
                    fetched_at=ob.patents_fetched_at,
                    source_url=ob.source_url,
                )
            if ob.exclusivity_rows is not None:
                _replace_ob_exclusivities(
                    s,
                    appl_no,
                    appl_type,
                    ob.exclusivity_rows,
                    fetched_at=ob.exclusivities_fetched_at,
                    source_url=ob.source_url,
                )
        if spl is not None:
            _upsert_spl_document(s, spl)


def persist_ob_products(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    appl_type: str,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObProduct]:
    """Replace the persisted ``products.txt`` snapshot for one application."""
    with session_scope() as s:
        s.expire_on_commit = False
        return _replace_ob_products(
            s,
            normalize_appl_no(application_number),
            normalize_appl_type(appl_type),
            rows,
            fetched_at=fetched_at,
            source_url=source_url,
        )


def persist_ob_patents(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    appl_type: str,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObPatent]:
    """Replace the persisted ``patent.txt`` snapshot for one application."""
    with session_scope() as s:
        s.expire_on_commit = False
        return _replace_ob_patents(
            s,
            normalize_appl_no(application_number),
            normalize_appl_type(appl_type),
            rows,
            fetched_at=fetched_at,
            source_url=source_url,
        )


def persist_ob_exclusivities(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    appl_type: str,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObExclusivity]:
    """Replace the persisted ``exclusivity.txt`` snapshot for one application."""
    with session_scope() as s:
        s.expire_on_commit = False
        return _replace_ob_exclusivities(
            s,
            normalize_appl_no(application_number),
            normalize_appl_type(appl_type),
            rows,
            fetched_at=fetched_at,
            source_url=source_url,
        )


def persist_spl_document(
    *,
    setid: str,
    appl_no: str | None,
    title: str | None,
    published: str | None,
    source_url: str | None,
    fetched_at: datetime,
) -> SplDocument:
    """Upsert the persisted SPL resolution by ``setid`` (its natural key)."""
    snapshot = SplSnapshot(
        setid=setid,
        appl_no=appl_no,
        title=title,
        published=published,
        source_url=source_url,
        fetched_at=fetched_at,
    )
    with session_scope() as s:
        s.expire_on_commit = False
        return _upsert_spl_document(s, snapshot)


def _replace_ob_products(
    s: Session,
    appl_no: str,
    appl_type: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None,
) -> list[ObProduct]:
    out: list[ObProduct] = []
    s.execute(
        delete(ObProduct).where(
            col(ObProduct.appl_no) == appl_no,
            or_(col(ObProduct.appl_type) == appl_type, col(ObProduct.appl_type).is_(None)),
        )
    )
    for row in rows:
        ingredient = row.get("ingredient") or None
        record = ObProduct(
            appl_no=row.get("appl_no") or appl_no,
            product_no=row.get("product_no") or "",
            appl_type=_row_appl_type(row.get("appl_type"), appl_type),
            ingredient=ingredient,
            normalized_name=canonical_name(ingredient) if ingredient else None,
            trade_name=row.get("trade_name") or None,
            dosage_form_route=row.get("dosage_form_route") or None,
            strength=row.get("strength") or None,
            rld=row.get("rld") or None,
            rs=row.get("rs") or None,
            te_code=row.get("te_code") or None,
            approval_date=row.get("approval_date") or None,
            applicant=row.get("applicant") or None,
            applicant_full_name=row.get("applicant_full_name") or None,
            source_url=source_url,
            last_fetched_at=fetched_at,
        )
        s.add(record)
        out.append(record)
    s.flush()
    return out


def _replace_ob_patents(
    s: Session,
    appl_no: str,
    appl_type: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None,
) -> list[ObPatent]:
    out: list[ObPatent] = []
    s.execute(
        delete(ObPatent).where(
            col(ObPatent.appl_no) == appl_no,
            or_(col(ObPatent.appl_type) == appl_type, col(ObPatent.appl_type).is_(None)),
        )
    )
    for row in rows:
        record = ObPatent(
            appl_no=row.get("appl_no") or appl_no,
            product_no=row.get("product_no") or None,
            appl_type=_row_appl_type(row.get("appl_type"), appl_type),
            patent_no=row.get("patent_no") or "",
            patent_expire_date=row.get("patent_expire_date") or None,
            drug_substance_flag=row.get("drug_substance_flag") or None,
            drug_product_flag=row.get("drug_product_flag") or None,
            patent_use_code=row.get("patent_use_code") or None,
            delist_flag=row.get("delist_flag") or None,
            submission_date=row.get("submission_date") or None,
            source_url=source_url,
            last_fetched_at=fetched_at,
        )
        s.add(record)
        out.append(record)
    s.flush()
    return out


def _replace_ob_exclusivities(
    s: Session,
    appl_no: str,
    appl_type: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None,
) -> list[ObExclusivity]:
    out: list[ObExclusivity] = []
    s.execute(
        delete(ObExclusivity).where(
            col(ObExclusivity.appl_no) == appl_no,
            or_(
                col(ObExclusivity.appl_type) == appl_type,
                col(ObExclusivity.appl_type).is_(None),
            ),
        )
    )
    for row in rows:
        record = ObExclusivity(
            appl_no=row.get("appl_no") or appl_no,
            product_no=row.get("product_no") or None,
            appl_type=_row_appl_type(row.get("appl_type"), appl_type),
            exclusivity_code=row.get("exclusivity_code") or "",
            exclusivity_date=row.get("exclusivity_date") or None,
            source_url=source_url,
            last_fetched_at=fetched_at,
        )
        s.add(record)
        out.append(record)
    s.flush()
    return out


def _upsert_spl_document(s: Session, snapshot: SplSnapshot) -> SplDocument:
    existing = s.execute(select(SplDocument).where(SplDocument.setid == snapshot.setid))
    row = existing.scalars().first() or SplDocument(setid=snapshot.setid)
    row.appl_no = normalize_appl_no(snapshot.appl_no) if snapshot.appl_no else None
    row.title = snapshot.title
    row.published = snapshot.published
    row.source_url = snapshot.source_url
    row.last_fetched_at = snapshot.fetched_at
    s.add(row)
    s.flush()
    return row
