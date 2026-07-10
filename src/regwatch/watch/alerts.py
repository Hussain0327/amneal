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
from sqlalchemy import desc, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col, select

from regwatch.common.logging import get_logger
from regwatch.store.db import session_scope
from regwatch.store.models import Alert as AlertRow
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.watch.matcher import WatchMatch, product_id

log = get_logger(__name__)

# The fields persisted/returned for one alert — the wire contract for
# GET /watch/latest (lib/api.ts AlertRecord). `id`/`created_at` are storage
# bookkeeping and never cross the wire. The wire ALSO carries `change_kind`
# ("new" | "revised"), which is NOT stored: latest_digest_records derives it
# from psg_version history at read time, so it is deliberately absent here.
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


def pairs_without_alert(pairs: list[tuple[str, int]]) -> set[tuple[str, int]]:
    """Of these (appl_no, product_id) pairs, the ones whose LATEST psg_version
    has NO alert row FOR THAT PRODUCT.

    INV-4 crash recovery, made PER-PRODUCT. Alerts are built by this watch
    stage AFTER ``ingest_listing`` returns, outside the ingest transaction (in
    BOTH storage modes -- the Postgres-mode atomic commit covers version + doc
    + chunks + BE, never alerts). A crash between the version commit and alert
    persistence leaves the version durably committed with no alert, and every
    later run reads the matching content_hash as ``unchanged`` and so never
    re-enters the alert path -- a permanent silent miss.

    The check is per (psg_version_id, listing_appl_no, product_id) -- the SAME
    granularity as the durable ``uq_alert_version_listing_product`` key -- NOT
    per version. A version already alerted for ONE product must still alert for
    a SECOND product added to the watchlist later (combos, multi-form holdings,
    or simply a growing watchlist); a per-version check silently drops that
    newly-watched product forever, since its steady state is ``unchanged``. The
    caller feeds these back through ``build_alerts`` (which re-verifies the
    version); ``_persist_alerts`` is idempotent, so re-surfacing an
    already-alerted (version, appl, product) is a no-op.
    """
    if not pairs:
        return set()
    missed: set[tuple[str, int]] = set()
    with session_scope() as s:
        # Cache the latest-version lookup per appl_no: several products can share
        # one listing and only the per-product alert check differs between them.
        # (A single LEFT JOIN would collapse the remaining per-pair queries -- a
        # deferred cron-only micro-optimization, not a correctness concern.)
        latest_version: dict[str, int | None] = {}
        # `pid`, not `product_id`: that name is now the shared coercion helper
        # imported from matcher and must not be shadowed here.
        for appl_no, pid in pairs:
            if appl_no not in latest_version:
                doc = s.scalars(select(PsgDocument).where(PsgDocument.appl_no == appl_no)).first()
                if doc is None or doc.id is None:
                    latest_version[appl_no] = None
                else:
                    ver = s.scalars(
                        select(PsgVersion)
                        .where(PsgVersion.psg_document_id == doc.id)
                        .order_by(desc(PsgVersion.captured_at), desc(PsgVersion.id))  # type: ignore[arg-type]
                        .limit(1)
                    ).first()
                    latest_version[appl_no] = ver.id if ver is not None else None
            version_id = latest_version[appl_no]
            if version_id is None:
                continue
            has_alert = s.scalars(
                select(AlertRow.id)
                .where(AlertRow.psg_version_id == version_id)
                .where(AlertRow.listing_appl_no == appl_no)
                .where(AlertRow.product_id == pid)
                .limit(1)
            ).first()
            if has_alert is None:
                missed.add((appl_no, pid))
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
        pid = product_id(m)
        if pid is None:
            log.info("alert_skipped_no_product_id", appl_no=m.listing.appl_no)
            continue
        alerts.append(
            Alert(
                product_id=pid,
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
        # Count via RETURNING, not cursor rowcount: psycopg v3 reports -1 for
        # this multi-VALUES insert form, and ON CONFLICT DO NOTHING emits only
        # the rows that actually landed - driver-independent on both dialects.
        result = s.execute(stmt.returning(AlertRow.__table__.c.id))  # type: ignore[attr-defined]
        return len(result.scalars().all())


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


def _since_key(since: datetime) -> str:
    """Normalize ``since`` to the format actually stored in ``captured_at``.

    Stored values are timezone-NAIVE isoformat strings: the writer is
    ``PsgVersion.captured_at.isoformat()`` and that column is a naive DateTime
    (no ``timezone=True``), so round-tripped values carry NO ``+00:00`` suffix.
    A tz-aware ``since.isoformat()`` DOES end in ``+00:00``, which sorts AFTER
    its exact stored equal (the stored string is a strict byte-prefix), so the
    documented inclusive at/after boundary would silently drop the boundary
    row. Comparing naive-to-naive keeps the lexicographic ``>=`` chronological
    for the writer's format AND inclusive at exact equality.
    """
    if since.tzinfo is None:
        # Treat naive input as already-UTC (mirrors the API's _as_utc):
        # astimezone() on a naive value would assume LOCAL time and shift it.
        return since.isoformat()
    return since.astimezone(UTC).replace(tzinfo=None).isoformat()


def count_digest_records(*, since: datetime | None = None) -> int:
    """COUNT of durable alerts matching the same ``since`` filter as
    ``latest_digest_records``.

    Split out so GET /watch/latest can report the TRUE total next to a bounded
    page -- ``len(page)`` alone reads as "that's everything" and rows past the
    cap would otherwise be invisible through the API forever.
    """
    with session_scope() as s:
        stmt = select(func.count()).select_from(AlertRow)
        if since is not None:
            stmt = stmt.where(AlertRow.captured_at >= _since_key(since))
        return int(s.scalars(stmt).one())


def latest_digest_records(
    limit: int = 100, *, offset: int = 0, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Return one page of durable alerts (UI feed), newest first.

    Reads from the `alert` table -- durable across redeploys. The returned dict
    keys match the Alert dataclass / former JSONL shape, plus a derived
    ``change_kind`` ("new" | "revised") -- lib/api.ts AlertRecord mirrors both.

    ``since`` keeps only alerts whose ``captured_at`` is at/after it (INCLUSIVE
    at the boundary), applied IN SQL BEFORE the limit so a genuinely-recent
    alert can never be dropped by the row cap (the prior code limited by
    ``created_at`` then filtered by ``captured_at`` in Python, which could hide
    recent rows). ``captured_at`` is a string column of NAIVE-UTC isoformat
    values (see ``_since_key``), so ``since`` is normalized to that same shape
    before the lexicographic compare. ``offset`` pages the feed;
    ``count_digest_records`` (same filter) reports the full matching total.
    """
    with session_scope() as s:
        stmt = select(AlertRow)
        if since is not None:
            stmt = stmt.where(AlertRow.captured_at >= _since_key(since))
        ordered = (
            stmt.order_by(desc(AlertRow.created_at), desc(AlertRow.id))  # type: ignore[arg-type]
            .offset(offset)
            .limit(limit)
        )
        rows = list(s.scalars(ordered))
        # `change_kind` is derived STRUCTURALLY: an alert is "new" iff no
        # psg_version row for the same document precedes its version (smaller
        # id). The prose diff_summary can degrade to the initial-version marker
        # when a revision's prior parsed text is gone (the prod cron runner's
        # disk is ephemeral), so kind must never be inferred from prose. Not
        # persisted -- no migration; the DB answers it authoritatively at read
        # time. One grouped query covers the whole page.
        doc_ids = {r.psg_document_id for r in rows}
        first_version_id: dict[int, int] = {}
        if doc_ids:
            grouped = s.execute(
                select(PsgVersion.psg_document_id, func.min(PsgVersion.id))
                .where(col(PsgVersion.psg_document_id).in_(doc_ids))
                .group_by(col(PsgVersion.psg_document_id))
            ).all()
            first_version_id = {doc_id: min_id for doc_id, min_id in grouped}
        # Materialize INSIDE the session: expire_on_commit detaches these rows
        # on scope exit, so reading attributes afterward would lazy-load against
        # a closed session. The returned dicts are plain values, ORM-free.
        records: list[dict[str, Any]] = []
        for r in rows:
            rec: dict[str, Any] = {field: getattr(r, field) for field in _RECORD_FIELDS}
            first_id = first_version_id.get(r.psg_document_id)
            rec["change_kind"] = (
                "new" if first_id is None or r.psg_version_id <= first_id else "revised"
            )
            records.append(rec)
        return records
