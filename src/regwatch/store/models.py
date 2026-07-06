"""SQLModel definitions for the structured store (Section 9).

These tables hold the *verified* facts: products on the watchlist, PSG
documents, their versions, extracted BE requirements, and the audit log.
Embeddings live in Chroma — see store/vector_store.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _json_column() -> Column[Any]:
    """A JSON column that becomes JSONB on Postgres (K3).

    Same Python types at every call site; SQLite (and any other dialect)
    keeps plain JSON. Each sa_column must be a fresh Column instance.
    """
    return Column(JSON().with_variant(JSONB(), "postgresql"))


class Product(SQLModel, table=True):
    """A target product on the company watchlist (INV-5: verified sources only)."""

    __tablename__ = "product"

    id: int | None = Field(default=None, primary_key=True)
    active_ingredient: str
    normalized_name: str = Field(index=True)
    dosage_form: str | None = None
    route: str | None = None
    rld_name: str | None = None
    rld_application_number: str | None = None
    company_status: str | None = None  # approved | tentative | pipeline
    source: str  # drugsfda | anda_letter | manual
    source_url: str | None = None
    on_watchlist: bool = Field(default=True)
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PsgDocument(SQLModel, table=True):
    """A PSG document as currently published by FDA."""

    __tablename__ = "psg_document"

    id: int | None = Field(default=None, primary_key=True)
    active_ingredient: str
    normalized_name: str = Field(index=True)
    dosage_form: str | None = None
    route: str | None = None
    appl_no: str | None = Field(default=None, index=True, unique=True)
    rld_or_rs_number: str | None = None
    psg_type: str  # draft | final
    recommended_date: str | None = None  # ISO date
    source_url: str
    pdf_path: str | None = None
    content_hash: str = Field(index=True)
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PsgVersion(SQLModel, table=True):
    """A captured version of a PSG document. New rows on every content change."""

    __tablename__ = "psg_version"

    id: int | None = Field(default=None, primary_key=True)
    psg_document_id: int = Field(foreign_key="psg_document.id", index=True)
    content_hash: str = Field(index=True)
    recommended_date: str | None = None
    parsed_text_path: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diff_summary: str | None = None  # cited summary of changes vs prior version


class BeRequirement(SQLModel, table=True):
    """Extracted BE requirements from a PSG version (each field carries a citation)."""

    __tablename__ = "be_requirement"

    id: int | None = Field(default=None, primary_key=True)
    psg_document_id: int = Field(foreign_key="psg_document.id", index=True)
    version_id: int = Field(foreign_key="psg_version.id", index=True)
    study_type: str | None = None
    study_design: str | None = None
    strengths: str | None = None
    dissolution: str | None = None
    waiver_conditions: str | None = None
    additional_notes: str | None = None
    fields_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    citations_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())


class User(SQLModel, table=True):
    """An authenticated analyst. Accounts are provisioned via the CLI — no self-signup."""

    __tablename__ = "user"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)  # stored lowercased
    password_hash: str  # bcrypt
    display_name: str
    role: str = Field(default="analyst")  # analyst | admin
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthSession(SQLModel, table=True):
    """A server-side login session. Only the sha256 of the cookie token is stored."""

    __tablename__ = "auth_session"

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    last_seen_at: datetime | None = None


class QueryLog(SQLModel, table=True):
    """Every query, its retrieved sources, the answer, and whether it refused (INV-6)."""

    __tablename__ = "query_log"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    session_id: str | None = Field(default=None, index=True)
    turn_id: str | None = Field(default=None, index=True)
    user_id: str | None = Field(default=None, index=True)
    mode: str  # qa | assemble | watch
    query_text: str
    retrieved_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    answer_text: str
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    refused: bool = False
    status: str | None = None
    route_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    model_name: str
    # H3 token/cost accounting for the dominant (synthesizer) LLM call. NULL =
    # no LLM call happened, or the provider didn't report usage / has no price
    # in the settings table — never a guessed number.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class AnswerFeedback(SQLModel, table=True):
    """An analyst's thumbs up/down on one answered Q&A turn (H4).

    One row per (audit_id, user_id) — re-rating replaces. These rows are the
    candidate pool for future eval gold-set items.
    """

    __tablename__ = "answer_feedback"
    # Declared in metadata so create_all (the Postgres bootstrap) and the
    # alembic migration produce the same constraints on both paths.
    __table_args__ = (
        UniqueConstraint("audit_id", "user_id", name="uq_answer_feedback_audit_user"),
        CheckConstraint("rating IN (-1, 1)", name="ck_answer_feedback_rating"),
    )

    id: int | None = Field(default=None, primary_key=True)
    audit_id: int = Field(foreign_key="query_log.id", index=True)
    user_id: str = Field(index=True)
    rating: int  # -1 (thumbs down) | 1 (thumbs up); CHECK-enforced
    comment: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChatSession(SQLModel, table=True):
    """A durable conversational thread.

    Conversation memory helps resolve follow-up wording, but it is never treated
    as FDA evidence. `active_filters_json` stores only deterministic context such
    as the currently selected product filter.
    """

    __tablename__ = "chat_session"
    # GET /sessions orders by updated_at within a user; on hosted Postgres the
    # composite index keeps that page a single index scan. Declared in metadata
    # (not ad-hoc DDL) so create_all, alembic autogenerate, and migration
    # 0007 all agree on the schema.
    __table_args__ = (Index("ix_chat_session_user_id_updated_at", "user_id", "updated_at"),)

    id: str = Field(primary_key=True)
    user_id: str | None = Field(default=None, index=True)
    title: str | None = None
    active_filters_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class ObProduct(SQLModel, table=True):
    """A persisted Orange Book ``Products.txt`` row (White-Paper provenance, INV-5).

    The populator writes-through on fetch so a cell can cite the durable row and
    carry its ``last_fetched_at`` as source freshness. Raw rows only — paragraph
    classification / eligibility are never stored (INV-3).
    """

    __tablename__ = "ob_product"

    id: int | None = Field(default=None, primary_key=True)
    appl_no: str = Field(index=True)
    product_no: str = Field(index=True)
    appl_type: str | None = None
    ingredient: str | None = None
    normalized_name: str | None = Field(default=None, index=True)
    trade_name: str | None = None
    dosage_form_route: str | None = None
    strength: str | None = None
    rld: str | None = None
    rs: str | None = None
    te_code: str | None = None
    approval_date: str | None = None
    applicant: str | None = None
    applicant_full_name: str | None = None
    source_url: str | None = None
    last_fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ObPatent(SQLModel, table=True):
    """A persisted Orange Book ``patent.txt`` row. Raw rows only (INV-3)."""

    __tablename__ = "ob_patent"

    id: int | None = Field(default=None, primary_key=True)
    appl_no: str = Field(index=True)
    product_no: str | None = None
    appl_type: str | None = Field(default=None, index=True)
    patent_no: str = Field(index=True)
    patent_expire_date: str | None = None
    drug_substance_flag: str | None = None
    drug_product_flag: str | None = None
    patent_use_code: str | None = None
    delist_flag: str | None = None
    submission_date: str | None = None
    source_url: str | None = None
    last_fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ObExclusivity(SQLModel, table=True):
    """A persisted Orange Book ``exclusivity.txt`` row. Raw rows only (INV-3)."""

    __tablename__ = "ob_exclusivity"

    id: int | None = Field(default=None, primary_key=True)
    appl_no: str = Field(index=True)
    product_no: str | None = None
    appl_type: str | None = Field(default=None, index=True)
    exclusivity_code: str = Field(index=True)
    exclusivity_date: str | None = None
    source_url: str | None = None
    last_fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SplDocument(SQLModel, table=True):
    """A persisted DailyMed SPL document resolution (White-Paper provenance)."""

    __tablename__ = "spl_document"

    id: int | None = Field(default=None, primary_key=True)
    setid: str = Field(index=True, unique=True)
    appl_no: str | None = Field(default=None, index=True)
    title: str | None = None
    published: str | None = None
    source_url: str | None = None
    last_fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChatMessage(SQLModel, table=True):
    """One user or assistant turn inside a chat session."""

    __tablename__ = "chat_message"

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="chat_session.id", index=True)
    turn_id: str = Field(index=True)
    role: str  # user | assistant
    content: str
    status: str | None = None
    model_name: str | None = None
    audit_id: int | None = Field(default=None, index=True)
    # Tier-2 history persistence: the route reason + the human-readable
    # interpretation behind the turn's status, so a rehydrated turn shows WHY we
    # answered/declined/clarified (mirrors QAResult.reason / .interpretation).
    reason: str | None = None
    interpretation: str | None = None
    filters_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    # Next-step affordances of the turn, persisted so they survive a reload:
    # clarify = re-runnable disambiguation options; related = inert "related, not
    # an answer" pointers for the refuse family (same {label, query, filters}
    # shape as the wire ClarifyOptionOut).
    clarify_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    related_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class Alert(SQLModel, table=True):
    """A durable Watch alert (INV-4: every alert refers to a real psg_version).

    Persisted by ``write_digest`` so the feed survives Fly redeploys — the JSONL
    digest lives on the container's ephemeral disk (no [[mounts]]), so it is
    wiped on every recycle. The (psg_version_id, listing_appl_no, product_id)
    unique key makes re-running ``regwatch watch`` the same day idempotent: the
    repeat insert is a no-op (ON CONFLICT DO NOTHING), never a duplicate row.

    ``captured_at`` is the ISO STRING the Alert dataclass already carried (the
    source PSG version's captured_at, re-emitted verbatim on the wire and
    re-parsed by GET /watch/latest's ``since`` filter), so it stays a string to
    avoid tz round-trip drift. ``created_at`` is a separate server clock used
    only to order the feed by recency.
    """

    __tablename__ = "alert"
    # Declared in metadata (not ad-hoc DDL) so create_all (the Postgres
    # bootstrap) and migration 0009 produce identical constraints + indexes.
    __table_args__ = (
        UniqueConstraint(
            "psg_version_id",
            "listing_appl_no",
            "product_id",
            name="uq_alert_version_listing_product",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    active_ingredient: str
    listing_appl_no: str = Field(index=True)
    listing_psg_type: str
    psg_document_id: int = Field(index=True)
    psg_version_id: int = Field(index=True)
    captured_at: str  # ISO string, mirrors the Alert dataclass / JSONL
    diff_summary: str | None = None
    confidence: float
    rationale: str
    source_url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class WatchRun(SQLModel, table=True):
    """One COMPLETED Watch pipeline run -- the durable "the cron actually ran" ledger.

    Before this table the only record of a run was the JSONL digest on the
    GitHub cron runner's ephemeral disk, so the UI could not distinguish a
    quiet day (recent run, zero alerts) from a cron that has been dead for a
    week -- both looked like an empty feed. One row per run that COMPLETES,
    including completed-with-errors runs (an errored-but-completed run is a
    real run; INV-4 wants the truthful record). A run that RAISES (e.g. the
    zero-listings crawl guard) writes nothing: the cron's dead-man's-switch
    owns that failure class, and a row here would misreport an aborted run.

    Timestamps mirror ``Alert.created_at`` exactly: plain DateTime columns (no
    ``timezone=True``) written from ``datetime.now(UTC)``, so values round-trip
    as naive-UTC like every other timestamp in this schema. ``digest_date`` is
    the YYYY-MM-DD of the JSONL digest file actually written, or None when the
    errored-run branch skipped that write (never claim an artifact that does
    not exist).
    """

    __tablename__ = "watch_run"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime
    # latest_watch_run reads newest-by-finished_at; the index keeps that read a
    # single index scan as the ledger grows one row per cron day forever.
    finished_at: datetime = Field(index=True)
    listings: int
    matched: int
    added: int
    revised: int
    unchanged: int
    errors: int
    alerts: int
    digest_date: str | None = None
