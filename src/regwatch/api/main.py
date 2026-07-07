"""FastAPI surface.

This is the clean boundary the IT/AI team will wrap or replace. Every
response is reproducible in Postman from a `.env` and a running instance.

Endpoints (per spec §10.16):
    POST   /auth/login     — issue a session cookie
    POST   /auth/logout    — revoke the session cookie
    GET    /auth/me        — current user
    POST   /query          — grounded Q&A (auth)
    POST   /query/stream   — grounded Q&A, streamed as Server-Sent Events (auth)
    POST   /feedback       — thumbs up/down on one of the caller's answers (auth)
    POST   /sources/search — structured FDA source lookup (auth)
    POST   /assemble       — build a cited dossier for a target product (auth)
    POST   /whitepaper     — populate the CRA White Paper (RLD + appl no) (auth)
    GET    /whitepaper/runs - org-shared saved white-paper runs (auth)
    GET    /whitepaper/runs/{id} - one saved run + analyst overlay (auth)
    POST   /whitepaper/runs/{id}/cells/{cell_id} - set/clear one analyst cell (auth)
    POST   /whitepaper/runs/{id}/finalize - freeze a run (draft -> final) (auth)
    POST   /whitepaper/runs/{id}/reopen - reopen a finalized run (auth)
    POST   /whitepaper/runs/{id}/docx - render a saved run as .docx (auth)
    DELETE /whitepaper/runs/{id} - creator-only draft delete (auth)
    GET    /watch/latest   — recent alerts (auth)
    GET    /products       — list watchlist (auth)
    POST   /products       — add manual product (auth)
    DELETE /products/{id}  - remove a product from the watchlist (soft) (auth)
    GET    /sessions       — the caller's chat sessions (auth)
    GET    /sessions/{id}  — one chat session with messages (auth)
    DELETE /sessions/{id}  — delete a chat session (auth)
    GET    /settings       — non-secret config (auth)
    GET    /health         — liveness + component diagnostics (open)
    GET    /ready          - readiness: db + vector store + LLM constructable (open)
    GET    /metrics        - Prometheus counters from the query_log audit
                              (open by default; bearer-gated when METRICS_TOKEN set)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

import anyio.to_thread
from config.settings import Settings, get_settings
from fastapi import APIRouter, Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, text, update
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from regwatch.assemble.dossier import build_dossier
from regwatch.auth.deps import SESSION_COOKIE, require_user
from regwatch.auth.sessions import authenticate, create_session, revoke_token
from regwatch.common.audit import log_query
from regwatch.common.conversation import SESSION_FILTER_KEYS, SessionOwnershipError
from regwatch.common.logging import configure_logging, get_logger
from regwatch.common.observability import capture_exception, init_sentry
from regwatch.common.ratelimit import (
    LOGIN_ATTEMPTS_PER_IP_PER_MINUTE,
    LOGIN_ATTEMPTS_PER_MINUTE,
    login_limiter,
    query_limiter,
)
from regwatch.generate.grounded_qa import QAResult, QueryStatusLiteral, ask
from regwatch.process.embedder import assert_embedding_runtime_available
from regwatch.sources.router import search_sources
from regwatch.sources.types import SourceKind, SourceQuery
from regwatch.store import whitepaper_runs as run_store
from regwatch.store.db import engine_dialect, get_engine, init_db, session_scope
from regwatch.store.models import (
    AnswerFeedback,
    ChatMessage,
    ChatSession,
    QueryLog,
    User,
    WhitepaperRun,
)
from regwatch.store.queries import fetch_citation_recency
from regwatch.store.vector_store import collection_size
from regwatch.watch.alerts import count_digest_records, latest_digest_records
from regwatch.watch.runs import latest_watch_run
from regwatch.watch.watchlist import add_manual_product, list_watchlist, set_on_watchlist
from regwatch.whitepaper import template_fetch
from regwatch.whitepaper.docx_writer import docx_media_type, write_whitepaper_docx
from regwatch.whitepaper.populator import (
    SpineResolutionError,
    build_whitepaper,
    resolve_spine,
    result_fingerprint,
)

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
    # Same fail-loud posture for the session cookie: a production deploy that
    # forgets AUTH_COOKIE_SECURE=true ships the cookie without Secure. Everything
    # still works, so the gap is silent — make it visible instead.
    if s.sentry_environment == "production" and not s.auth_cookie_secure:
        log.warning(
            "insecure_session_cookie_in_production",
            detail="SENTRY_ENVIRONMENT=production but AUTH_COOKIE_SECURE is false — "
            "the session cookie ships without the Secure flag",
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
        # /health is the one anonymous-reachable endpoint: never return the raw
        # exception (it discloses DB host/port/name + driver) to the caller.
        # Keep the detail server-side / in Sentry.
        log.warning("health_db_unreachable", error=str(exc))
        capture_exception(exc)
        return {"ok": False, "error": "unreachable"}


def _chroma_component() -> dict[str, Any]:
    try:
        return {"ok": True, "corpus_count": collection_size()}
    except Exception as exc:
        log.warning("health_vector_store_unreachable", error=str(exc))
        capture_exception(exc)
        return {"ok": False, "error": "unreachable"}


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
        # Pure path/config inspection (no I/O beyond a stat, never raises), so a
        # prod stack silently rendering FALLBACK_MARKER documents is visible at
        # a glance. Diagnostic only: it never flips the 503.
        "whitepaper_template": template_fetch.template_status(
            s.whitepaper_template_path, s.whitepaper_template_url
        ),
        "warnings": warnings,
    }
    if s.allow_test_providers:
        body["allow_test_providers"] = True
    if not (db["ok"] and chroma["ok"]):
        body["status"] = "unhealthy"
        response.status_code = 503
    return body


# ---------- /ready ----------
def _llm_ready(s: Settings) -> tuple[bool, str | None]:
    """The LLM provider must be CONSTRUCTABLE (key present / valid name) - but we
    make NO paid call here. get_llm_provider() raises on a missing key or unknown
    provider, which is exactly the readiness signal we want; echo always
    constructs. Returns (ok, reason) where reason is a short, non-secret label.
    """
    try:
        from regwatch.generate.llm import get_llm_provider

        get_llm_provider()
        return True, None
    except Exception as exc:
        # Don't leak the message (it could echo a configured value); the type +
        # provider name is enough for an operator to act on.
        log.warning("ready_llm_unconstructable", error_type=type(exc).__name__)
        return False, f"llm provider {s.llm_provider!r} is not constructable"


@app.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    """Readiness probe: 200 only when the DB + vector store are reachable AND the
    LLM client is constructable (key present). Distinct from /health's liveness:
    a load balancer routes traffic on this. No paid LLM call is made - only the
    cheap reachability checks. Both are timeout-bounded by the per-connection
    connect/statement timeouts on the shared engine (the vector-store probe is a
    `SELECT count(*)` in pgvector mode, so a degraded DB is capped by
    DB_STATEMENT_TIMEOUT rather than hanging the probe). 503 names the FIRST
    failed check so an operator sees what to fix.
    """
    db = _db_component()
    chroma = _chroma_component()
    llm_ok, llm_reason = _llm_ready(get_settings())
    checks = {"db": db["ok"], "vector_store": chroma["ok"], "llm": llm_ok}
    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    failed = next(name for name, ok in checks.items() if not ok)
    response.status_code = 503
    return {
        "status": "not_ready",
        "checks": checks,
        "failed": failed,
        "detail": llm_reason if failed == "llm" else f"{failed} is unreachable",
    }


# ---------- /metrics ----------
def _query_log_counters() -> dict[str, int]:
    """Aggregate query_log into counters for /metrics in ONE grouped query (no
    N+1). Keys: total, refused, and per-mode totals (qa/assemble/whitepaper/...).
    A DB error yields an empty dict so /metrics degrades to the static help/type
    lines rather than 500-ing the scrape.
    """
    counters: dict[str, int] = {}
    try:
        with session_scope() as s:
            for mode, refused, n in s.execute(
                sa_select(
                    col(QueryLog.mode),
                    col(QueryLog.refused),
                    func.count(),
                ).group_by(col(QueryLog.mode), col(QueryLog.refused))
            ):
                count = int(n)
                counters["total"] = counters.get("total", 0) + count
                if refused:
                    counters["refused"] = counters.get("refused", 0) + count
                counters[f"mode:{mode}"] = counters.get(f"mode:{mode}", 0) + count
    except Exception as exc:
        log.warning("metrics_query_failed", error_type=type(exc).__name__)
        return {}
    return counters


def _render_prometheus(counters: dict[str, int]) -> str:
    """Hand-rolled Prometheus text exposition (no client dependency).

    Emits HELP/TYPE then one sample per series. Modes become a `mode` label on
    regwatch_queries_total; refusals are their own counter. All counter values,
    so a scraper computes rates/ratios. Missing series default to 0 so a fresh
    process still exposes the named metrics.
    """
    refused = counters.get("refused", 0)
    total = counters.get("total", 0)
    lines = [
        "# HELP regwatch_queries_total Total audited query_log rows by mode.",
        "# TYPE regwatch_queries_total counter",
    ]
    mode_keys = sorted(k for k in counters if k.startswith("mode:"))
    if mode_keys:
        for key in mode_keys:
            mode = key[len("mode:") :]
            lines.append(f'regwatch_queries_total{{mode="{mode}"}} {counters[key]}')
    else:
        # No rows yet: still expose the series (empty-label) so the metric exists.
        lines.append(f"regwatch_queries_total {total}")
    lines += [
        "# HELP regwatch_queries_refused_total Audited query_log rows that refused.",
        "# TYPE regwatch_queries_refused_total counter",
        f"regwatch_queries_refused_total {refused}",
    ]
    return "\n".join(lines) + "\n"


def _metrics_authorized(request: Request, s: Settings) -> bool:
    """Whether this /metrics request may proceed.

    OPT-IN gate: when metrics_token is unset the endpoint is open (returns True),
    preserving today's behavior so an existing Prometheus scrape keeps working.
    Once ops sets METRICS_TOKEN the caller must present a matching
    `Authorization: Bearer <token>`. compare_digest gives a constant-time check
    (no token-length/prefix leak via timing); comparing on the utf-8 bytes keeps
    a non-ASCII header from raising inside compare_digest and 500-ing the scrape.
    """
    if s.metrics_token is None:
        return True
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.encode("utf-8"), s.metrics_token.encode("utf-8"))


@app.get("/metrics")
def metrics(request: Request) -> Response:
    """Prometheus text-exposition counters derived from the query_log audit table.

    Hand-rolled (no prometheus_client dependency): exposes total queries by mode
    and the refusal counter. The body is plain text/version-0.0.4.

    Access is OPT-IN: open like /health and /ready by default (so a scraper
    reaches it without the session cookie), but when METRICS_TOKEN is set the
    request must carry `Authorization: Bearer <token>` or this returns 401.
    /health and /ready are never gated this way.
    """
    if not _metrics_authorized(request, get_settings()):
        raise HTTPException(status_code=401, detail="metrics authentication required")
    body = _render_prometheus(_query_log_counters())
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


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


def _client_ip(request: Request, s: Settings) -> str:
    """The client IP to key the per-IP login limiter on.

    Any client-supplied forwarding header is spoofable: the LEFTMOST
    X-Forwarded-For hop is whatever the browser sent, so keying on it would let
    an attacker rotate a fake value and mint unlimited per-IP buckets, defeating
    the spray guard. So:
      * trust_proxy_headers OFF (direct exposure): key on the un-spoofable
        TCP-level request.client.host.
      * trust_proxy_headers ON (behind Fly/Vercel): prefer Fly-Client-IP, which
        Fly's edge sets to the platform-attested real client and a client cannot
        forge end-to-end. Only if it is absent fall back to the RIGHTMOST XFF
        hop (the entry our trusted edge appended), never split(",")[0]. The
        rightmost token is the closest-to-us proxy-attested address; earlier
        tokens are attacker-controlled and ignored.
    Falls back to "unknown" only when no source is available (no socket peer),
    so the limiter never crashes the login path.
    """
    if s.trust_proxy_headers:
        # Fly's platform-attested client IP (set by Fly's edge); not forgeable by
        # the browser the way XFF's leftmost hop is.
        fly_client_ip = request.headers.get("fly-client-ip", "").strip()
        if fly_client_ip:
            return fly_client_ip
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]  # rightmost = appended by our trusted edge, not the client
    return request.client.host if request.client else "unknown"


@auth_router.post("/login", response_model=AuthUserResponse)
def login(req: LoginRequest, request: Request, response: Response) -> AuthUserResponse:
    s = get_settings()
    email = req.email.strip().lower()
    # Two independent windows: per-email (a targeted brute force on one account)
    # AND per-IP (a credential-spray sweeping many DISTINCT emails from one host,
    # which the per-email key alone never sees). Either tripping returns 429.
    # NOTE: in-process limiter under min_machines_running=2 is ~2x effective; a
    # shared-store limiter is a separate parked item, NOT built here.
    ip = _client_ip(request, s)
    if not login_limiter.allow(
        f"login:{email}", LOGIN_ATTEMPTS_PER_MINUTE
    ) or not login_limiter.allow(f"login:ip:{ip}", LOGIN_ATTEMPTS_PER_IP_PER_MINUTE):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    user = authenticate(req.email, req.password)
    if user is None or user.id is None:
        # One message for unknown email / wrong password / inactive user;
        # authenticate() burns a bcrypt verify in every branch (uniform timing).
        raise HTTPException(status_code=401, detail="invalid email or password")
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
    # max_length bounds the synthesizer prompt and per-request work; other string
    # fields are capped for the same reason. 4000 chars is far above any real
    # question while refusing a megabyte payload.
    question: str = Field(..., min_length=2, max_length=4000)
    filters: dict[str, Any] | None = None
    # le mirrors SourceSearchRequest.limit: an authed caller must not be able to
    # request an unbounded k that materializes the whole corpus into one search.
    k: int | None = Field(None, ge=1, le=50)
    session_id: str | None = None

    @field_validator("filters")
    @classmethod
    def _whitelist_filter_keys(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep only the session-scope filter keys; drop everything else.

        A caller-supplied ``version_id`` would switch off the retriever's
        current-version scoping (superseded PSG chunks could then be cited as
        current), and any key outside the store's filterable columns raises an
        uncaught 500 in pgvector mode with no audit row. Unknown keys are
        DROPPED rather than 422'd because clarify options persisted by older
        sessions echo legacy keys (e.g. ``source_url``) and must keep working.
        Non-scalar values are dropped too: they cannot bind to the scalar
        filter columns.
        """
        if v is None:
            return None
        kept = {
            key: val
            for key, val in v.items()
            if key in SESSION_FILTER_KEYS and isinstance(val, str | int | float | bool)
        }
        dropped = sorted(set(v) - set(kept))
        if dropped:
            log.debug("query_filters_dropped", keys=dropped)
        return kept


