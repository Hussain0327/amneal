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
    # One version row per revision: two overlapping ingest runs racing the same
    # content must collide here instead of double-recording one FDA change
    # (INV-4 -- a duplicate never-alerted row would re-alert it the next day).
    # Declared in metadata so create_all (the fresh-Postgres bootstrap) and
    # migration 0014 produce the identical index on both paths. A unique INDEX
    # (not a UniqueConstraint) because 0014 must add it to existing SQLite DBs
    # without a batch table rebuild.
    __table_args__ = (
        Index("uq_psg_version_doc_hash", "psg_document_id", "content_hash", unique=True),
    )

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


class FdaDocument(SQLModel, table=True):
    """Stable identity for one document in the authoritative FDA corpus.

    ``canonical_id`` is derived from an FDA-owned primary key (for example an
    ApplicationDocsID, an Orange Book row key, or a PSG application number),
    never from a title.  Mutable source bytes live in version rows.
    """

    __tablename__ = "fda_document"
    __table_args__ = (
        CheckConstraint(
            "source_family IN ('drugs_at_fda', 'action_package', 'psg', "
            "'fda_be_guidance', 'orange_book')",
            name="ck_fda_document_source_family",
        ),
        CheckConstraint(
            "shard_id IS NULL OR (shard_id >= 0 AND shard_id < 512)",
            name="ck_fda_document_shard_id",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    canonical_id: str = Field(index=True, unique=True)
    source_family: str = Field(index=True)
    document_type: str = Field(index=True)
    title: str
    source_url: str
    application_number: str | None = Field(default=None, index=True)
    product_number: str | None = Field(default=None, index=True)
    active_ingredient: str | None = None
    normalized_name: str | None = Field(default=None, index=True)
    brand_name: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    shard_id: int | None = Field(default=None, index=True)
    is_active: bool = Field(default=True, index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class FdaDocumentVersion(SQLModel, table=True):
    """Immutable captured bytes and parse facts for one FDA document revision."""

    __tablename__ = "fda_document_version"
    __table_args__ = (
        Index(
            "uq_fda_document_version_doc_hash",
            "fda_document_id",
            "content_hash",
            "processing_fingerprint",
            unique=True,
        ),
        CheckConstraint("byte_size >= 0", name="ck_fda_document_version_byte_size"),
        CheckConstraint("page_count >= 0", name="ck_fda_document_version_page_count"),
        CheckConstraint("chunk_count >= 0", name="ck_fda_document_version_chunk_count"),
        CheckConstraint(
            "chunk_status IN ('pending', 'complete', 'failed')",
            name="ck_fda_document_version_chunk_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    fda_document_id: int = Field(foreign_key="fda_document.id", index=True)
    content_hash: str = Field(index=True, min_length=64, max_length=64)
    processing_fingerprint: str = Field(index=True, min_length=64, max_length=64)
    source_updated_at: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    mime_type: str
    byte_size: int
    page_count: int
    chunk_count: int
    artifact_uri: str | None = None
    artifact_retained: bool = False
    acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    chunk_status: str = Field(default="pending", index=True)
    chunked_at: datetime | None = Field(default=None, index=True)
    chunk_error: str | None = None
    # Deprecated compatibility field from migration 0023. New workers write
    # artifact_uri, which may be file://, s3://, or discard://.
    artifact_path: str | None = None
    parse_engine: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())


class FdaVersionEmbeddingState(SQLModel, table=True):
    """Per-profile lifecycle for one immutable FDA document version."""

    __tablename__ = "fda_version_embedding_state"
    __table_args__ = (
        Index(
            "uq_fda_version_embedding_state_version_profile",
            "fda_version_id",
            "profile_id",
            unique=True,
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_fda_version_embedding_state_status",
        ),
        CheckConstraint(
            "expected_chunks >= 0",
            name="ck_fda_version_embedding_state_expected_chunks",
        ),
        CheckConstraint(
            "embedded_chunks >= 0 AND embedded_chunks <= expected_chunks",
            name="ck_fda_version_embedding_state_embedded_chunks",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    fda_version_id: int = Field(foreign_key="fda_document_version.id", index=True)
    profile_id: str = Field(index=True)
    expected_chunks: int = 0
    embedded_chunks: int = 0
    status: str = Field(default="pending", index=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class FdaCorpusManifest(SQLModel, table=True):
    """Durable pointer and discovery facts for one exact corpus manifest."""

    __tablename__ = "fda_corpus_manifest"
    __table_args__ = (
        CheckConstraint("document_count >= 0", name="ck_fda_corpus_manifest_document_count"),
    )

    sha256: str = Field(primary_key=True, min_length=64, max_length=64)
    schema_version: int = 1
    artifact_uri: str
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_retained: bool = True
    document_count: int
    complete_universe: bool = False
    source_snapshots_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    counts_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class FdaCorpusRun(SQLModel, table=True):
    """Auditable plan/sync ledger for the authoritative corpus."""

    __tablename__ = "fda_corpus_run"
    __table_args__ = (
        CheckConstraint("mode IN ('plan', 'sync')", name="ck_fda_corpus_run_mode"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_fda_corpus_run_status",
        ),
    )

    id: str = Field(primary_key=True)
    mode: str
    status: str = Field(default="running", index=True)
    manifest_sha256: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    completed_at: datetime | None = None
    expected_documents: int = 0
    discovered_documents: int = 0
    added_documents: int = 0
    revised_documents: int = 0
    unchanged_documents: int = 0
    error_documents: int = 0
    chunks_written: int = 0
    stats_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())


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
    # Wall time from turn start to this audit write, stamped by whichever
    # control plane owns the turn (Go persistTurn natively, Python ask() on the
    # relay/stream path). NULL = pre-migration row, or a writer that does not
    # measure — never 0, which a percentile would read as an instant turn.
    latency_ms: int | None = None


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
    __table_args__ = (
        Index("ix_chat_session_user_id_updated_at", "user_id", "updated_at"),
        # Declared in metadata (matching answer_feedback's ck_answer_feedback_rating
        # above) so create_all and migration 0021 produce the identical constraint.
        CheckConstraint("origin IN ('thread', 'assistant')", name="ck_chat_session_origin"),
    )

    id: str = Field(primary_key=True)
    user_id: str | None = Field(default=None, index=True)
    title: str | None = None
    active_filters_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    # Which surface owns this thread: "thread" is the analyst's real work
    # (shows up in the work rail's Threads list); "assistant" is the Research
    # Studio panel's own scratch conversation (kept, but never listed there --
    # issue #208). Set once, on session CREATE, by ensure_session; never
    # rewritten on an existing row (same rule active_filters_json follows).
    # server_default matches the Field default so create_all (bootstrap) and
    # migration 0021 (existing rows) agree on the same column shape.
    origin: str = Field(default="thread", sa_column_kwargs={"server_default": "thread"})
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


class WhitepaperRun(SQLModel, table=True):
    """A persisted White-Paper populate result -- the durable run of record.

    Two-layer compliance model (INV-3/INV-5): everything the populator produced
    (``spine_json``/``sections_json``/``warnings_json``) is IMMUTABLE after
    insert -- no code path updates it -- and ``sections_sha256`` is the
    fingerprint that render/finalize re-verify, so a stored run can never be
    silently altered. Attributed analyst text lives in ``whitepaper_input``,
    a separate table, so a manual cell's generated ``value`` stays ``None``
    forever and the human answer is visibly human.

    Spine keys are denormalized as plain columns for listing/filtering; the
    three status counts describe the immutable generated layer, so their
    denormalization cannot drift. Timestamps mirror ``WatchRun``: plain
    DateTime columns written from ``datetime.now(UTC)``, round-tripping as
    naive-UTC like every other timestamp in this schema.
    """

    __tablename__ = "whitepaper_run"
    # Declared in metadata so create_all (the fresh-Postgres bootstrap) and
    # migration 0013 produce identical constraints on both paths.
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'final')", name="ck_whitepaper_run_status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by_user_id: int = Field(foreign_key="user.id", index=True)
    # Spine keys denormalized for listing/filtering and Phase-3 staleness joins.
    rld_name_input: str
    application_number: str = Field(index=True)  # six digits, normalized
    application_type: str  # NDA | ANDA | BLA
    ingredient: str
    normalized_name: str = Field(index=True)
    # The full generated payload, immutable after insert (INV-3).
    spine_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    sections_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    warnings_json: list[str] = Field(default_factory=list, sa_column=_json_column())
    sections_sha256: str
    # Ties the run to its existing QueryLog audit row (mode="whitepaper").
    source_audit_id: int = Field(index=True)
    # Workflow: draft -> final. Finalize freezes the analyst layer too.
    status: str = Field(default="draft", index=True)
    finalized_at: datetime | None = None
    finalized_by_user_id: int | None = Field(default=None, foreign_key="user.id")
    # Status counts over the immutable generated layer, computed once at create.
    populated_count: int
    analyst_input_count: int
    verified_absent_count: int


class WhitepaperInput(SQLModel, table=True):
    """Attributed analyst overlay -- one CURRENT value per (run, cell).

    The overlay is the ONLY mutable layer of a run (INV-3): human text with an
    author, never mixed into ``sections_json``. Clearing a cell deletes the
    row -- an empty value never persists as a confident blank (INV-5). Updates
    overwrite in place; edit history is deferred until someone asks for it.
    """

    __tablename__ = "whitepaper_input"
    # Declared in metadata so create_all (the fresh-Postgres bootstrap) and
    # migration 0013 produce identical constraints on both paths.
    __table_args__ = (UniqueConstraint("run_id", "cell_id", name="uq_whitepaper_input_run_cell"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="whitepaper_run.id", index=True)
    cell_id: str  # validated in code against template.CELL_SPECS
    value: str  # non-empty; clearing deletes the row
    author_user_id: int = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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


class EvalRun(SQLModel, table=True):
    """One COMPLETED eval run -- the durable retrieval-quality ledger.

    Third of the run ledgers, after ``WatchRun`` and ``DeficiencyRun``. It
    exists because a scorecard is only evidence when it is comparable, and
    until now a scorecard lived in terminal output plus an optional ``--out``
    file on a CI run that ages out. "Did the chunker change hurt recall?" was
    unanswerable from the repository.

    ``gold_set_sha256`` is what keeps the ledger honest: two runs over
    different gold sets are not comparable, and a trend line that silently
    splices them is worse than no trend line. ``dirty`` records the same thing
    for code -- a run from a dirty tree is not reproducible from its commit.

    ``passed`` is stored rather than recomputed from the metric columns, so
    changing a threshold later cannot retroactively rewrite which historical
    runs are recorded as having cleared the gate.

    This ledger only ever grows, one row per eval invocation, and is read as
    "the last N runs for one arm" -- hence the (profile_id, created_at) index.
    """

    __tablename__ = "eval_run"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile_id: str
    commit_sha: str
    dirty: bool
    gold_set_sha256: str
    n_items: int
    corpus_chunks: int
    corpus_docs: int
    recall_at_k: float
    mrr: float
    citation_precision: float
    faithfulness: float
    fact_recall: float
    refusal_accuracy: float
    passed: bool
    # Full provenance: fingerprint + prompt manifest + scorecard, including the
    # per-question traces. Those are read by a human debugging one regression,
    # never aggregated in SQL, so they stay in the document rather than earning
    # a second table and a join.
    artifact_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())

    __table_args__ = (Index("ix_eval_run_profile_created", "profile_id", "created_at"),)


class DeficiencyRun(SQLModel, table=True):
    """One deficiency-analysis job over an uploaded submission PDF.

    Unlike ``WhitepaperRun`` (which persists only completed results), this row
    is created BEFORE the work runs: the upload endpoint answers 202 with the
    row id and the UI polls it, so pending/running/failed are first-class
    states. The analysis itself executes as a background task inside the API
    process (a deliberate, documented exception to "the Fly image never parses
    a PDF" -- DECISIONS.md 2026-07-30), so a process restart can strand a row
    in pending/running forever; readers apply the ``deficiency_run_stale_minutes``
    cutoff at read time (``store.deficiency_runs.effective_status``) instead of
    trusting a janitor that would itself need the missing durable queue.

    ``report_json`` is the full FaultReport payload, immutable after the one
    ``complete`` transition; ``audit_id`` ties the run to its QueryLog row
    (mode="defpredict", INV-6). The uploaded PDF is NOT stored -- only its
    sha256, so a report can be matched to a document without the document
    ever persisting.

    Timestamps mirror ``WatchRun``: plain DateTime columns written from
    ``datetime.now(UTC)``, round-tripping as naive-UTC like the rest of this
    schema.
    """

    __tablename__ = "deficiency_run"
    # Declared in metadata so create_all (the fresh-Postgres bootstrap) and
    # migration 0019 produce identical constraints on both paths.
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_deficiency_run_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    created_by_user_id: int = Field(foreign_key="user.id", index=True)
    filename: str  # display label only; sanitized at the API boundary
    sha256: str  # of the uploaded bytes; the document itself is never stored
    # What submitted this run. NULL is the original PDF-upload path, which is
    # org-shared; "studio" is a Compliance Studio check, which is private to
    # its creator. The two surfaces share this table but never share
    # visibility, so every read path filters on it.
    source: str | None = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    page_count: int | None = None
    fault_count: int | None = None
    error: str | None = None
    # Full FaultReport payload; NULL until the run completes, immutable after.
    report_json: dict[str, Any] | None = Field(default=None, sa_column=_json_column())
    # QueryLog row for this run (mode="defpredict"); set on the terminal
    # transition so every LLM-content path stays audit-covered (INV-6).
    audit_id: int | None = Field(default=None, index=True)
