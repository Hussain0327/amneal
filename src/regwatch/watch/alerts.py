"""Alerts: emit a cited summary of matched changes (INV-4).

We only emit alerts for PSG versions whose `id` exists in the database. If a
match references a version we never fetched, the alert is skipped — never
fabricated.

Delivery for the POC:
  - On-disk JSONL digest at `data/processed/alerts/digest-YYYY-MM-DD.jsonl`
  - Returned as structured data so the API / UI can show it

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
from sqlmodel import select

from regwatch.common.logging import get_logger
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.watch.matcher import WatchMatch

log = get_logger(__name__)


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
        doc_rows = list(s.scalars(select(PsgDocument).where(PsgDocument.normalized_name != "")))
        candidates = [d for d in doc_rows if (d.source_url or "").endswith(f"PSG_{appl_no}.pdf")]
        if not candidates:
            return None
        doc = candidates[0]
        if doc.id is None:
            raise RuntimeError("psg_document row missing id")
        ver_rows = list(
            s.scalars(
                select(PsgVersion)
                .where(PsgVersion.psg_document_id == doc.id)
                .order_by(desc(PsgVersion.captured_at))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        if not ver_rows:
            return None
        v = ver_rows[0]
        if v.id is None:
            raise RuntimeError("psg_version row missing id")
        return doc.id, v.id, v.diff_summary, v.captured_at.isoformat()


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


def write_digest(alerts: list[Alert], *, when: date | None = None) -> Path:
    """Write the alerts to a JSONL digest file in `data/processed/alerts/`."""
    s = get_settings()
    s.ensure_dirs()
    when = when or datetime.now(UTC).date()
    out_dir = s.processed_dir / "alerts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"digest-{when.isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(asdict(a)) + "\n")
    log.info("digest_written", path=str(path), n=len(alerts))
    return path


def latest_digest_records(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent digest file's records (UI feed)."""
    s = get_settings()
    out_dir = s.processed_dir / "alerts"
    if not out_dir.exists():
        return []
    files = sorted(out_dir.glob("digest-*.jsonl"))
    if not files:
        return []
    latest = files[-1]
    records: list[dict[str, Any]] = []
    with latest.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
            if len(records) >= limit:
                break
    return records
