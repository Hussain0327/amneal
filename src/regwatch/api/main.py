"""FastAPI surface.

This is the clean boundary the IT/AI team will wrap or replace. Every
response is reproducible in Postman from a `.env` and a running instance.

Endpoints (per spec §10.16):
    POST   /auth/login     — issue a session cookie
    POST   /auth/logout    — revoke the session cookie
    GET    /auth/me        — current user
    POST   /query          — grounded Q&A (auth)
    POST   /feedback       — thumbs up/down on one of the caller's answers (auth)
    POST   /sources/search — structured FDA source lookup (auth)
    POST   /assemble       — build a cited dossier for a target product (auth)
    POST   /whitepaper     — populate the CRA White Paper (RLD + appl no) (auth)
    POST   /whitepaper/docx — render a returned /whitepaper result as .docx (auth)
    GET    /watch/latest   — recent alerts (auth)
    GET    /products       — list watchlist (auth)
    POST   /products       — add manual product (auth)
    GET    /sessions       — the caller's chat sessions (auth)
    GET    /sessions/{id}  — one chat session with messages (auth)
    DELETE /sessions/{id}  — delete a chat session (auth)
    GET    /settings       — non-secret config (auth)
    GET    /health         — liveness + component diagnostics (open)
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from fastapi import APIRouter, Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text, update
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from regwatch.assemble.dossier import build_dossier
from regwatch.auth.deps import SESSION_COOKIE, require_user
from regwatch.auth.sessions import authenticate, create_session, revoke_token
from regwatch.common.audit import log_query
from regwatch.common.conversation import SessionOwnershipError
from regwatch.common.logging import configure_logging, get_logger
from regwatch.common.observability import capture_exception, init_sentry
from regwatch.common.ratelimit import LOGIN_ATTEMPTS_PER_MINUTE, login_limiter, query_limiter
from regwatch.generate.grounded_qa import ask
from regwatch.process.embedder import assert_embedding_runtime_available
from regwatch.sources.router import search_sources
from regwatch.sources.types import SourceKind, SourceQuery
from regwatch.store.db import engine_dialect, get_engine, init_db, session_scope
from regwatch.store.models import AnswerFeedback, ChatMessage, ChatSession, QueryLog, User
from regwatch.store.vector_store import collection_size
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import ALLOWED_SOURCES, add_manual_product, list_watchlist
from regwatch.whitepaper.docx_writer import docx_media_type, write_whitepaper_docx
from regwatch.whitepaper.populator import SpineResolutionError, build_whitepaper

configure_logging()
log = get_logger(__name__)


def _guard_test_providers(s: Settings) -> None:
    """Fail fast when test-grade echo providers face a real corpus.

    Echo embeddings are hash noise — retrieval silently degrades while
    citations still validate. An empty corpus is fine (fresh checkout, the
    pre-seed boot of a Docker stack); a seeded one is not, unless the
    operator opted in explicitly (tests/CI).
    """
    if s.allow_test_providers:
        return
    if s.embedding_provider != "echo" and s.llm_provider != "echo":
        return
    if collection_size() == 0:
        return
    raise RuntimeError(
        "Test-grade 'echo' provider configured "
        f"(EMBEDDING_PROVIDER={s.embedding_provider}, LLM_PROVIDER={s.llm_provider}) "
        "against a non-empty vector corpus — retrieval quality would silently degrade. "
        "Fix: set EMBEDDING_PROVIDER=local-bge-small (for Docker also "
        "INSTALL_LOCAL_EMBEDDINGS=true) and a real LLM_PROVIDER, or set "
        "REGWATCH_ALLOW_TEST_PROVIDERS=1 to explicitly allow test providers."
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    # Sentry first (H1): OFF unless SENTRY_DSN is set. Initialized before
    # init_db so the explicit migration-mode-mismatch captures in store/db.py
    # can report a refused boot.
    sentry_enabled = init_sentry(s)
    # B4: in production, a missing SENTRY_DSN means every 500 vanishes to stderr
    # with no alerting. Don't hard-fail (Sentry being down must not take the app
    # down), but make the gap loud instead of silent.
    if s.sentry_environment == "production" and not sentry_enabled:
        log.warning(
            "sentry_disabled_in_production",
            detail="SENTRY_ENVIRONMENT=production but no SENTRY_DSN — errors are not being reported",
        )
    if os.getenv("REGWATCH_DB_INITIALIZED") != "1":
        init_db()
    elif s.database_url:
        # init_db (which asserts this itself) ran out-of-process — e.g. the
        # Docker entrypoint's `regwatch init-db`. Re-assert the K6 fail-fast
        # here so the API process never boots with a wrong-dim provider.
        from regwatch.store.pgvector_store import assert_embedding_provider_dim

        assert_embedding_provider_dim()
    _guard_test_providers(s)
    # A provider whose runtime deps are missing (slim image + local-bge-small)
    # must refuse to boot, not 500 on the first embed call.
    assert_embedding_runtime_available(s.embedding_provider)
    yield


app = FastAPI(
    title="REGWATCH",
    version="0.1.0",
    description=(
        "Operational POC for a generic-drug Clinical Regulatory Affairs team. "
        "Public FDA data only. The system surfaces, organizes, compares, and cites; "
        "it never authors submission content or renders regulatory judgment."
    ),
    lifespan=_lifespan,
    # The contract opens exactly one unauthenticated endpoint (GET /health).
    # FastAPI's default docs routes register at app level — outside the
    # `protected` router — so they would disclose the full API surface (every
    # route, schema, and the session-cookie name) to anonymous visitors via
    # the UI's /api proxy. Off for the pilot.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS — the Next.js UI (regwatch/frontend/) calls this API from the browser.
# Allowlist comes from settings. allow_credentials lets the browser send the
# HttpOnly session cookie; the explicit origin allowlist is what keeps that safe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


async def _handle_upstream_error(_request: Request, exc: Exception) -> JSONResponse:
    """Map an LLM-provider transport error to 503 — never a naked 500 that leaks
    provider internals. The /query path already degrades to an audited refusal
    (B2); this is the safety net for /assemble, /whitepaper, and the
    router/extractor LLM calls. Genuine bugs still 500 and are Sentry-captured.
    """
    log.warning("upstream_provider_error", error_type=type(exc).__name__)
    capture_exception(exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "the answer service is temporarily unavailable; please try again"},
    )


def _register_upstream_error_handlers(target: FastAPI) -> None:
    """Register the 503 handler for whichever LLM SDK base errors are importable.

    Registering the SDK's base error class catches all of its subclasses
    (timeout / connection / rate-limit / 5xx status). Kept lazy so the API does
    not hard-depend on the LLM SDKs in echo-only environments (tests/CI).
    """
    bases: list[type[Exception]] = []
    try:
        from openai import APIError as _OpenAIAPIError

        bases.append(_OpenAIAPIError)
    except Exception:  # SDK not installed in this environment
        pass
    try:
        from anthropic import APIError as _AnthropicAPIError

        bases.append(_AnthropicAPIError)
    except Exception:
        pass
    for base in bases:
        target.add_exception_handler(base, _handle_upstream_error)


_register_upstream_error_handlers(app)

# Single authorization chokepoint: every endpoint except GET /health and the
# /auth routes is registered on this router, so its router-level dependency
# makes an accidentally-unauthenticated route impossible.
auth_router = APIRouter(prefix="/auth", tags=["auth"])
protected = APIRouter(dependencies=[Depends(require_user)])


# ---------- /health ----------
def _db_component() -> dict[str, Any]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        # B1: expose the dialect so a prod stack accidentally on SQLite is
        # visibly wrong (operators / the uptime check can assert 'postgresql').
        return {"ok": True, "dialect": engine_dialect()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _chroma_component() -> dict[str, Any]:
    try:
        return {"ok": True, "corpus_count": collection_size()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _llm_key_present(s: Settings) -> bool:
    if s.llm_provider == "openai":
        return bool(s.openai_api_key)
    if s.llm_provider == "anthropic":
        return bool(s.anthropic_api_key)
    return True  # echo needs no key


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    """Diagnose the stack: db, chroma, providers. Superset of {"status": "ok"}.

    503 only when the DB or Chroma is unreachable. An empty corpus is healthy
    (with a warning) so a fresh stack can boot and the ingest service can seed.
    """
    s = get_settings()
    db = _db_component()
    chroma = _chroma_component()
    warnings: list[str] = []
    if chroma["ok"] and chroma["corpus_count"] == 0:
        warnings.append("corpus is empty — run `regwatch seed` (or the compose ingest profile)")
    if s.embedding_provider == "echo" or s.llm_provider == "echo":
        warnings.append("test-grade 'echo' provider in use — retrieval quality is degraded")
    body: dict[str, Any] = {
        "status": "ok",
        "components": {
            "db": db,
            "chroma": chroma,
            "llm": {"provider": s.llm_provider, "key_present": _llm_key_present(s)},
            "embedding": {"provider": s.embedding_provider},
        },
        "warnings": warnings,
    }
    if s.allow_test_providers:
        body["allow_test_providers"] = True
    if not (db["ok"] and chroma["ok"]):
        body["status"] = "unhealthy"
        response.status_code = 503
    return body


# ---------- /auth ----------
class LoginRequest(BaseModel):
    # max_length bounds the rate limiter's per-key memory — the limiter key
    # embeds the email — and RFC 5321 caps addresses at 254 chars anyway.
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str


class AuthUserResponse(BaseModel):
    user: UserOut


def _user_out(user: User) -> UserOut:
    if user.id is None:  # pragma: no cover — auth only ever returns persisted users
        raise HTTPException(status_code=401, detail="authentication required")
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


@auth_router.post("/login", response_model=AuthUserResponse)
def login(req: LoginRequest, response: Response) -> AuthUserResponse:
    email = req.email.strip().lower()
    if not login_limiter.allow(f"login:{email}", LOGIN_ATTEMPTS_PER_MINUTE):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    user = authenticate(req.email, req.password)
    if user is None or user.id is None:
        # One message for unknown email / wrong password / inactive user;
        # authenticate() burns a bcrypt verify in every branch (uniform timing).
        raise HTTPException(status_code=401, detail="invalid email or password")
    s = get_settings()
    token, _ = create_session(user.id)  # always a fresh row — no session fixation
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=s.auth_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=s.auth_cookie_secure,
        path="/",
    )
    return AuthUserResponse(user=_user_out(user))


@auth_router.post("/logout", status_code=204)
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> None:
    """Revoke the server-side session and clear the cookie. Never errors."""
    if session_token:
        revoke_token(session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@auth_router.get("/me", response_model=AuthUserResponse)
def me(user: User = Depends(require_user)) -> AuthUserResponse:
    return AuthUserResponse(user=_user_out(user))


def _enforce_query_rate_limit(user: User) -> None:
    limit = get_settings().rate_limit_per_minute
    if not query_limiter.allow(f"user:{user.id}", limit):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


# ---------- /query ----------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=2)
    filters: dict[str, Any] | None = None
    k: int | None = Field(None, ge=1)
    session_id: str | None = None


class QueryCitation(BaseModel):
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    source_url: str
    snippet: str


class ClarifyOptionOut(BaseModel):
    label: str
    query: str
    filters: dict[str, Any] | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[QueryCitation]
    refused: bool
    model_name: str
    audit_id: int
    session_id: str
    turn_id: str
    status: str = "answer"  # "answer" | "summary" | "clarify" | "scope_warning" | "refused"
    interpretation: str | None = None
    clarify: list[ClarifyOptionOut] = []


def _authorize_session_access(session_id: str, user_id: str) -> None:
    """Enforce chat-session ownership before ask() touches it.

    Missing row → proceed (ask() creates it bound to the caller). NULL owner
    (legacy demo data) → adopt it via a conditional UPDATE, so two requests
    racing on the same legacy session cannot both win — the loser re-reads the
    committed owner and 404s. Another user's row → 404, not 403, so the
    response does not confirm the session exists.
    """
    with session_scope() as s:
        row = s.get(ChatSession, session_id)
        if row is None:
            return
        owner = row.user_id
        if owner is None:
            s.execute(
                update(ChatSession)
                .where(col(ChatSession.id) == session_id, col(ChatSession.user_id).is_(None))
                .values(user_id=user_id)
            )
            s.expire(row)
            owner = row.user_id  # re-read: ours when we won the race, else the winner's
        if owner != user_id:
            raise HTTPException(status_code=404, detail="session not found")


@protected.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, user: User = Depends(require_user)) -> QueryResponse:
    _enforce_query_rate_limit(user)
    user_id = str(user.id)
    if req.session_id:
        _authorize_session_access(req.session_id, user_id)
    try:
        result = ask(
            req.question,
            filters=req.filters,
            k=req.k,
            session_id=req.session_id,
            user_id=user_id,
        )
    except SessionOwnershipError as exc:
        # An ownership race lost after the pre-check above — same 404 as any
        # other foreign session, never confirming the session exists.
        raise HTTPException(status_code=404, detail="session not found") from exc
    if result.session_id is None or result.turn_id is None:
        raise HTTPException(status_code=500, detail="query did not produce session metadata")
    return QueryResponse(
        answer=result.answer,
        citations=[QueryCitation(**c.__dict__) for c in result.citations],
        refused=result.refused,
        model_name=result.model_name,
        audit_id=result.audit_id,
        session_id=result.session_id,
        turn_id=result.turn_id,
        status=result.status,
        interpretation=result.interpretation,
        clarify=[
            ClarifyOptionOut(label=o.label, query=o.query, filters=o.filters)
            for o in result.clarify
        ],
    )


# ---------- /feedback ----------
class FeedbackRequest(BaseModel):
    audit_id: int
    rating: int
    comment: str | None = Field(None, max_length=2000)

    @field_validator("rating")
    @classmethod
    def _check_rating(cls, v: int) -> int:
        if v not in (-1, 1):
            raise ValueError("rating must be -1 (thumbs down) or 1 (thumbs up)")
        return v


class FeedbackResponse(BaseModel):
    audit_id: int
    rating: int
    comment: str | None = None


def _upsert_feedback(audit_id: int, user_id: str, rating: int, comment: str | None) -> None:
    """One feedback row per (audit_id, user_id) — re-rating replaces."""
    with session_scope() as s:
        existing = s.scalars(
            select(AnswerFeedback).where(
                AnswerFeedback.audit_id == audit_id,
                AnswerFeedback.user_id == user_id,
            )
        ).first()
        if existing is None:
            s.add(
                AnswerFeedback(audit_id=audit_id, user_id=user_id, rating=rating, comment=comment)
            )
        else:
            existing.rating = rating
            existing.comment = comment
            s.add(existing)
        s.flush()


@protected.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest, user: User = Depends(require_user)) -> FeedbackResponse:
    """Thumbs up/down on one of the caller's own answered Q&A turns (H4).

    404 for a missing, foreign, or non-qa audit row — mirroring the docx
    ownership pattern, the response never confirms that someone else's audit
    row exists. Feedback rows are the candidate pool for future eval gold-set
    items (see README).
    """
    user_id = str(user.id)
    with session_scope() as s:
        row = s.get(QueryLog, req.audit_id)
        if row is None or row.mode != "qa" or row.user_id != user_id:
            raise HTTPException(status_code=404, detail="answer not found")
    try:
        _upsert_feedback(req.audit_id, user_id, req.rating, req.comment)
    except IntegrityError:
        # Lost a concurrent-insert race on uq_answer_feedback_audit_user — the
        # row exists now, so the retry takes the replace branch.
        _upsert_feedback(req.audit_id, user_id, req.rating, req.comment)
    return FeedbackResponse(audit_id=req.audit_id, rating=req.rating, comment=req.comment)


# ---------- /sources/search ----------
class SourceSearchRequest(BaseModel):
    query_text: str = ""
    active_ingredient: str | None = None
    brand_name: str | None = None
    application_number: str | None = None
    ndc: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    limit: int = Field(10, ge=1, le=50)
    sources: list[SourceKind] | None = None


class SourceRecordResponse(BaseModel):
    source: SourceKind
    title: str
    source_url: str
    identifiers: dict[str, str]
    fields: dict[str, Any]


class SourceSearchResponse(BaseModel):
    routed_sources: list[SourceKind]
    records: list[SourceRecordResponse]


@protected.post("/sources/search", response_model=SourceSearchResponse)
def sources_search(req: SourceSearchRequest) -> SourceSearchResponse:
    routed, records = search_sources(
        SourceQuery(
            query_text=req.query_text,
            active_ingredient=req.active_ingredient,
            brand_name=req.brand_name,
            application_number=req.application_number,
            ndc=req.ndc,
            dosage_form=req.dosage_form,
            route=req.route,
            limit=req.limit,
        ),
        sources=req.sources,
    )
    return SourceSearchResponse(
        routed_sources=routed,
        records=[
            SourceRecordResponse(
                source=r.source,
                title=r.title,
                source_url=r.source_url,
                identifiers=r.identifiers,
                fields=r.fields,
            )
            for r in records
        ],
    )


# ---------- /assemble ----------
class AssembleRequest(BaseModel):
    active_ingredient: str = Field(..., min_length=2)
    dosage_form: str | None = None
    rld: str | None = Field(None, description="RLD brand name or application number")


class AssembleResponse(BaseModel):
    markdown: str
    sections: dict[str, Any]
    refused: bool


@protected.post("/assemble", response_model=AssembleResponse)
def assemble(req: AssembleRequest, user: User = Depends(require_user)) -> AssembleResponse:
    _enforce_query_rate_limit(user)
    dossier = build_dossier(
        active_ingredient=req.active_ingredient,
        dosage_form=req.dosage_form,
        rld=req.rld,
        user_id=str(user.id),
    )
    return AssembleResponse(**dossier)


# ---------- /whitepaper ----------
class WhitepaperRequest(BaseModel):
    rld_name: str = Field(..., min_length=1, max_length=200)
    application_number: str = Field(..., min_length=1, max_length=40)


@protected.post("/whitepaper")
def whitepaper(req: WhitepaperRequest, user: User = Depends(require_user)) -> dict[str, Any]:
    """Populate the CRA White Paper for an RLD name + NDA/ANDA number.

    Writes one whitepaper audit row (in build_whitepaper) on success AND on a
    422 resolution failure. Rate-limited like /query and /assemble.
    """
    _enforce_query_rate_limit(user)
    try:
        return build_whitepaper(req.rld_name, req.application_number, user_id=str(user.id))
    except SpineResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


class WhitepaperDocxRequest(BaseModel):
    """Body: the EXACT JSON object a previous POST /whitepaper returned."""

    result: dict[str, Any]


_DOCX_RESULT_DETAIL = (
    "result must be the exact JSON object returned by POST /whitepaper "
    "({spine, sections, warnings, audit_id})"
)
_DOCX_CELL_KEYS = ("id", "label", "status")

# What /whitepaper actually returns: six digits, optionally NDA/ANDA/BLA-
# prefixed. The value is interpolated into the Content-Disposition filename
# (and the audit row), so anything looser — CR/LF, quotes, path characters —
# is rejected rather than trusted into a response header.
_DOCX_APPL_NO_RE = re.compile(r"^[A-Z]{0,4}\d{6}$")


def _validated_docx_result(result: dict[str, Any]) -> tuple[int, str]:
    """Minimal shape check before rendering: (audit_id, application_number).

    The docx is rendered verbatim from this payload — no re-populate — so the
    shape the writer dereferences must hold, and nothing else is trusted.
    """

    def reject(why: str) -> HTTPException:
        return HTTPException(status_code=422, detail=f"{_DOCX_RESULT_DETAIL}: {why}")

    audit_id = result.get("audit_id")
    if not isinstance(audit_id, int) or isinstance(audit_id, bool):
        raise reject("audit_id must be an integer")
    spine = result.get("spine")
    if not isinstance(spine, dict):
        raise reject("spine must be an object")
    appl_no = spine.get("application_number")
    if not isinstance(appl_no, str) or not _DOCX_APPL_NO_RE.fullmatch(appl_no):
        raise reject(
            "spine.application_number must be an FDA application number "
            "(six digits, optional NDA/ANDA/BLA prefix)"
        )
    sections = result.get("sections")
    if not isinstance(sections, list) or not sections:
        raise reject("sections must be a non-empty list")
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("cells"), list):
            raise reject("every section must be an object with a cells list")
        for cell in section["cells"]:
            if not isinstance(cell, dict):
                raise reject("every cell must be an object")
            if any(not isinstance(cell.get(key), str) for key in _DOCX_CELL_KEYS):
                raise reject("every cell must carry string id, label, and status")
            if cell.get("value") is not None and not isinstance(cell["value"], str):
                raise reject("cell value must be a string or null")
            evidence = cell.get("evidence")
            if not isinstance(evidence, list) or any(not isinstance(ev, dict) for ev in evidence):
                raise reject("every cell must carry an evidence list of objects")
    return audit_id, appl_no


def _require_owned_whitepaper_audit(audit_id: int, user_id: str) -> None:
    """The audit row must be the caller's own successful white-paper run.

    One uniform 422 for fabricated, foreign, or non-whitepaper ids — the
    response never confirms that someone else's audit row exists.
    """
    with session_scope() as s:
        row = s.get(QueryLog, audit_id)
        if (
            row is None
            or row.mode != "whitepaper"
            or row.status != "populated"
            or row.user_id != user_id
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"audit_id {audit_id} does not reference one of your white-paper runs — "
                    "re-run POST /whitepaper and send its result verbatim"
                ),
            )


@protected.post("/whitepaper/docx")
def whitepaper_docx(req: WhitepaperDocxRequest, user: User = Depends(require_user)) -> Response:
    """Render the Word document FROM a previously returned /whitepaper result.

    No re-populate: zero live fetches, zero LLM calls — the .docx is rendered
    from the exact JSON the analyst reviewed, after verifying result.audit_id
    is the caller's own white-paper audit row. Writes one lightweight audit row
    (mode="whitepaper", docx_render) and keeps the /query rate limiter.
    """
    _enforce_query_rate_limit(user)
    audit_id, appl_no = _validated_docx_result(req.result)
    _require_owned_whitepaper_audit(audit_id, str(user.id))
    data = write_whitepaper_docx(req.result, template_path=get_settings().whitepaper_template_path)
    log_query(
        mode="whitepaper",
        query_text=f"whitepaper docx application_number={appl_no!r}",
        retrieved=[],
        answer_text=f"Rendered the white-paper .docx from audit #{audit_id} (no re-population).",
        citations=[],
        refused=False,
        model_name="(docx-render)",
        user_id=str(user.id),
        status="docx_rendered",
        route_json={
            "route": "whitepaper",
            "reason": "docx_render",
            "source_audit_id": audit_id,
            "application_number": appl_no,
        },
    )
    return Response(
        content=data,
        media_type=docx_media_type(),
        headers={"Content-Disposition": f'attachment; filename="whitepaper_{appl_no}.docx"'},
    )


# ---------- /watch/latest ----------
def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _record_captured_at(record: dict[str, Any]) -> datetime | None:
    raw = record.get("captured_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


@protected.get("/watch/latest")
def watch_latest(since: datetime | None = None) -> dict[str, Any]:
    records = latest_digest_records(limit=200)
    if since:
        since_utc = _as_utc(since)
        records = [
            r
            for r in records
            if (captured_at := _record_captured_at(r)) is not None and captured_at >= since_utc
        ]
    return {"count": len(records), "alerts": records}


# ---------- /products ----------
class ProductCreate(BaseModel):
    active_ingredient: str
    dosage_form: str | None = None
    route: str | None = None
    rld_name: str | None = None
    rld_application_number: str | None = None
    company_status: str | None = None
    source: str = Field(..., description=f"one of {sorted(ALLOWED_SOURCES)}")
    source_url: str | None = None


@protected.get("/products")
def list_products() -> dict[str, Any]:
    items = list_watchlist()
    return {"count": len(items), "products": items}


@protected.post("/products", status_code=201)
def create_product(req: ProductCreate) -> dict[str, Any]:
    if req.source not in ALLOWED_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"source must be one of {sorted(ALLOWED_SOURCES)} (INV-5)",
        )
    added = add_manual_product(
        active_ingredient=req.active_ingredient,
        dosage_form=req.dosage_form,
        route=req.route,
        rld_name=req.rld_name,
        rld_application_number=req.rld_application_number,
        company_status=req.company_status,
        source=req.source,
        source_url=req.source_url,
    )
    return {"added": added, "products": list_watchlist()}


# ---------- /sessions (per-user chat history) ----------
def _session_title(s: Session, row: ChatSession) -> str:
    if row.title:
        return row.title
    first = s.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == row.id, ChatMessage.role == "user")
        .order_by(col(ChatMessage.created_at).asc())
        .limit(1)
    ).first()
    if first is not None and first.content:
        return first.content[:60]
    return "(untitled)"


def _owned_session_or_404(s: Session, session_id: str, user_id: str) -> ChatSession:
    """NULL-user legacy sessions stay invisible here until adopted via /query."""
    row = s.get(ChatSession, session_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    return row


@protected.get("/sessions")
def list_sessions(user: User = Depends(require_user)) -> dict[str, Any]:
    """Two queries max — network RTT amplifies per-row queries ~1000x on Postgres.

    Query 1 is the session page with the title fallback (first user message)
    folded in as a correlated scalar subquery; query 2 fetches all message
    counts for the page via one GROUP BY. Never N+1.
    """
    user_id = str(user.id)
    with session_scope() as s:
        first_user_message = (
            sa_select(col(ChatMessage.content))
            .where(
                col(ChatMessage.session_id) == col(ChatSession.id),
                col(ChatMessage.role) == "user",
            )
            .order_by(col(ChatMessage.created_at).asc())
            .limit(1)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        rows: list[tuple[ChatSession, str | None]] = [
            (r[0], r[1])
            for r in s.execute(
                sa_select(ChatSession, first_user_message)
                .where(col(ChatSession.user_id) == user_id)
                .order_by(col(ChatSession.updated_at).desc())
            )
        ]
        session_ids = [row.id for row, _ in rows]
        counts: dict[str, int] = {}
        if session_ids:
            counts = {
                str(sid): int(n)
                for sid, n in s.execute(
                    sa_select(col(ChatMessage.session_id), func.count())
                    .where(col(ChatMessage.session_id).in_(session_ids))
                    .group_by(col(ChatMessage.session_id))
                )
            }
        sessions = [
            {
                "id": row.id,
                "title": row.title or (first_msg[:60] if first_msg else "(untitled)"),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "message_count": counts.get(row.id, 0),
            }
            for row, first_msg in rows
        ]
    return {"sessions": sessions}


@protected.get("/sessions/{session_id}")
def get_session(session_id: str, user: User = Depends(require_user)) -> dict[str, Any]:
    with session_scope() as s:
        row = _owned_session_or_404(s, session_id, str(user.id))
        messages = s.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(col(ChatMessage.created_at).asc())
        ).all()
        return {
            "session": {
                "id": row.id,
                "title": _session_title(s, row),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            },
            "messages": [
                {
                    "id": m.id,
                    "turn_id": m.turn_id,
                    "role": m.role,
                    "content": m.content,
                    "status": m.status,
                    "citations": list(m.citations_json or []),
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
        }


@protected.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, user: User = Depends(require_user)) -> None:
    with session_scope() as s:
        row = _owned_session_or_404(s, session_id, str(user.id))
        for m in s.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)):
            s.delete(m)
        s.delete(row)


# ---------- /settings (read-only, no secrets) ----------
@protected.get("/settings")
def get_public_settings() -> dict[str, Any]:
    s = get_settings()
    return {
        "embedding_provider": s.embedding_provider,
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "retrieval_top_k": s.retrieval_top_k,
        "refusal_score_threshold": s.refusal_score_threshold,
        "company_name": s.company_name,
    }


app.include_router(auth_router)
app.include_router(protected)
