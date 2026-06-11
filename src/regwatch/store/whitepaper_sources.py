"""Write-through persistence for White-Paper structured sources.

The populator fetches Orange Book rows and the DailyMed SPL resolution, then
writes them through here so every populated cell can cite a durable row that
carries ``last_fetched_at`` as source freshness (INV-5). Tables hold raw rows
only — paragraph classification and eligibility never persist (INV-3).

Write-through semantics:

- Orange Book rows REPLACE the application's previous snapshot, so a row that
  FDA delisted does not linger as stale evidence;
- SPL documents UPSERT by ``setid`` (the natural key DailyMed assigns).

Application numbers are stored in the Orange Book 6-digit form (``020503``),
matching ``psg_document.appl_no`` and the OB files themselves.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete
from sqlmodel import col, select

from regwatch.common.text_normalize import canonical_name
from regwatch.store.db import session_scope
from regwatch.store.models import ObExclusivity, ObPatent, ObProduct, SplDocument

_APP_PREFIX = re.compile(r"^(?:NDA|ANDA|BLA)", re.IGNORECASE)


def normalize_appl_no(value: str) -> str:
    """Normalize any accepted form (``NDA 020503``, ``N020503``) to 6 digits.

    Raises on an unparseable value instead of silently storing nothing —
    "no rows" must always mean "queried and absent" (INV-5).
    """
    digits = re.sub(r"\D", "", _APP_PREFIX.sub("", value.strip()))
    if not digits:
        raise ValueError(f"unparseable application number: {value!r}")
    return digits.zfill(6)


def persist_ob_products(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObProduct]:
    """Replace the persisted ``products.txt`` snapshot for one application."""
    appl_no = normalize_appl_no(application_number)
    out: list[ObProduct] = []
    with session_scope() as s:
        s.expire_on_commit = False
        s.execute(delete(ObProduct).where(col(ObProduct.appl_no) == appl_no))
        for row in rows:
            ingredient = row.get("ingredient") or None
            record = ObProduct(
                appl_no=row.get("appl_no") or appl_no,
                product_no=row.get("product_no") or "",
                appl_type=row.get("appl_type") or None,
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


def persist_ob_patents(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObPatent]:
    """Replace the persisted ``patent.txt`` snapshot for one application."""
    appl_no = normalize_appl_no(application_number)
    out: list[ObPatent] = []
    with session_scope() as s:
        s.expire_on_commit = False
        s.execute(delete(ObPatent).where(col(ObPatent.appl_no) == appl_no))
        for row in rows:
            record = ObPatent(
                appl_no=row.get("appl_no") or appl_no,
                product_no=row.get("product_no") or None,
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


def persist_ob_exclusivities(
    application_number: str,
    rows: Sequence[Mapping[str, str]],
    *,
    fetched_at: datetime,
    source_url: str | None = None,
) -> list[ObExclusivity]:
    """Replace the persisted ``exclusivity.txt`` snapshot for one application."""
    appl_no = normalize_appl_no(application_number)
    out: list[ObExclusivity] = []
    with session_scope() as s:
        s.expire_on_commit = False
        s.execute(delete(ObExclusivity).where(col(ObExclusivity.appl_no) == appl_no))
        for row in rows:
            record = ObExclusivity(
                appl_no=row.get("appl_no") or appl_no,
                product_no=row.get("product_no") or None,
                exclusivity_code=row.get("exclusivity_code") or "",
                exclusivity_date=row.get("exclusivity_date") or None,
                source_url=source_url,
                last_fetched_at=fetched_at,
            )
            s.add(record)
            out.append(record)
        s.flush()
    return out


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
    with session_scope() as s:
        s.expire_on_commit = False
        existing = s.execute(select(SplDocument).where(SplDocument.setid == setid))
        row = existing.scalars().first() or SplDocument(setid=setid)
        row.appl_no = normalize_appl_no(appl_no) if appl_no else None
        row.title = title
        row.published = published
        row.source_url = source_url
        row.last_fetched_at = fetched_at
        s.add(row)
        s.flush()
    return row