class QueryCitation(BaseModel):
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    source_url: str
    snippet: str
    # Tier-2 confidence: the retriever similarity score of the passage this
    # citation traces to (copied by chunk_id from the audited retrieval, never
    # recomputed). None when no retrieved passage matches.
    score: float | None = None
    # Tier-2 recency: the FDA recommended date + cited diff summary of the PSG
    # version/document this citation traces to, joined by version_id (fallback
    # doc_id) in a single batched lookup. Both are best-effort context: a
    # missing row or a DB error yields null and never blocks the answer.
    recommended_date: date | None = None
    diff_summary: str | None = None


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
    status: QueryStatusLiteral = "answer"
    # The route reason behind the status ("low_top_score", "no_product",
    # "did_you_mean", …) — surfaced so the UI/eval can tell WHY we declined or
    # clarified, mirroring QAResult.reason.
    reason: str | None = None
    interpretation: str | None = None
    clarify: list[ClarifyOptionOut] = []
    # Inert "related, not an answer" pointers for the decline family — same wire
    # shape as `clarify` options (label + re-runnable query + filters), but the
    # UI renders them as plain pills, never citation chips. Never carries passage
    # text/score; refused/citations are unaffected (the refusal contract holds).
    related: list[ClarifyOptionOut] = []


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


