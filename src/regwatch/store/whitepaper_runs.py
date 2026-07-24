"""Persistence for White-Paper runs and the attributed analyst overlay.

Two-layer compliance model (INV-3/INV-5): ``create_run`` inserts the generated
payload ONCE and this module exposes no update for it -- the only mutable state
is the ``whitepaper_input`` overlay (attributed human text), the draft/final
status, and ``updated_at``. ``sections_sha256`` travels with the row so
finalize (and the server-side docx render) can re-verify that the stored
sections are byte-identical to what the audited populate produced.

Free functions per the house store pattern (each opens ``session_scope``);
read results are materialized into frozen dataclasses INSIDE the session so no
detached ORM row ever lazy-loads against a closed session. Importing
``result_fingerprint`` from the whitepaper domain module is dependencies-
pointing-inward: the store depends on the domain's canonical-bytes rule, never
the other way around.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, desc, func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from regwatch.store.db import session_scope
from regwatch.store.models import User, WhitepaperInput, WhitepaperRun
from regwatch.store.whitepaper_sources import normalize_appl_no

# The fingerprint's datetime->isoformat serializer is deliberately shared (the
# stored JSON must hold the exact strings the sha256 hashed, or the stored
# fingerprint could never re-verify). It is module-private in the populator;
# importing it here keeps ONE canonical serializer instead of a drifting copy.
from regwatch.whitepaper.populator import _fingerprint_default, result_fingerprint
from regwatch.whitepaper.template import spec_by_id

# Same bound as the populator's generated cell values (_MAX_VALUE_CHARS): an
# analyst overlay value is a cell value too, so it gets the same payload cap.
MAX_INPUT_CHARS = 4000

# Control characters minus newline/tab: C0 (except \t \n), DEL, and C1. \r is
# stripped too -- values normalize to \n-only line endings.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_STATUS_FINAL = "final"
_STATUS_DRAFT = "draft"


class WhitepaperRunError(Exception):
    """Base for run-store domain errors so the API can map them as a family."""


class RunNotFoundError(WhitepaperRunError):
    """No run with that id exists (API: 404)."""


class RunFinalizedError(WhitepaperRunError):
    """The run is final: the analyst layer is frozen (API: 409)."""


class RunNotFinalError(WhitepaperRunError):
    """Reopen requested on a run that is not final (API: 409)."""


class RunNotOwnedError(WhitepaperRunError):
    """Delete is creator-only; a finalized paper is a shared record (API: 403)."""


class InvalidCellError(WhitepaperRunError):
    """cell_id is not in template.CELL_SPECS (API: 422)."""


class InputTooLongError(WhitepaperRunError):
    """Cleaned value exceeds MAX_INPUT_CHARS (API: 422)."""


class ConcurrentEditError(WhitepaperRunError):
    """Two analysts hit the same empty cell at once: the second insert trips
    ``uq_whitepaper_input_run_cell``. A typed conflict (API: 409) instead of a
    naked 500 whose IntegrityError str() embeds the SQL parameter preview --
    the client retries and the retry takes the update path."""


class IntegrityMismatchError(WhitepaperRunError):
    """Stored sections no longer match sections_sha256 -- stored-data
    corruption, never a client error (API: 500 + Sentry)."""


@dataclass(frozen=True)
class RunSummary:
    """One list row -- explicitly WITHOUT the large JSON payloads."""

    id: int
    rld_name_input: str
    application_number: str
    application_type: str
    ingredient: str
    normalized_name: str
    status: str
    populated_count: int
    analyst_input_count: int
    verified_absent_count: int
    inputs_count: int
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InputView:
    """One overlay cell with its attribution, ready for the wire."""

    cell_id: str
    value: str
    author_user_id: int
    author: str | None
    updated_at: datetime


@dataclass(frozen=True)
class RunDetail:
    """The full run: verbatim generated payload + the analyst overlay."""

    id: int
    rld_name_input: str
    application_number: str
    application_type: str
    ingredient: str
    normalized_name: str
    spine: dict[str, Any]
    sections: list[dict[str, Any]]
    warnings: list[str]
    sections_sha256: str
    source_audit_id: int
    status: str
    populated_count: int
    analyst_input_count: int
    verified_absent_count: int
    created_by_user_id: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None
    finalized_by_user_id: int | None
    finalized_by: str | None
    inputs: list[InputView]


def _canonical_json(obj: Any) -> Any:
    """Round-trip through the fingerprint's own serializer.

    Populate results may carry datetime objects inside evidence; SQLAlchemy's
    JSON type cannot serialize them, and any OTHER serializer could produce
    strings the fingerprint never hashed. Round-tripping with
    ``_fingerprint_default`` stores exactly the values the sha256 saw, so
    ``result_fingerprint(stored sections) == stored sections_sha256`` holds
    forever (INV-5: the stored evidence strings stay verifiable).
    """
    return json.loads(json.dumps(obj, default=_fingerprint_default))


def _status_counts(sections: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(populated, analyst_input_required, verified_absent) over all cells."""
    counts = {"populated": 0, "analyst_input_required": 0, "verified_absent": 0}
    for section in sections:
        for cell in section.get("cells", []):
            status = cell.get("status")
            if status in counts:
                counts[status] += 1
    return (
        counts["populated"],
        counts["analyst_input_required"],
        counts["verified_absent"],
    )


