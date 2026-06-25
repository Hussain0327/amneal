"""Alerts: emit a cited summary of matched changes (INV-4).

We only emit alerts for PSG versions whose `id` exists in the database. If a
match references a version we never fetched, the alert is skipped — never
fabricated.

Delivery for the POC:
  - DURABLE: rows in the `alert` table. This is the source of truth for
    GET /watch/latest and survives Fly redeploys (the JSONL digest below lives
    on the container's ephemeral disk and is wiped on every recycle).
  - On-disk JSONL digest at `data/processed/alerts/digest-YYYY-MM-DD.jsonl`,
    kept for backward-compat: it is the `WatchRunResult.digest_path` artifact
    and the truthful "ran, no changes" empty-file record.

Future deliveries (email, Slack) plug in here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from config.settings import get_settings
from sqlalchemy import desc
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select

from regwatch.common.logging import get_logger
from regwatch.store.db import session_scope
from regwatch.store.models import Alert as AlertRow
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.watch.matcher import WatchMatch

log = get_logger(__name__)

# The fields persisted/returned for one alert — the wire contract for
# GET /watch/latest (lib/api.ts AlertRecord). `id`/`created_at` are storage
# bookkeeping and never cross the wire.
_RECORD_FIELDS = (
    "product_id",
    "active_ingredient",
    "listing_appl_no",
    "listing_psg_type",
    "psg_document_id",
    "psg_version_id",
    "captured_at",
    "diff_summary",
    "confidence",
    "rationale",
    "source_url",
)


@dataclass
class Alert:
    product_id: int
    active_ingredient: str
    listing_appl_no: str
    listing_psg_type: str
    psg_document_id: int
    psg_version_id: int
    captured_at: str
    diff_summary: str | None
    confidence: float
    rationale: str
    source_url: str


def _fetch_version_for_listing(appl_no: str) -> tuple[int, int, str | None, str] | None:
    """Return (doc_id, latest_version_id, diff_summary, captured_at_iso) or None."""
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument).where(PsgDocument.appl_no == appl_no)).first()
        if doc is None:
            return None
        if doc.id is None:
            raise RuntimeError("psg_document row missing id")
        ver_rows = list(
            s.scalars(
                select(PsgVersion)
                .where(PsgVersion.psg_document_id == doc.id)
                # id tie-break so a captured_at collision is deterministic
                # (matches assemble/dossier.py's latest-version helper).
                .order_by(desc(PsgVersion.captured_at), desc(PsgVersion.id))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        if not ver_rows:
            return None
        v = ver_rows[0]
        if v.id is None:
            raise RuntimeError("psg_version row missing id")
        return doc.id, v.id, v.diff_summary, v.captured_at.isoformat()


def appl_nos_without_alert(appl_nos: list[str]) -> set[str]:
    """Of these appl_nos, the ones whose LATEST psg_version has NO alert row.

    INV-4 crash recovery: ``ingest_listing`` commits the psg_version row and
    THEN writes chunks/BE in separate (non-atomic) stores. If that second step
    crashes, the version is durably committed but no alert was ever built, and
    every later run reads the matching content_hash as ``unchanged`` and so
    never re-enters the alert path -- a permanent silent miss. We re-derive the
    gap directly from the durable tables (LEFT JOIN alert ON psg_version_id):
    a committed-latest-version with no alert row is exactly that missed case.

    Returns only appl_nos that HAVE a latest version (a doc that was never
    fetched has nothing to alert on) AND that version has no alert row. The
    caller feeds these back through ``build_alerts`` (which re-verifies the
    version) so the missed alert is finally produced; ``_persist_alerts`` is
    idempotent, so re-surfacing an already-alerted version is a no-op.
    """
    if not appl_nos:
        return set()
    missed: set[str] = set()
    with session_scope() as s:
        for appl_no in appl_nos:
            doc = s.scalars(select(PsgDocument).where(PsgDocument.appl_no == appl_no)).first()
            if doc is None or doc.id is None:
                continue
            ver = s.scalars(
                select(PsgVersion)
                .where(PsgVersion.psg_document_id == doc.id)
                .order_by(desc(PsgVersion.captured_at), desc(PsgVersion.id))  # type: ignore[arg-type]
                .limit(1)
            ).first()
            if ver is None or ver.id is None:
                continue
            has_alert = s.scalars(
                select(AlertRow.id).where(AlertRow.psg_version_id == ver.id).limit(1)
            ).first()
            if has_alert is None:
                missed.add(appl_no)
    return missed


def build_alerts(matches: list[WatchMatch]) -> list[Alert]:
    """Build verified alerts (INV-4: every alert refers to a real DB version)."""
    alerts: list[Alert] = []
    for m in matches:
        version = _fetch_version_for_listing(m.listing.appl_no)
        if version is None:
            log.info("alert_skipped_no_version", appl_no=m.listing.appl_no)
            continue
        doc_id, version_id, diff_summary, captured_at = version
        product_id = m.product.get("id")
        if not isinstance(product_id, int):
            log.info("alert_skipped_no_product_id", appl_no=m.listing.appl_no)
            continue
        alerts.append(
            Alert(
                product_id=product_id,
                active_ingredient=m.product.get("active_ingredient") or "",
                listing_appl_no=m.listing.appl_no,
                listing_psg_type=m.listing.psg_type,
                psg_document_id=doc_id,
                psg_version_id=version_id,
                captured_at=captured_at,
                diff_summary=diff_summary,
                confidence=m.confidence,
                rationale=m.rationale,
                source_url=m.listing.pdf_url,
            )
        )
    return alerts


def digest_path(when: date | None = None) -> Path:
    """The digest file path for a given date (computed, NOT written)."""
    s = get_settings()
    when = when or datetime.now(UTC).date()
    return s.processed_dir / "alerts" / f"digest-{when.isoformat()}.jsonl"


def _persist_alerts(alerts: list[Alert]) -> int:
    """UPSERT alerts into the durable `alert` table; ON CONFLICT DO NOTHING.

    The unique key (psg_version_id, listing_appl_no, product_id) makes a
    same-day re-run a no-op instead of a duplicate (INV-4 idempotence). A real
    revision creates a NEW psg_version row, so it gets a new version_id and a
    new alert. Returns the number of rows actually inserted (conflicts skipped).
    """
    if not alerts:
        return 0
    # Core inserts don't run SQLModel's Python-side `created_at` default, so set
    # it explicitly. One run's alerts share a timestamp; created_at DESC, id DESC
    # then orders the feed deterministically.
    now = datetime.now(UTC)
    rows = [{**asdict(a), "created_at": now} for a in alerts]
    with session_scope() as s:
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            stmt: Any = (
                pg_insert(AlertRow.__table__)  # type: ignore[attr-defined]
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_alert_version_listing_product")
            )
        else:
            stmt = (
                sqlite_insert(AlertRow.__table__)  # type: ignore[attr-defined]
                .values(rows)
                .on_conflict_do_nothing(
                    index_elements=["psg_version_id", "listing_appl_no", "product_id"]
                )
            )
        result = s.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


def write_digest(alerts: list[Alert], *, when: date | None = None) -> Path:
    """Persist alerts to the DB (durable, idempotent) AND write the JSONL digest.

    The DB is the source of truth for GET /watch/latest. The JSONL write is
    retained for backward-compat: it is the `WatchRunResult.digest_path`
    artifact and the truthful "ran, no changes" empty-file record.
    """
    inserted = _persist_alerts(alerts)
    s = get_settings()
    s.ensure_dirs()
    path = digest_path(when)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(asdict(a)) + "\n")
    log.info("digest_written", path=str(path), n=len(alerts), inserted=inserted)
    return path


def latest_digest_records(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent durable alerts (UI feed), newest first.

    Reads from the `alert` table — durable across redeploys. The returned dict
    keys match the Alert dataclass / former JSONL shape exactly, so the
    /watch/latest wire contract and lib/api.ts AlertRecord are unaffected.
    """
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(AlertRow)
                .order_by(desc(AlertRow.created_at), desc(AlertRow.id))  # type: ignore[arg-type]
                .limit(limit)
            )
        )
        # Materialize INSIDE the session: expire_on_commit detaches these rows
        # on scope exit, so reading attributes afterward would lazy-load against
        # a closed session. The returned dicts are plain values, ORM-free.
        return [{field: getattr(r, field) for field in _RECORD_FIELDS} for r in rows]