def _parse_iso_date(value: str | None) -> date | None:
    """Parse a stored ISO date string to a ``date``; null on anything unparseable.

    recommended_date is persisted as a free string (models.py), so a malformed
    or partial value must degrade to null rather than 500 a valid answer.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _wire_citations(result: QAResult) -> list[QueryCitation]:
    """Serialize domain citations to the wire, enriched with score + recency.

    score is copied from the audited retrieval by chunk_id (never recomputed;
    null when no passage matches). recommended_date + diff_summary come from one
    batched recency lookup (no N+1) that returns nulls on any failure, so the
    enrichment can never block or break an already-validated answer.
    """
    scores: dict[str, float | None] = {
        str(p.get("chunk_id")): p.get("score") for p in result.retrieved
    }
    version_ids = sorted({c.version_id for c in result.citations})
    doc_ids = sorted({c.doc_id for c in result.citations})
    recency = fetch_citation_recency(version_ids, doc_ids)
    out: list[QueryCitation] = []
    for c in result.citations:
        # Domain Citation may already carry a score; prefer an explicit retrieval
        # match by chunk_id, else fall back to the dataclass value.
        score = scores.get(c.chunk_id, c.score)
        r = recency.resolve(c.version_id, c.doc_id)
        out.append(
            QueryCitation(
                short_name=c.short_name,
                page=c.page,
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                version_id=c.version_id,
                source_url=c.source_url,
                snippet=c.snippet,
                score=score,
                recommended_date=_parse_iso_date(r.recommended_date),
                diff_summary=r.diff_summary,
            )
        )
    return out


def _build_query_response(result: QAResult) -> QueryResponse:
    """Serialize a validated QAResult into the wire QueryResponse.

    Shared by POST /query and POST /query/stream so the two endpoints can never
    drift in shape. The missing-session-metadata guard is a real 500 for the
    buffered /query path; the streaming path catches it and closes without a
    result frame (the client then falls back to /query, which surfaces the 500).
    """
    if result.session_id is None or result.turn_id is None:
        raise HTTPException(status_code=500, detail="query did not produce session metadata")
    return QueryResponse(
        answer=result.answer,
        citations=_wire_citations(result),
        refused=result.refused,
        model_name=result.model_name,
        audit_id=result.audit_id,
        session_id=result.session_id,
        turn_id=result.turn_id,
        # QAResult.status IS QueryStatusLiteral (imported from grounded_qa, the
        # domain layer), so the OpenAPI enum -- and the generated TS union --
        # can never drift from what the domain emits.
        status=result.status,
        reason=result.reason,
        interpretation=result.interpretation,
        clarify=[
            ClarifyOptionOut(label=o.label, query=o.query, filters=o.filters)
            for o in result.clarify
        ],
        related=[
            ClarifyOptionOut(label=o.label, query=o.query, filters=o.filters)
            for o in result.related
        ],
    )


# ask() holds its worker thread for the whole pipeline including LLM synthesis
# (llm_timeout_s x retries — minutes under a slow provider), and a disconnected
# stream's thread is non-abandoning, so it keeps its token to completion. On
# the default anyio pool (40 tokens, shared with every sync-def endpoint
# including /health) a provider slowdown would starve platform liveness checks.
# A dedicated bounded limiter isolates ask() dispatch; at saturation we shed
# with a defined failure (503 buffered / stream close -> client fallback)
# instead of queueing every caller behind minutes-long holds.
_ASK_LIMITER = anyio.CapacityLimiter(16)


async def _dispatch_ask(**kwargs: Any) -> QAResult:
    """Run ask() on its dedicated bounded worker pool; 503 when saturated.

    The saturation check is read-then-acquire: a request racing past it queues
    briefly instead of shedding, which only softens the bound — steady-state
    saturation still returns the defined 503. Like run_in_threadpool, the
    dispatched thread is non-abandoning on cancellation.
    """
    limiter = _ASK_LIMITER
    if limiter.statistics().borrowed_tokens >= limiter.total_tokens:
        raise HTTPException(status_code=503, detail="server is busy, retry shortly")
    return await anyio.to_thread.run_sync(partial(ask, **kwargs), limiter=limiter)


@protected.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest, user: User = Depends(require_user)) -> QueryResponse:
    _enforce_query_rate_limit(user)
    user_id = str(user.id)
    if req.session_id:
        # DB I/O — keep it off the event loop (async def gives up the implicit
        # threadpool that sync-def endpoints get).
        await run_in_threadpool(_authorize_session_access, req.session_id, user_id)
    try:
        result = await _dispatch_ask(
            question=req.question,
            filters=req.filters,
            k=req.k,
            session_id=req.session_id,
            user_id=user_id,
        )
    except SessionOwnershipError as exc:
        # An ownership race lost after the pre-check above — same 404 as any
        # other foreign session, never confirming the session exists.
        raise HTTPException(status_code=404, detail="session not found") from exc
    # _build_query_response queries citation recency — also off-loop.
    return await run_in_threadpool(_build_query_response, result)


def _sse_event(name: str, data: dict[str, Any]) -> str:
    """One Server-Sent Events frame. The Ask client (askQueryStream) parses three
    event names: ``status`` (``{"text": ...}`` progress), ``token`` (``{"delta":
    ...}`` provisional answer text), and ``result`` (the full validated
    QueryResponse). Any other name is ignored, so we emit only these."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