def _clean_value(value: str) -> str:
    """Strip control characters (keeping newline/tab) and outer whitespace."""
    return _CONTROL_CHARS.sub("", value).strip()


def _require_cell(cell_id: str) -> None:
    if spec_by_id(cell_id) is None:
        raise InvalidCellError(f"unknown cell_id: {cell_id!r}")


def _run_or_raise(s: Session, run_id: int) -> WhitepaperRun:
    run = s.get(WhitepaperRun, run_id)
    if run is None:
        raise RunNotFoundError(f"whitepaper run {run_id} not found")
    return run


def _editable_run_or_raise(s: Session, run_id: int) -> WhitepaperRun:
    run = _run_or_raise(s, run_id)
    if run.status == _STATUS_FINAL:
        raise RunFinalizedError(f"whitepaper run {run_id} is final")
    return run


def create_run(*, user_id: int, rld_name_input: str, result: dict[str, Any]) -> int:
    """Insert a run from a ``build_whitepaper`` result; returns the run id.

    The generated payload is stored via the fingerprint's canonical round-trip
    (see ``_canonical_json``) and never updated afterwards (INV-3). The three
    status counts describe that immutable layer, so denormalizing them here
    cannot drift.
    """
    spine = result.get("spine") or {}
    sections = result.get("sections") or []
    warnings = result.get("warnings") or []
    audit_id = result.get("audit_id")
    if not isinstance(audit_id, int):
        raise ValueError("result carries no audit_id -- refuse to store an unaudited run")
    # Fingerprint the sections as handed over; the round-trip below is
    # fingerprint-preserving by construction (same serializer).
    sections_sha256 = result_fingerprint(sections)
    populated, analyst, absent = _status_counts(sections)
    run = WhitepaperRun(
        created_by_user_id=user_id,
        rld_name_input=rld_name_input,
        application_number=normalize_appl_no(str(spine.get("application_number") or "")),
        application_type=str(spine.get("application_type") or ""),
        ingredient=str(spine.get("ingredient") or ""),
        normalized_name=str(spine.get("normalized_name") or ""),
        spine_json=_canonical_json(spine),
        sections_json=_canonical_json(sections),
        warnings_json=_canonical_json(list(warnings)),
        sections_sha256=sections_sha256,
        source_audit_id=audit_id,
        populated_count=populated,
        analyst_input_count=analyst,
        verified_absent_count=absent,
    )
    with session_scope() as s:
        s.expire_on_commit = False
        s.add(run)
        s.flush()
        # Narrowing for mypy only; flush() has just assigned the PK.
        assert run.id is not None  # noqa: S101
        return run.id


