"""SQLModel definitions for the structured store (Section 9).

These tables hold the *verified* facts: products on the watchlist, PSG
documents, their versions, extracted BE requirements, and the audit log.
Embeddings live in Chroma — see store/vector_store.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


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
    fields_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    citations_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


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
    retrieved_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    answer_text: str
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    refused: bool = False
    status: str | None = None
    route_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    model_name: str


class ChatSession(SQLModel, table=True):
    """A durable conversational thread.

    Conversation memory helps resolve follow-up wording, but it is never treated
    as FDA evidence. `active_filters_json` stores only deterministic context such
    as the currently selected product filter.
    """

    __tablename__ = "chat_session"

    id: str = Field(primary_key=True)
    user_id: str | None = Field(default=None, index=True)
    title: str | None = None
    active_filters_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


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
    filters_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