# How long the SSE body may go quiet before we emit a comment keep-alive frame.
# Well under the ~60s idle timeout of typical proxies/load balancers.
_SSE_KEEPALIVE_INTERVAL_S = 15.0


async def _query_event_stream(req: QueryRequest, user_id: str) -> AsyncIterator[str]:
    """SSE body for POST /query/stream.

    Streams real pipeline progress as ``status`` frames and provisional answer
    text as ``token`` frames, then the validated answer as exactly ONE terminal
    ``result`` frame. The ``token`` deltas are COSMETIC: the authoritative answer
    is only the ``result`` frame, built from ask()'s post-citation-validation text
    (INV-1), and the refusal sentinel is never streamed as tokens (guarded inside
    ask()). The client renders tokens as a clearly-provisional "draft" with no
    citation surface, then replaces it with the validated ``result`` (INV-2).
    ask() runs in a worker thread so its progress/token callbacks push onto the
    event loop while it works, and writes exactly one audit row internally (INV-6,
    never duplicated). Once ask() has been dispatched onto its thread it runs to
    completion even if the client then disconnects (the threadpool is
    non-abandoning), so that turn is still audited; a disconnect in the narrow
    window BEFORE dispatch cancels the work before it starts and writes no row —
    correct, since nothing ran to audit. On any unexpected failure the stream
    closes with no ``result`` frame, which makes the client fall back to blocking
    POST /query exactly once (any provisional tokens are discarded).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def on_progress(textline: str) -> None:
        # Runs on the ask() worker thread — hand the line to the loop thread.
        loop.call_soon_threadsafe(queue.put_nowait, ("status", textline))

    def on_token(delta: str) -> None:
        # Provisional answer delta from the worker thread — cosmetic only; the
        # authoritative answer is still the terminal validated ``result`` frame.
        loop.call_soon_threadsafe(queue.put_nowait, ("token", delta))

    async def _run() -> None:
        try:
            result = await _dispatch_ask(
                question=req.question,
                filters=req.filters,
                k=req.k,
                session_id=req.session_id,
                user_id=user_id,
                on_progress=on_progress,
                on_token=on_token,
            )
            queue.put_nowait(("result", result))
        except HTTPException:
            # ask() pool saturated — close with no result frame; the client
            # falls back to blocking /query, which returns the real 503.
            queue.put_nowait(("error", None))
        except SessionOwnershipError:
            # Ownership lost after the pre-flight check — close, let /query 404.
            queue.put_nowait(("error", None))
        except Exception as exc:  # broad: any escape closes the stream for fallback
            log.warning("query_stream_failed", exc_info=True)
            capture_exception(exc)
            queue.put_nowait(("error", None))

    worker = asyncio.create_task(_run())
    try:
        yield _sse_event("status", {"text": "Consulting the corpus…"})
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=_SSE_KEEPALIVE_INTERVAL_S
                )
            except TimeoutError:
                # The synthesis quiet gap can span llm_timeout_s x retries with
                # zero bytes on the wire; intermediaries with ~60s idle timers
                # would cut a stream that was going to succeed and trigger the
                # client's double-cost /query fallback. SSE comment frames are
                # skipped by the client parser, so they are pure keep-alive.
                yield ": keep-alive\n\n"
                continue
            if kind == "status":
                yield _sse_event("status", {"text": payload})
                continue
            if kind == "token":
                yield _sse_event("token", {"delta": payload})
                continue
            if kind == "result":
                try:
                    # Recency enrichment does DB I/O — build the response off
                    # the event loop so a DB stall never freezes every stream.
                    response = await run_in_threadpool(_build_query_response, payload)
                except HTTPException:
                    log.warning("query_stream_missing_session_metadata")
                    return  # close without a result frame -> client falls back
                yield f"event: result\ndata: {response.model_dump_json()}\n\n"
                return
            # kind == "error": close without a result frame -> client falls back.
            return
    finally:
        worker.cancel()


@protected.post("/query/stream")
def query_stream(req: QueryRequest, user: User = Depends(require_user)) -> StreamingResponse:
    """Streaming twin of POST /query (Server-Sent Events).

    Same auth, rate limit, ownership, pipeline, and single audit row as /query;
    it streams live progress as ``status`` frames and the SAME validated
    QueryResponse as one terminal ``result`` frame (both built via
    _build_query_response, so the shapes cannot drift). The Ask UI consumes this
    and transparently falls back to POST /query if the stream fails. Rate-limit
    (429), ownership (404), and auth (401) are enforced BEFORE the stream opens,
    as real HTTP statuses — never mid-stream.
    """
    _enforce_query_rate_limit(user)
    user_id = str(user.id)
    if req.session_id:
        _authorize_session_access(req.session_id, user_id)
    return StreamingResponse(
        _query_event_stream(req, user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    # These fields are interpolated into outbound FDA query params; cap them so a
    # single authed caller can't push a multi-megabyte value into the request.
    query_text: str = Field("", max_length=1000)
    active_ingredient: str | None = Field(None, max_length=200)
    brand_name: str | None = Field(None, max_length=200)
    application_number: str | None = Field(None, max_length=40)
    ndc: str | None = Field(None, max_length=40)
    dosage_form: str | None = Field(None, max_length=200)
    route: str | None = Field(None, max_length=200)
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
def sources_search(
    req: SourceSearchRequest, user: User = Depends(require_user)
) -> SourceSearchResponse:
    # Fans out to live FDA endpoints (openFDA, DailyMed, Orange Book ZIP), so
    # rate-limit it like the other outbound/expensive routes — an authed caller
    # must not be able to hammer FDA unthrottled (amplification / FDA-side block).
    _enforce_query_rate_limit(user)
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
    # max_length mirrors QueryRequest.question's rationale — these free-text
    # fields reach the grounded-QA prompt and the audit row, so cap them.
    active_ingredient: str = Field(..., min_length=2, max_length=200)
    dosage_form: str | None = Field(None, max_length=200)
    rld: str | None = Field(
        None, max_length=200, description="RLD brand name or application number"
    )


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


def _user_pk(user: User) -> int:
    if user.id is None:  # pragma: no cover - require_user only returns persisted users
        raise HTTPException(status_code=401, detail="authentication required")
    return user.id


def _persist_whitepaper_run(user_id: int, rld_name_input: str, result: dict[str, Any]) -> None:
    """Persist the populate result as a durable run; sets ``result["run_id"]``.

    DEGRADE, never fail: populate is the expensive step (live FDA fetches + an
    LLM turn), so losing durability must not lose the result -- same philosophy
    as the populator's ``_persist`` snapshot write-through. On failure the
    response ships with ``run_id: null`` plus an explicit warning, and the
    capture is sanitized the same way: a StatementError's str() embeds the
    failed SQL + parameter preview (the run payload), so only the exception
    CLASS is forwarded, with no cause/context chain.
    """
    try:
        result["run_id"] = run_store.create_run(
            user_id=user_id, rld_name_input=rld_name_input, result=result
        )
        return
    except Exception as exc:
        result["run_id"] = None
        warnings = result.get("warnings")
        if isinstance(warnings, list):
            warnings.append(
                "Saving this run failed - the populate result below is complete but "
                "was not persisted, so it will not appear in the runs list. "
                "Re-run POST /whitepaper to retry."
            )
        log.warning("whitepaper_run_persist_failed", error_type=type(exc).__name__)
        sanitized = RuntimeError(f"whitepaper run persist failed: {type(exc).__name__}")
        sanitized.__cause__ = None
        sanitized.__suppress_context__ = True
        capture_exception(sanitized)


@protected.post("/whitepaper")
def whitepaper(req: WhitepaperRequest, user: User = Depends(require_user)) -> dict[str, Any]:
    """Populate the CRA White Paper for an RLD name + NDA/ANDA number.

    Writes one whitepaper audit row (in build_whitepaper) on success AND on a
    422 resolution failure. Rate-limited like /query and /assemble. A
    successful populate is persisted as a durable org-shared run and the
    response gains ``run_id`` (null when the persist degraded, see
    ``_persist_whitepaper_run``).
    """
    _enforce_query_rate_limit(user)
    try:
        result = build_whitepaper(req.rld_name, req.application_number, user_id=str(user.id))
    except SpineResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    _persist_whitepaper_run(_user_pk(user), req.rld_name, result)
    return result


# ---------- /resolve ----------
class ResolveRequest(BaseModel):
    rld_name: str = Field(..., min_length=1, max_length=200)
    application_number: str = Field(..., min_length=1, max_length=40)


@protected.post("/resolve")
def resolve(req: ResolveRequest, user: User = Depends(require_user)) -> dict[str, Any]:
    """Resolve an RLD name + application number to the canonical spine.

    Deterministic entity resolution, NOT an LLM turn: it writes NO audit row
    (success or failure) and returns no answer text — it lets a surface pin a
    canonical product without running a full populate. On an unresolved or
    mismatched application it 422s with the resolver's own detail (refuse over
    guess). Rate-limited like /query, /assemble, /whitepaper (it hits live FDA
    sources just as they do).
    """
    _enforce_query_rate_limit(user)
    try:
        return resolve_spine(req.rld_name, req.application_number, user_id=str(user.id))
    except SpineResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


# ---------- /whitepaper/runs (durable runs + attributed analyst overlay) ----------
# What /whitepaper produces (and create_run normalizes): six digits, optionally
# NDA/ANDA/BLA-prefixed. The value is interpolated into the Content-Disposition
# filename (and the audit row), so anything looser (CR/LF, quotes, path
# characters) is refused rather than trusted into a response header.
_DOCX_APPL_NO_RE = re.compile(r"^[A-Z]{0,4}[0-9]{6}$")

_RUN_NOT_FOUND_DETAIL = "white-paper run not found"


class WhitepaperRunSummary(BaseModel):
    """One org-shared run list row (no JSON payloads -- see the detail route)."""

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


class WhitepaperRunListResponse(BaseModel):
    count: int
    total: int
    limit: int
    offset: int
    runs: list[WhitepaperRunSummary]


class WhitepaperInputOut(BaseModel):
    """One attributed analyst overlay value."""

    value: str
    author: str | None
    updated_at: datetime


class WhitepaperRunDetailResponse(BaseModel):
    """The full stored run.

    ``spine``/``sections``/``warnings`` are deliberately passthrough fields
    (plain dict/list, no nested models): the response must be VERBATIM what the
    audited populate stored -- serialization may never reshape or filter the
    fingerprinted sections payload (INV-3).
    """

    id: int
    rld_name_input: str
    application_number: str
    application_type: str
    ingredient: str
    normalized_name: str
    spine: dict[str, Any]
    sections: list[dict[str, Any]]
    warnings: list[str]
    status: str
    populated_count: int
    analyst_input_count: int
    verified_absent_count: int
    source_audit_id: int
    created_by: str
    created_by_user_id: int
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None
    finalized_by: str | None
    inputs: dict[str, WhitepaperInputOut]


class WhitepaperCellRequest(BaseModel):
    # null (or empty-after-cleaning) clears the cell. max_length sits a bit
    # ABOVE the store's MAX_INPUT_CHARS: the store cap applies AFTER control
    # characters are stripped, so this boundary bound (defense in depth against
    # unbounded bodies) must not reject values that clean down under the cap.
    value: str | None = Field(None, max_length=run_store.MAX_INPUT_CHARS + 1024)


class WhitepaperCellResponse(BaseModel):
    run_id: int
    cell_id: str
    cleared: bool
    input: WhitepaperInputOut | None


class WhitepaperRunStatusResponse(BaseModel):
    run_id: int
    status: str


def _input_out(view: run_store.InputView) -> WhitepaperInputOut:
    return WhitepaperInputOut(value=view.value, author=view.author, updated_at=view.updated_at)


def _stored_corruption_500(run_id: int, event: str) -> HTTPException:
    """Stored-data corruption (INV-3/INV-4): 500 + sanitized Sentry capture.

    Never a client 422 -- there is no client payload to fix -- and the detail
    (like the capture) never carries the stored hashes or values.
    """
    log.error(event, run_id=run_id)
    sanitized = RuntimeError(f"{event}: run_id={run_id}")
    sanitized.__cause__ = None
    sanitized.__suppress_context__ = True
    capture_exception(sanitized)
    return HTTPException(
        status_code=500, detail="stored white-paper run failed its integrity check"
    )


def _run_application_number(run_id: int) -> str:
    """The run's stored application number for audit rows -- one light column
    select, never the JSON payloads. Empty when the run vanished mid-request."""
    with session_scope() as s:
        appl_no = s.execute(
            sa_select(col(WhitepaperRun.application_number)).where(col(WhitepaperRun.id) == run_id)
        ).scalar_one_or_none()
    return appl_no or ""


def _log_run_workflow(user: User, run_id: int, *, status: str) -> None:
    """One QueryLog audit row per finalize/reopen -- the same generic audit
    trail docx_rendered rides on. model_name is a non-LLM marker (no model ran),
    consistent with "(docx-render)"."""
    appl_no = _run_application_number(run_id)
    log_query(
        mode="whitepaper",
        query_text=f"whitepaper run {status} run_id={run_id} application_number={appl_no!r}",
        retrieved=[],
        answer_text=f"White-paper run #{run_id} {status}.",
        citations=[],
        refused=False,
        model_name="(workflow)",
        user_id=str(user.id),
        status=status,
        route_json={
            "route": "whitepaper",
            "reason": status,
            "run_id": run_id,
            "application_number": appl_no,
        },
    )


@protected.get("/whitepaper/runs", response_model=WhitepaperRunListResponse)
def whitepaper_runs_list(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    application_number: str | None = Query(None, max_length=40),
    normalized_name: str | None = Query(None, max_length=200),
    status: str | None = Query(None, max_length=40),
) -> WhitepaperRunListResponse:
    """Org-shared saved runs, newest activity first.

    Any authenticated analyst sees every run (a product decision, design doc
    section 10); deletes stay creator-only. Shape follows /watch/latest:
    count/total/limit/offset so pagination stays truthful.
    """
    try:
        summaries, total = run_store.list_runs(
            limit=limit,
            offset=offset,
            application_number=application_number,
            normalized_name=normalized_name,
            status=status,
        )
    except ValueError as exc:
        # normalize_appl_no refused the filter -- a client error, never a 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runs = [WhitepaperRunSummary(**asdict(row)) for row in summaries]
    return WhitepaperRunListResponse(
        count=len(runs), total=total, limit=limit, offset=offset, runs=runs
    )


@protected.get("/whitepaper/runs/{run_id}", response_model=WhitepaperRunDetailResponse)
def whitepaper_run_detail(run_id: int) -> WhitepaperRunDetailResponse:
    """One saved run: verbatim generated payload + the analyst overlay.

    404 for a missing id -- runs are org-shared, so existence is not a secret
    (the legacy uniform-422 pattern applied only to the per-user audit lookup).
    """
    detail = run_store.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL)
    return WhitepaperRunDetailResponse(
        id=detail.id,
        rld_name_input=detail.rld_name_input,
        application_number=detail.application_number,
        application_type=detail.application_type,
        ingredient=detail.ingredient,
        normalized_name=detail.normalized_name,
        spine=detail.spine,
        sections=detail.sections,
        warnings=detail.warnings,
        status=detail.status,
        populated_count=detail.populated_count,
        analyst_input_count=detail.analyst_input_count,
        verified_absent_count=detail.verified_absent_count,
        source_audit_id=detail.source_audit_id,
        created_by=detail.created_by,
        created_by_user_id=detail.created_by_user_id,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
        finalized_at=detail.finalized_at,
        finalized_by=detail.finalized_by,
        inputs={iv.cell_id: _input_out(iv) for iv in detail.inputs},
    )


@protected.post("/whitepaper/runs/{run_id}/cells/{cell_id}", response_model=WhitepaperCellResponse)
def whitepaper_run_set_cell(
    run_id: int, cell_id: str, req: WhitepaperCellRequest, user: User = Depends(require_user)
) -> WhitepaperCellResponse:
    """Set (or clear) one attributed analyst overlay cell (org-shared edit).

    A ``null`` or empty-after-cleaning value clears the cell. No query rate
    limit: a pure bounded DB write, no FDA/LLM call. The store owns the domain
    rules; this boundary maps its typed errors -- 404 missing run, 409
    final-frozen or lost-the-concurrent-insert (retry), 422 unknown cell /
    oversized value.
    """
    try:
        view = run_store.upsert_input(
            run_id=run_id, cell_id=cell_id, value=req.value or "", user_id=_user_pk(user)
        )
    except run_store.RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL) from exc
    except (run_store.RunFinalizedError, run_store.ConcurrentEditError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (run_store.InvalidCellError, run_store.InputTooLongError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if view is None:
        return WhitepaperCellResponse(run_id=run_id, cell_id=cell_id, cleared=True, input=None)
    return WhitepaperCellResponse(
        run_id=run_id, cell_id=cell_id, cleared=False, input=_input_out(view)
    )


@protected.post("/whitepaper/runs/{run_id}/finalize", response_model=WhitepaperRunStatusResponse)
def whitepaper_run_finalize(
    run_id: int, user: User = Depends(require_user)
) -> WhitepaperRunStatusResponse:
    """draft -> final: freezes the analyst layer; open to any analyst, audited.

    The store re-verifies the stored sections fingerprint FIRST -- a mismatch
    is stored-data corruption (500 + Sentry), never a client error.
    """
    try:
        run_store.finalize_run(run_id=run_id, user_id=_user_pk(user))
    except run_store.RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL) from exc
    except run_store.RunFinalizedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except run_store.IntegrityMismatchError as exc:
        raise _stored_corruption_500(run_id, "whitepaper_run_integrity_mismatch") from exc
    _log_run_workflow(user, run_id, status="finalized")
    return WhitepaperRunStatusResponse(run_id=run_id, status="final")


@protected.post("/whitepaper/runs/{run_id}/reopen", response_model=WhitepaperRunStatusResponse)
def whitepaper_run_reopen(
    run_id: int, user: User = Depends(require_user)
) -> WhitepaperRunStatusResponse:
    """final -> draft: clears the finalize stamp; open to any analyst, audited."""
    try:
        run_store.reopen_run(run_id=run_id, user_id=_user_pk(user))
    except run_store.RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL) from exc
    except run_store.RunNotFinalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _log_run_workflow(user, run_id, status="reopened")
    return WhitepaperRunStatusResponse(run_id=run_id, status="draft")


@protected.post("/whitepaper/runs/{run_id}/docx")
def whitepaper_run_docx(run_id: int, user: User = Depends(require_user)) -> Response:
    """Render the Word document FROM the saved run -- no client echo, no re-populate.

    Zero live fetches, zero LLM calls: the .docx renders the STORED generated
    layer after re-verifying ``result_fingerprint(sections) == sections_sha256``
    (a mismatch is stored-data corruption: 500 + Sentry, no document), with the
    attributed analyst overlay applied per the writer's INV-3 discipline. The
    official template is lazily fetched on first use (ensure_template); any
    fetch failure keeps the loud FALLBACK_MARKER path. Keeps the /query rate
    limiter (docx assembly is CPU-bound) and writes one lightweight audit row
    (mode="whitepaper", docx_rendered).
    """
    _enforce_query_rate_limit(user)
    detail = run_store.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL)
    if result_fingerprint(detail.sections) != detail.sections_sha256:
        raise _stored_corruption_500(run_id, "whitepaper_run_integrity_mismatch")
    appl_no = detail.application_number
    if not _DOCX_APPL_NO_RE.fullmatch(appl_no):
        # create_run normalizes to six digits, so anything else is corrupted
        # stored data (same 500 class as the fingerprint mismatch) -- CR/LF or
        # quotes never reach the Content-Disposition header.
        raise _stored_corruption_500(run_id, "whitepaper_run_unsafe_application_number")
    inputs = {
        iv.cell_id: {
            "value": iv.value,
            "author": iv.author or "unknown",
            "updated_at": iv.updated_at.isoformat(),
        }
        for iv in detail.inputs
    }
    s = get_settings()
    template_path = template_fetch.ensure_template(
        s.whitepaper_template_path, s.whitepaper_template_url
    )
    result = {"spine": detail.spine, "sections": detail.sections, "warnings": detail.warnings}
    data = write_whitepaper_docx(result, template_path=template_path, inputs=inputs)
    log_query(
        mode="whitepaper",
        query_text=f"whitepaper docx run_id={run_id} application_number={appl_no!r}",
        retrieved=[],
        answer_text=f"Rendered the white-paper .docx from run #{run_id} (no re-population).",
        citations=[],
        refused=False,
        model_name="(docx-render)",
        user_id=str(user.id),
        status="docx_rendered",
        route_json={
            "route": "whitepaper",
            "reason": "docx_render",
            "run_id": run_id,
            "source_audit_id": detail.source_audit_id,
            "application_number": appl_no,
        },
    )
    return Response(
        content=data,
        media_type=docx_media_type(),
        headers={"Content-Disposition": f'attachment; filename="whitepaper_{appl_no}.docx"'},
    )


@protected.delete("/whitepaper/runs/{run_id}")
def whitepaper_run_delete(run_id: int, user: User = Depends(require_user)) -> dict[str, Any]:
    """Creator-only (403), drafts-only (409): a finalized paper is a record."""
    try:
        run_store.delete_run(run_id=run_id, user_id=_user_pk(user))
    except run_store.RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND_DETAIL) from exc
    except run_store.RunNotOwnedError as exc:
        raise HTTPException(status_code=403, detail="only the run's creator may delete it") from exc
    except run_store.RunFinalizedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True, "run_id": run_id}


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
def watch_latest(
    since: datetime | None = None,
    # Bounded page, not a hard window: without offset, alerts past the newest
    # `limit` rows were permanently unreachable through the API (one big watch
    # run can insert more than a page in a single batch).
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    since_utc = _as_utc(since) if since else None
    # Push `since` into SQL (applied BEFORE the row cap) so a genuinely-recent
    # alert is never dropped by the limit; the prior code capped by created_at
    # then filtered captured_at in Python, which could hide recent rows.
    records = latest_digest_records(limit=limit, offset=offset, since=since_utc)
    # Same-filter SQL COUNT: `total` is the full matching count, so clients can
    # tell a truncated page from the whole feed (count == len(page) only).
    total = count_digest_records(since=since_utc)
    if since_utc is not None:
        # Backstop: the SQL compare is lexicographic over the stored NAIVE-UTC
        # isoformat captured_at strings (alerts._since_key normalizes `since` to
        # that shape), so re-filter in Python to additionally drop any row whose
        # captured_at fails to parse (excluded, never a 500).
        records = [
            r
            for r in records
            if (captured_at := _record_captured_at(r)) is not None and captured_at >= since_utc
        ]
    return {
        "count": len(records),
        "total": total,
        "limit": limit,
        "offset": offset,
        "alerts": records,
        # Newest COMPLETED watch run from the durable ledger (null = never
        # ran). The alert list alone cannot distinguish a quiet day from a
        # cron that has been dead for a week -- this can (INV-4: it is only
        # ever a run that actually happened, never inferred).
        "last_run": latest_watch_run(),
    }


# ---------- /products ----------
# INV-5 at the API boundary: POST /products is a HUMAN assertion, so it may
# only claim provenance a human can actually stand behind. "drugsfda" (also in
# watchlist.ALLOWED_SOURCES) means "machine-verified against the automated
# Drugs@FDA import"; accepting it on a hand-typed row would fabricate that
# verification. Kept as an explicit literal (not ALLOWED_SOURCES minus
# drugsfda) so a future machine source fails CLOSED here by default.
USER_ASSERTABLE_SOURCES = frozenset({"manual", "anda_letter"})


class ProductCreate(BaseModel):
    # Persisted to the watchlist — cap free-text fields for the same reason the
    # rest of the surface does (consistency + bounded per-row storage).
    active_ingredient: str = Field(..., min_length=1, max_length=200)
    dosage_form: str | None = Field(None, max_length=200)
    route: str | None = Field(None, max_length=200)
    rld_name: str | None = Field(None, max_length=200)
    rld_application_number: str | None = Field(None, max_length=40)
    company_status: str | None = Field(None, max_length=200)
    source: str = Field(
        ...,
        max_length=200,
        description=(
            f"one of {sorted(USER_ASSERTABLE_SOURCES)}; 'drugsfda' rows come only "
            "from the automated Drugs@FDA import (INV-5)"
        ),
    )
    source_url: str | None = Field(None, max_length=2000)

    @field_validator("active_ingredient")
    @classmethod
    def _require_non_blank_ingredient(cls, v: str) -> str:
        # A whitespace-only name normalizes to "" -- permanently unmatchable
        # junk (DELETE /products only soft-unwatches: the row itself is kept
        # forever for alert-history integrity). Reject at the boundary; store
        # the stripped form so the row matches what was meant.
        v = v.strip()
        if not v:
            raise ValueError("active_ingredient must not be blank")
        return v


@protected.get("/products")
def list_products() -> dict[str, Any]:
    items = list_watchlist()
    return {"count": len(items), "products": items}


@protected.post("/products", status_code=201)
def create_product(req: ProductCreate) -> dict[str, Any]:
    if req.source not in USER_ASSERTABLE_SOURCES:
        # "drugsfda" is a real source value, so the generic "must be one of"
        # would read as a typo rather than the policy it is.
        if req.source == "drugsfda":
            detail = (
                "source 'drugsfda' is machine-verified provenance: those rows "
                "come only from the automated Drugs@FDA import, never manual "
                f"entry (INV-5). Use one of {sorted(USER_ASSERTABLE_SOURCES)}."
            )
        else:
            detail = f"source must be one of {sorted(USER_ASSERTABLE_SOURCES)} (INV-5)"
        raise HTTPException(status_code=422, detail=detail)
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


@protected.delete("/products/{product_id}")
def delete_product(product_id: int) -> dict[str, Any]:
    """Remove a product from the watchlist (SOFT: the row is kept).

    ``on_watchlist`` flips to False instead of deleting the row -- durable
    alert rows reference ``product_id``, so a hard delete would orphan the
    alert history the feed still renders (INV-4), and the row's INV-5
    provenance survives for audit. Idempotent: re-deleting an already-unwatched
    row still returns ``removed: true`` because the caller's goal state holds;
    404 is reserved for ids no Product row ever had, mirroring the "does it
    exist" contract of the other 404s on this surface.
    """
    if not set_on_watchlist(product_id, False):
        raise HTTPException(status_code=404, detail="product not found")
    return {"removed": True, "products": list_watchlist()}


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
                    # Tier-2: rehydrated turns keep their provenance + next-step
                    # affordances so a reloaded conversation is fully interactive.
                    "audit_id": m.audit_id,
                    "reason": m.reason,
                    "interpretation": m.interpretation,
                    "clarify": list(m.clarify_json or []),
                    "related": list(m.related_json or []),
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
        }


@protected.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, user: User = Depends(require_user)) -> None:
    with session_scope() as s:
        row = _owned_session_or_404(s, session_id, str(user.id))
        # One bulk DELETE for the messages instead of a per-row ORM delete
        # (ChatMessage has no ORM cascades, so a set-based delete is equivalent).
        s.execute(sa_delete(ChatMessage).where(col(ChatMessage.session_id) == session_id))
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