def list_runs(
    *,
    limit: int,
    offset: int,
    application_number: str | None = None,
    normalized_name: str | None = None,
    status: str | None = None,
) -> tuple[list[RunSummary], int]:
    """Org-shared run list (no user filter), newest ``updated_at`` first.

    Selects explicit columns -- never the large JSON payloads -- plus the
    overlay-row count and the creator's display name in the same statement.
    ``total`` is a same-filter COUNT (the watch_latest pattern) so pagination
    stays truthful. An unparseable ``application_number`` filter raises
    ``ValueError`` (refuse over guess); callers map it to 422.
    """
    filters = []
    if application_number is not None:
        filters.append(
            col(WhitepaperRun.application_number) == normalize_appl_no(application_number)
        )
    if normalized_name is not None:
        filters.append(col(WhitepaperRun.normalized_name) == normalized_name)
    if status is not None:
        filters.append(col(WhitepaperRun.status) == status)
    with session_scope() as s:
        inputs_count = (
            sa_select(func.count())
            .select_from(WhitepaperInput)
            .where(col(WhitepaperInput.run_id) == col(WhitepaperRun.id))
            .scalar_subquery()
        )
        stmt = (
            sa_select(
                col(WhitepaperRun.id),
                col(WhitepaperRun.rld_name_input),
                col(WhitepaperRun.application_number),
                col(WhitepaperRun.application_type),
                col(WhitepaperRun.ingredient),
                col(WhitepaperRun.normalized_name),
                col(WhitepaperRun.status),
                col(WhitepaperRun.populated_count),
                col(WhitepaperRun.analyst_input_count),
                col(WhitepaperRun.verified_absent_count),
                inputs_count.label("inputs_count"),
                col(User.display_name).label("created_by"),
                col(WhitepaperRun.created_at),
                col(WhitepaperRun.updated_at),
            )
            .join(User, col(User.id) == col(WhitepaperRun.created_by_user_id))
            .where(*filters)
            .order_by(desc(col(WhitepaperRun.updated_at)), desc(col(WhitepaperRun.id)))
            .limit(limit)
            .offset(offset)
        )
        rows = s.execute(stmt).all()
        total = s.execute(
            sa_select(func.count()).select_from(WhitepaperRun).where(*filters)
        ).scalar_one()
        summaries = [
            RunSummary(
                id=row.id,
                rld_name_input=row.rld_name_input,
                application_number=row.application_number,
                application_type=row.application_type,
                ingredient=row.ingredient,
                normalized_name=row.normalized_name,
                status=row.status,
                populated_count=row.populated_count,
                analyst_input_count=row.analyst_input_count,
                verified_absent_count=row.verified_absent_count,
                inputs_count=row.inputs_count,
                created_by=row.created_by,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
        return summaries, int(total)


def get_run(run_id: int) -> RunDetail | None:
    """Run + overlay rows + author display names; None for a missing id.

    Two statements total (run joined to creator/finalizer, inputs joined to
    authors) -- never one query per overlay row.
    """
    finalizer = aliased(User)
    # Aliased-class attributes lose their SQLA expression type for mypy (they
    # resolve as the model's plain annotations), so re-type them explicitly.
    finalizer_name: Any = finalizer.display_name
    finalizer_id: Any = finalizer.id
    with session_scope() as s:
        row = s.execute(
            sa_select(WhitepaperRun, col(User.display_name), finalizer_name)
            .join(User, col(User.id) == col(WhitepaperRun.created_by_user_id))
            .outerjoin(finalizer, finalizer_id == col(WhitepaperRun.finalized_by_user_id))
            .where(col(WhitepaperRun.id) == run_id)
        ).first()
        if row is None:
            return None
        run, created_by, finalized_by = row
        input_rows = s.execute(
            sa_select(WhitepaperInput, col(User.display_name))
            .outerjoin(User, col(User.id) == col(WhitepaperInput.author_user_id))
            .where(col(WhitepaperInput.run_id) == run_id)
            .order_by(col(WhitepaperInput.cell_id))
        ).all()
        inputs = [
            InputView(
                cell_id=inp.cell_id,
                value=inp.value,
                author_user_id=inp.author_user_id,
                author=author_name,
                updated_at=inp.updated_at,
            )
            for inp, author_name in input_rows
        ]
        assert run.id is not None  # noqa: S101 - narrowing; row was loaded by PK
        return RunDetail(
            id=run.id,
            rld_name_input=run.rld_name_input,
            application_number=run.application_number,
            application_type=run.application_type,
            ingredient=run.ingredient,
            normalized_name=run.normalized_name,
            spine=run.spine_json,
            sections=run.sections_json,
            warnings=run.warnings_json,
            sections_sha256=run.sections_sha256,
            source_audit_id=run.source_audit_id,
            status=run.status,
            populated_count=run.populated_count,
            analyst_input_count=run.analyst_input_count,
            verified_absent_count=run.verified_absent_count,
            created_by_user_id=run.created_by_user_id,
            created_by=created_by,
            created_at=run.created_at,
            updated_at=run.updated_at,
            finalized_at=run.finalized_at,
            finalized_by_user_id=run.finalized_by_user_id,
            finalized_by=finalized_by,
            inputs=inputs,
        )


def upsert_input(*, run_id: int, cell_id: str, value: str, user_id: int) -> InputView | None:
    """Set (or clear) one analyst overlay cell; org-shared, attributed.

    The value is cleaned first (control characters stripped, whitespace
    trimmed); an EMPTY cleaned value means CLEAR -- the overlay row is deleted
    and ``None`` is returned, so a blank can never persist as analyst text
    (INV-5). The length cap applies AFTER cleaning. Every real mutation bumps
    ``run.updated_at`` so the org-shared list orders by activity.
    """
    _require_cell(cell_id)
    cleaned = _clean_value(value)
    if len(cleaned) > MAX_INPUT_CHARS:
        raise InputTooLongError(
            f"value is {len(cleaned)} chars after cleaning; max {MAX_INPUT_CHARS}"
        )
    with session_scope() as s:
        s.expire_on_commit = False
        run = _editable_run_or_raise(s, run_id)
        existing = s.scalars(
            select(WhitepaperInput).where(
                col(WhitepaperInput.run_id) == run_id,
                col(WhitepaperInput.cell_id) == cell_id,
            )
        ).first()
        now = datetime.now(UTC)
        if not cleaned:
            if existing is not None:
                s.delete(existing)
                run.updated_at = now
                s.add(run)
            return None
        row = existing or WhitepaperInput(
            run_id=run_id, cell_id=cell_id, value=cleaned, author_user_id=user_id
        )
        row.value = cleaned
        row.author_user_id = user_id
        row.updated_at = now
        s.add(row)
        run.updated_at = now
        s.add(run)
        try:
            s.flush()
        except IntegrityError as exc:
            # The unique constraint is the concurrency backstop: another
            # writer inserted this (run, cell) between our lookup and flush.
            # session_scope rolls back; the winner's value stands.
            raise ConcurrentEditError(
                f"whitepaper run {run_id} cell {cell_id!r} was edited concurrently; retry"
            ) from exc
        author = s.get(User, user_id)
        return InputView(
            cell_id=cell_id,
            value=cleaned,
            author_user_id=user_id,
            author=author.display_name if author is not None else None,
            updated_at=now,
        )


def clear_input(*, run_id: int, cell_id: str, user_id: int) -> bool:
    """Delete one overlay cell; True when a row existed. Org-shared like
    ``upsert_input`` (``user_id`` is the acting analyst, kept for the API's
    audit trail). A no-op clear does NOT bump ``updated_at`` -- nothing changed."""
    _require_cell(cell_id)
    with session_scope() as s:
        run = _editable_run_or_raise(s, run_id)
        existing = s.scalars(
            select(WhitepaperInput).where(
                col(WhitepaperInput.run_id) == run_id,
                col(WhitepaperInput.cell_id) == cell_id,
            )
        ).first()
        if existing is None:
            return False
        s.delete(existing)
        run.updated_at = datetime.now(UTC)
        s.add(run)
        return True


def finalize_run(*, run_id: int, user_id: int) -> None:
    """draft -> final: re-verify the stored fingerprint FIRST, then freeze.

    A mismatch means the stored generated layer was corrupted or tampered with
    (INV-3/INV-4) -- refuse to stamp a finalized record over it. Re-finalizing
    a final run raises ``RunFinalizedError`` so the original finalizer's
    attribution is never silently overwritten.
    """
    with session_scope() as s:
        run = _run_or_raise(s, run_id)
        if run.status == _STATUS_FINAL:
            raise RunFinalizedError(f"whitepaper run {run_id} is already final")
        if result_fingerprint(run.sections_json) != run.sections_sha256:
            raise IntegrityMismatchError(
                f"whitepaper run {run_id}: stored sections do not match sections_sha256"
            )
        now = datetime.now(UTC)
        run.status = _STATUS_FINAL
        run.finalized_at = now
        run.finalized_by_user_id = user_id
        run.updated_at = now
        s.add(run)


def reopen_run(*, run_id: int, user_id: int) -> None:
    """final -> draft: clears the finalize stamp (``user_id`` is the acting
    analyst; the API writes the audit row). Reopening a draft is a conflict --
    there is nothing to reopen."""
    with session_scope() as s:
        run = _run_or_raise(s, run_id)
        if run.status != _STATUS_FINAL:
            raise RunNotFinalError(f"whitepaper run {run_id} is not final")
        run.status = _STATUS_DRAFT
        run.finalized_at = None
        run.finalized_by_user_id = None
        run.updated_at = datetime.now(UTC)
        s.add(run)


def delete_run(*, run_id: int, user_id: int) -> None:
    """Creator-only, drafts-only delete; a finalized paper is a record.

    Overlay rows are deleted explicitly (no DB-level cascade, house style)
    inside the same transaction as the run row.
    """
    with session_scope() as s:
        run = _run_or_raise(s, run_id)
        if run.created_by_user_id != user_id:
            raise RunNotOwnedError(f"whitepaper run {run_id} was created by another user")
        if run.status == _STATUS_FINAL:
            raise RunFinalizedError(f"whitepaper run {run_id} is final")
        s.execute(delete(WhitepaperInput).where(col(WhitepaperInput.run_id) == run_id))
        s.delete(run)
