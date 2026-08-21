"""FastAPI surface.

This is the clean boundary the IT/AI team will wrap or replace. Every
response is reproducible in Postman from a `.env` and a running instance.

Endpoints (per spec §10.16):
    POST   /query          — grounded Q&A (auth)
    POST   /query/stream   — grounded Q&A, streamed as Server-Sent Events (auth)
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
    POST   /deficiency/analyze - upload a submission PDF for deficiency analysis (auth)
    GET    /deficiency/runs - org-shared deficiency analysis runs (auth)
    GET    /deficiency/runs/{id} - one run + its fault report (auth)
    GET    /watch/latest   — recent alerts (auth)
    GET    /psg/documents  - PSG reference-library catalog for the Studio rail (auth)
    GET    /psg/documents/{id}/pdf - stream one PSG PDF inline (auth)
    HEAD   /psg/documents/{id}/pdf - availability probe, DB row only (auth)
    GET    /psg/documents/{id}/content - one PSG as studio blocks (auth)
    GET    /psg/documents/{id}/docx - the same PSG as a Word download (auth)
    GET    /psg/documents/{id}/requirements - what this PSG requires (auth)
    GET    /livez          - process liveness only, touches no dependency (open)
    GET    /health         — liveness + component diagnostics (open)
    GET    /ready          - readiness: db + vector store + LLM constructable (open)
    GET    /metrics        - Prometheus counters from the query_log audit
                              (open by default; bearer-gated when METRICS_TOKEN set)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Literal

import anyio.to_thread
import httpx
from config.settings import Settings, get_settings
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text, update
from sqlalchemy import select as sa_select
from sqlmodel import col

from regwatch.assemble.dossier import build_dossier
from regwatch.auth.deps import require_user
from regwatch.common.audit import log_query
from regwatch.common.conversation import SESSION_FILTER_KEYS, SessionOwnershipError
from regwatch.common.logging import configure_logging, get_logger
from regwatch.common.observability import capture_exception, init_sentry
from regwatch.common.ratelimit import query_limiter
from regwatch.common.text_normalize import stripped_name
from regwatch.deficiency.runner import run_deficiency_analysis, run_studio_check
from regwatch.generate.grounded_qa import QAResult, QueryStatusLiteral, ask, compute_turn
from regwatch.generate.llm import assert_llm_runtime_available
from regwatch.generate.prompts import active_grounded_qa_prompt
from regwatch.generate.rag_contract import AuditPayload, RagOutcome, SessionPatch
from regwatch.ingest.psg_crawler import (
    BROWSER_UA,
    PdfInvalidError,
    PdfTooLargeError,
    _RetryableHTTP,
    download_pdf,
)
from regwatch.process.embedder import assert_embedding_runtime_available
from regwatch.process.psg_document import (
    PsgChunkText,
    PsgDocumentBody,
    build_body,
    document_file_name,
)
from regwatch.process.psg_docx import (
    DOCX_MEDIA_TYPE,
    PsgDocxMeta,
    safe_file_stem,
    write_psg_docx,
)
from regwatch.sources.router import search_sources
from regwatch.sources.types import SourceKind as InternalSourceKind
from regwatch.sources.types import SourceQuery
from regwatch.store import chemistry as chemistry_store
from regwatch.store import deficiency_runs as deficiency_run_store
from regwatch.store import whitepaper_runs as run_store
from regwatch.store.db import engine_dialect, get_engine, init_db, session_scope
from regwatch.store.models import (
    ChatSession,
    QueryLog,
    User,
    WhitepaperRun,
)
from regwatch.store.queries import (
    PsgDocumentDetail,
    count_psg_documents,
    fetch_psg_document_detail,
    fetch_psg_pdf_source,
    fetch_psg_requirements,
    list_psg_documents,
)
from regwatch.store.vector_store import collection_size, document_chunks
from regwatch.watch.alerts import count_digest_records, latest_digest_records
from regwatch.watch.runs import latest_watch_run
from regwatch.whitepaper import template_fetch
from regwatch.whitepaper.docx_writer import docx_media_type, write_whitepaper_docx
from regwatch.whitepaper.populator import (
    SpineResolutionError,
    WhitepaperBuildTimeoutError,
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
        "Fix: set INGEST_EMBEDDING_PROVIDER=openai and LLM_PROVIDER=openai, "
        "or set REGWATCH_ALLOW_TEST_PROVIDERS=1 to "
        "explicitly allow test providers."
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
    # Same fail-loud posture for the session cookie. The cookie is MINTED by
    # the Go proxy since the step-4 cutover (which logs its own boot warning
    # for this misconfig); [env] is app-wide on Fly, so warning here too is a
    # redundant tripwire on the same silent gap, at zero cost.
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
    if s.retrieval_corpus == "authoritative_fda":
        from regwatch.corpus.status import assert_authoritative_corpus_ready_for_activation

        assert_authoritative_corpus_ready_for_activation()
    # An unset EMBEDDING_PROVIDER, or one whose endpoint credentials are
    # missing, must refuse to boot -- not 500 on the first embed call.
    assert_embedding_runtime_available(s.embedding_provider)
    # Same posture for generation: every answer turn synthesizes, so an unset
    # LLM_PROVIDER (or a missing OPENAI_API_KEY) is a
    # misconfigured deployment and refuses HERE -- not lazily, refusing every
    # question while /health reads green. Scoped to this lifespan on purpose:
    # the corpus worker (dagster-daemon) and the CLI commands never run it,
    # so non-generating entrypoints gain no generation requirement.
    assert_llm_runtime_available(s.llm_provider)
    # Which synthesis prompt this process will serve is a FLAG read, not a build
    # artifact: a secret flip swaps v7 for v6 with no deploy, and until now the
    # only record of the answer policy in force was inferred from audit rows
    # after the fact. One line at boot, with the same identity fields the
    # per-turn llm_prompt line carries, so a machine's log says it outright.
    log.info(
        "qa_prompt_active",
        prose=s.prose_synthesis_enabled,
        selective=s.selective_citation_enabled,
        **active_grounded_qa_prompt().log_fields(),
    )
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
    # The contract opens a fixed set of unauthenticated operational probes
    # (GET /livez, /health, /ready, /metrics) and nothing else -- every product
    # route sits behind the `protected` router below.
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
    """Register the 503 handler for the LLM SDK base error when importable.

    The Responses provider uses the OpenAI SDK, so registering its base error
    class catches all of its subclasses (timeout /
    connection / rate-limit / 5xx status). Kept lazy so the API does not
    hard-depend on the SDK in echo-only environments (tests/CI).
    """
    try:
        from openai import APIError as _OpenAIAPIError
    except Exception:  # SDK not installed in this environment
        return
    target.add_exception_handler(_OpenAIAPIError, _handle_upstream_error)


_register_upstream_error_handlers(app)

# Single authorization chokepoint: every endpoint except the app-level open
# probes (GET /livez, /health, /ready, /metrics) is registered on this router,
# so its router-level dependency makes an accidentally-unauthenticated route
# impossible. The /auth + /sessions surface lives in the Go proxy now
# (go/internal/api, step 4 of docs/POLYGLOT_TARGET_2026-07-10.md); Python
# keeps only the VERIFY side of the auth contract (require_user ->
# resolve_token over the same auth_session rows the Go binary mints).
protected = APIRouter(dependencies=[Depends(require_user)])


# ---------- /livez ----------
class LivezResponse(BaseModel):
    status: Literal["ok"]


# On `app`, not `protected`: the platform probe carries no session cookie.
# include_in_schema=False for the same reason as /internal/query/compute -- no
# client calls this, so it stays out of the frozen OpenAPI snapshot the TS
# codegen consumes. `async def` keeps it off the shared anyio worker pool that
# every sync-def endpoint borrows from (see _ASK_LIMITER): a liveness answer
# that can queue behind slow request work is not a liveness answer.
@app.get("/livez", response_model=LivezResponse, include_in_schema=False)
async def livez() -> dict[str, str]:
    """Process liveness only: this process is up and running handlers.

    Deliberately blind to DB, vector store, LLM provider, and embedding-profile
    state -- it touches no dependency at all, and that is the entire contract.
    It exists so the proxy's Fly rotation check can traverse edge -> proxy ->
    6PN -> uvicorn (proving the upstream is actually reachable) WITHOUT waking
    Lakebase: the end-to-end /health check it replaces held a DB session open
    every 30s per machine, so the Autoscaling instance never reached its
    scale-to-zero window (commit 35102e1). Component diagnostics stay on
    /health; dependency readiness stays on /ready.
    """
    return {"status": "ok"}


# ---------- /health ----------
def _db_component() -> dict[str, Any]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        # B1: expose the dialect so operators / the uptime check can assert
        # 'postgresql' -- any other value must read as visibly wrong.
        return {"ok": True, "dialect": engine_dialect()}
    except Exception as exc:
        # /health is the one anonymous-reachable endpoint: never return the raw
        # exception (it discloses DB host/port/name + driver) to the caller.
        # Keep the detail server-side / in Sentry.
        log.warning("health_db_unreachable", error=str(exc))
        capture_exception(exc)
        return {"ok": False, "error": "unreachable"}


def _vector_store_component() -> dict[str, Any]:
    try:
        return {"ok": True, "corpus_count": collection_size()}
    except Exception as exc:
        log.warning("health_vector_store_unreachable", error=str(exc))
        capture_exception(exc)
        return {"ok": False, "error": "unreachable"}


def _embedding_component() -> dict[str, Any]:
    """What ACTUALLY embeds a query -- not what EMBEDDING_PROVIDER claims.

    Retrieval picks its arm from ACTIVE_EMBEDDING_PROFILE (retrieve/retriever.py);
    only the "legacy" arm ever reads EMBEDDING_PROVIDER. Reporting the raw
    setting made this probe answer "openai" while queries were in fact embedded
    by the Databricks-hosted profile model -- the exact inverse of the data
    residency question an operator opens /health to answer.

    The profile ID is reported because the provider NAME alone cannot identify
    which vector geometry is live when several profiles share a provider. The
    profile's model name is deliberately NOT reported: /health is the one
    anonymous-reachable endpoint, and an internal serving-endpoint name is a
    disclosure this probe does not need to make.
    """
    s = get_settings()
    profile_id = (s.active_embedding_profile or "legacy").strip()
    if profile_id == "legacy":
        return {"provider": s.embedding_provider or "unset", "profile": "legacy"}
    try:
        from regwatch.store.embedding_profiles import get_embedding_profile

        provider = get_embedding_profile(profile_id).provider
    except Exception as exc:
        # An unresolvable profile must not fail the probe: the ID is still the
        # truthful answer to "which arm is live", and only the provider name
        # needed the lookup. Same disclosure rule as _db_component.
        log.warning("health_embedding_profile_unresolved", profile=profile_id, error=str(exc))
        capture_exception(exc)
        return {"provider": "unresolved", "profile": profile_id}
    return {"provider": provider, "profile": profile_id}


def _llm_key_present(s: Settings) -> bool:
    if s.llm_provider == "openai":
        # Added with the 2026-08-20 cutover. Without this branch a correctly
        # configured OpenAI deployment reported key_present=false on /health --
        # a health endpoint claiming a credential is missing when it is not is
        # as bad as one claiming it is present when it is not.
        return bool(s.openai_api_key and s.openai_llm_model)
    # echo needs no key; an UNSET provider is a misconfiguration, not a
    # keyless-but-healthy state, and must not read as ok on /health.
    return s.llm_provider == "echo"


# /health and /ready predate their response models and their wire contract is
# conditional KEY PRESENCE (e.g. db carries `dialect` on success XOR `error` on
# failure; `allow_test_providers` appears only when true). The models declare
# every possible key; response_model_exclude_none reproduces the exact
# presence semantics - no null-filled keys may appear that were absent before.
class HealthDbComponent(BaseModel):
    ok: bool
    dialect: str | None = None
    error: str | None = None


class HealthVectorComponent(BaseModel):
    ok: bool
    corpus_count: int | None = None
    error: str | None = None


class HealthLlmComponent(BaseModel):
    provider: str
    key_present: bool


class HealthEmbeddingComponent(BaseModel):
    provider: str
    # The ACTIVE_EMBEDDING_PROFILE arm ("legacy", or an ep_ profile ID). Two
    # profiles can share a provider name, so the ID is what actually pins the
    # vector geometry a query is embedded into.
    profile: str


class HealthComponents(BaseModel):
    db: HealthDbComponent
    vector_store: HealthVectorComponent
    llm: HealthLlmComponent
    embedding: HealthEmbeddingComponent


class HealthResponse(BaseModel):
    status: Literal["ok", "unhealthy"]
    components: HealthComponents
    whitepaper_template: Literal["present", "fetchable", "absent"]
    warnings: list[str]
    allow_test_providers: bool | None = None


@app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
def health(response: Response) -> dict[str, Any]:
    """Diagnose the stack: db, pgvector, providers. Superset of {"status": "ok"}.

    503 only when the DB or the vector store is unreachable. An empty corpus is
    healthy (with a warning) so a fresh stack can boot and the ingest can seed.
    """
    s = get_settings()
    db = _db_component()
    vector_store = _vector_store_component()
    embedding = _embedding_component()
    warnings: list[str] = []
    if vector_store["ok"] and vector_store["corpus_count"] == 0:
        warnings.append("corpus is empty — run `regwatch seed` (or the compose ingest profile)")
    # The EFFECTIVE embedding provider, not the raw setting: with a profile
    # active, EMBEDDING_PROVIDER=echo is inert and warning about it would be
    # noise, while an echo PROFILE would previously have gone unwarned.
    if embedding["provider"] == "echo" or s.llm_provider == "echo":
        warnings.append("test-grade 'echo' provider in use — retrieval quality is degraded")
    body: dict[str, Any] = {
        "status": "ok",
        "components": {
            "db": db,
            # pgvector is the only vector backend; the component key is
            # deliberately backend-neutral (see DEPLOY.md).
            "vector_store": vector_store,
            "llm": {"provider": s.llm_provider, "key_present": _llm_key_present(s)},
            "embedding": embedding,
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
    if not (db["ok"] and vector_store["ok"]):
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


class ReadyChecks(BaseModel):
    db: bool
    vector_store: bool
    llm: bool
    # Present ONLY when the boot RLS sweep left a public table unprotected (the
    # same conditional-key contract as /health): a healthy body keeps its exact
    # three-key shape, so existing probes are unaffected.
    rls: bool | None = None


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadyChecks
    # Present only in the not_ready body (see the exclude_none note on /health).
    failed: Literal["db", "vector_store", "llm", "rls"] | None = None
    detail: str | None = None


@app.get("/ready", response_model=ReadyResponse, response_model_exclude_none=True)
def ready(response: Response) -> dict[str, Any]:
    """Readiness probe: 200 only when the DB + vector store are reachable AND the
    LLM client is constructable (key present). Distinct from /health's liveness:
    a load balancer routes traffic on this. No paid LLM call is made - only the
    cheap reachability checks. Both are timeout-bounded by the per-connection
    connect/statement timeouts on the shared engine (the vector-store probe is a
    `SELECT count(*)` in pgvector mode, so a degraded DB is capped by
    DB_STATEMENT_TIMEOUT rather than hanging the probe). 503 names the FIRST
    failed check so an operator sees what to fix.

    Also fails CLOSED on row level security: boot deliberately tolerates a
    lock-contended `ALTER ... ENABLE ROW LEVEL SECURITY` (the 2026-06-18
    incident design), so this is where a still-unprotected public table -- which
    is readable over a PostgREST-style Data API -- stops being SILENT. No
    extra DB round trip: the set is what the boot sweep recorded.
    """
    # Scope of the RLS gate, kept OUT of the docstring because docstrings are
    # exported verbatim into the public OpenAPI description (openapi.json):
    # fly.toml health-checks /health, both in [[http_service.checks]] and
    # [checks.app_health] -- NOT /ready. So this 503 does not pull the machine
    # from Fly rotation today. The load-bearing alert is the Sentry capture in
    # store.db._record_unprotected_tables; /ready is the machine-level signal an
    # operator or a future gateway check reads.
    #
    # Function-local: a module-level import down here trips ruff E402, and this
    # readiness-only dependency does not belong in the module header.
    from regwatch.store.db import unprotected_public_tables

    db = _db_component()
    vector_store = _vector_store_component()
    llm_ok, llm_reason = _llm_ready(get_settings())
    checks: dict[str, bool] = {
        "db": db["ok"],
        "vector_store": vector_store["ok"],
        "llm": llm_ok,
    }
    unprotected = unprotected_public_tables()
    if unprotected:
        # Added only on failure: the healthy body must stay byte-identical to
        # what probes already parse (see the ReadyChecks.rls note).
        checks["rls"] = False
    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    failed = next(name for name, ok in checks.items() if not ok)
    if failed == "llm":
        detail = llm_reason
    elif failed == "rls":
        # Count only. /ready is anonymous-reachable, so naming the unprotected
        # tables would hand an anon-key holder the exact targets; the names go
        # to the log + Sentry (store.db._record_unprotected_tables).
        detail = f"{len(unprotected)} public table(s) missing row level security"
    else:
        detail = f"{failed} is unreachable"
    response.status_code = 503
    return {
        "status": "not_ready",
        "checks": checks,
        "failed": failed,
        "detail": detail,
    }


# ---------- /metrics ----------
def _query_log_counters() -> dict[str, int]:
    """Aggregate query_log into counters for /metrics in ONE grouped query (no
    N+1). Keys: total, refused, per-mode totals, and route-shadow outcomes.
    A DB error yields an empty dict so /metrics degrades to the static help/type
    lines rather than 500-ing the scrape.
    """
    counters: dict[str, int] = {}
    try:
        with session_scope() as s:
            route_outcome = col(QueryLog.route_json)["route_call"]["outcome"].as_string()
            compile_status = col(QueryLog.route_json)["route_call"]["compile_status"].as_string()
            for mode, refused, shadow_outcome, shadow_compile, n in s.execute(
                sa_select(
                    col(QueryLog.mode),
                    col(QueryLog.refused),
                    route_outcome,
                    compile_status,
                    func.count(),
                ).group_by(
                    col(QueryLog.mode),
                    col(QueryLog.refused),
                    route_outcome,
                    compile_status,
                )
            ):
                count = int(n)
                counters["total"] = counters.get("total", 0) + count
                if refused:
                    counters["refused"] = counters.get("refused", 0) + count
                counters[f"mode:{mode}"] = counters.get(f"mode:{mode}", 0) + count
                if shadow_outcome:
                    key = f"route_shadow:{shadow_outcome}"
                    counters[key] = counters.get(key, 0) + count
                if shadow_compile:
                    key = f"route_compile:{shadow_compile}"
                    counters[key] = counters.get(key, 0) + count
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
    route_outcomes = ("success", "provider_error", "invalid", "request_error")
    lines += [
        "# HELP regwatch_route_shadow_calls_total Audited route-shadow calls by outcome.",
        "# TYPE regwatch_route_shadow_calls_total counter",
    ]
    for outcome in route_outcomes:
        value = counters.get(f"route_shadow:{outcome}", 0)
        lines.append(f'regwatch_route_shadow_calls_total{{outcome="{outcome}"}} {value}')
    failures = sum(
        counters.get(f"route_shadow:{outcome}", 0)
        for outcome in route_outcomes
        if outcome != "success"
    )
    lines += [
        "# HELP regwatch_route_shadow_failures_total Audited unsuccessful route-shadow calls.",
        "# TYPE regwatch_route_shadow_failures_total counter",
        f"regwatch_route_shadow_failures_total {failures}",
        "# HELP regwatch_route_shadow_compilations_total Route-shadow scope compilations by status.",
        "# TYPE regwatch_route_shadow_compilations_total counter",
    ]
    for status in ("success", "error", "not_attempted"):
        value = counters.get(f"route_compile:{status}", 0)
        lines.append(f'regwatch_route_shadow_compilations_total{{status="{status}"}} {value}')
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
    # Per-request opt-in for the provisional draft SSE channel. Ignored by the
    # blocking /query route and whenever the server-side dual gate is off.
    live_draft: bool = False
    # Which surface this turn's session bookkeeping belongs to (issue #208):
    # "thread" (default) is the analyst's real work, listed in the work rail's
    # Threads list; "assistant" is the Research Studio panel's own scratch
    # conversation, kept but filtered out of that list. Declared LAST so a
    # multi-field validation failure reports errors in this declared order
    # (pydantic parity note: Go's validationItem mirrors this field order too).
    origin: Literal["thread", "assistant"] = "thread"

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
    # Human-identifying provenance: short_name is "PSG_<appl_no>", an FDA
    # application number, which names nothing a reader can act on. These four
    # let the client render "Beclomethasone Dipropionate - Inhalation Aerosol
    # PSG, revised Mar 2021" and fall back to short_name when absent (a
    # citation persisted before this shipped).
    product_name: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    psg_type: str | None = None


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
    # Set only on /query/stream turns that painted at least one provisional
    # draft frame which the gate then withdrew (refuse/clarify/error/meta/
    # scope_warning) or partially dropped. The client keys its withdrawal
    # note on this server-declared value -- never on text diffing.
    draft_withdrawn: str | None = None


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
    """Serialize domain citations to the wire, enriched with score.

    score is copied from the audited retrieval by chunk_id (never recomputed;
    null when no passage matches).

    Recency is NO LONGER joined here. grounded_qa._enrich_citation_recency runs
    the same batched lookup before the turn is persisted, so the domain citation
    already carries recommended_date/diff_summary and history keeps them. This
    boundary only parses the stored string to a date.
    """
    scores: dict[str, float | None] = {
        str(p.get("chunk_id")): p.get("score") for p in result.retrieved
    }
    out: list[QueryCitation] = []
    for c in result.citations:
        # Domain Citation may already carry a score; prefer an explicit retrieval
        # match by chunk_id, else fall back to the dataclass value.
        score = scores.get(c.chunk_id, c.score)
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
                recommended_date=_parse_iso_date(c.recommended_date),
                diff_summary=c.diff_summary,
                product_name=c.product_name,
                dosage_form=c.dosage_form,
                route=c.route,
                psg_type=c.psg_type,
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


def _shed_if_ask_pool_saturated() -> None:
    """Raise the defined 503 shed when the ask() worker pool is saturated.

    ONE helper shared by _dispatch_ask (public /query family) and the internal
    compute endpoint, so the flag-on Go control plane meets the same overload
    contract as flag-off clients -- without it, saturation degrades to Go-side
    240s deadline timeouts and synthesized upstream_error rows while the
    non-abandoning Python threads keep queueing. The saturation check is
    read-then-acquire: a request racing past it queues briefly instead of
    shedding, which only softens the bound -- steady-state saturation still
    returns the defined 503.

    The REGWATCH_FAULT_INJECT="saturate" seam forces the shed path for the
    contract suite (S27); same allow_test_providers boot fence as
    grounded_qa._maybe_inject_fault, so it is inert in production regardless
    of the env var.
    """
    forced = (
        os.environ.get("REGWATCH_FAULT_INJECT", "").strip() == "saturate"
        and get_settings().allow_test_providers
    )
    limiter = _ASK_LIMITER
    if forced or limiter.statistics().borrowed_tokens >= limiter.total_tokens:
        raise HTTPException(status_code=503, detail="server is busy, retry shortly")


async def _dispatch_ask(**kwargs: Any) -> QAResult:
    """Run ask() on its dedicated bounded worker pool; 503 when saturated.

    Like run_in_threadpool, the dispatched thread is non-abandoning on
    cancellation.
    """
    _shed_if_ask_pool_saturated()
    return await anyio.to_thread.run_sync(partial(ask, **kwargs), limiter=_ASK_LIMITER)


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
            origin=req.origin,
        )
    except SessionOwnershipError as exc:
        # An ownership race lost after the pre-check above — same 404 as any
        # other foreign session, never confirming the session exists.
        raise HTTPException(status_code=404, detail="session not found") from exc
    # _build_query_response queries citation recency — also off-loop.
    return await run_in_threadpool(_build_query_response, result)


def _sse_event(name: str, data: dict[str, Any]) -> str:
    """One Server-Sent Events frame. The Ask client (askQueryStream) parses five
    event names: ``status`` (``{"text": ...}`` progress), ``token`` (``{"delta":
    ...}`` a slice of the already-gated, already-audited answer), ``draft``
    (``{"delta": ...}`` LIVE un-gated provisional prose, dual-gated -- see
    REGWATCH_LIVE_DRAFT), ``draft_reset`` (``{}``, discard every draft delta
    received so far), and ``result`` (the full validated QueryResponse). Any
    other name is ignored, so we emit only these."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


# How long the SSE body may go quiet before we emit a comment keep-alive frame.
# Well under the ~60s idle timeout of typical proxies/load balancers.
_SSE_KEEPALIVE_INTERVAL_S = 15.0


async def _query_event_stream(req: QueryRequest, user_id: str) -> AsyncIterator[str]:
    """SSE body for POST /query/stream.

    Streams real pipeline progress as ``status`` frames and the answer as
    ``token`` frames, then that same answer as exactly ONE terminal ``result``
    frame. The ``token`` deltas are NOT a live model stream and never were a
    provisional draft under this contract: synthesis is one buffered json-mode
    call, and ask() replays the RENDERED answer only after the turn cleared the
    claim gate (INV-1) and its audit row was committed (INV-6), and only for an
    answer/summary turn -- a decline replays zero tokens. So a token delta is
    always a slice of the exact bytes the ``result`` frame will carry. The
    client is free to keep rendering them as a no-citation-surface draft that
    the ``result`` replaces (it does today, which is strictly conservative).
    ask() runs in a worker thread so its progress/token callbacks push onto the
    event loop while it works, and writes exactly one audit row internally (INV-6,
    never duplicated). Once ask() has been dispatched onto its thread it runs to
    completion even if the client then disconnects (the threadpool is
    non-abandoning), so that turn is still audited; a disconnect in the narrow
    window BEFORE dispatch cancels the work before it starts and writes no row —
    correct, since nothing ran to audit. On any unexpected failure the stream
    closes with no ``result`` frame, which makes the client fall back to blocking
    POST /query exactly once (any tokens already emitted are discarded).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def on_progress(textline: str) -> None:
        # Runs on the ask() worker thread — hand the line to the loop thread.
        loop.call_soon_threadsafe(queue.put_nowait, ("status", textline))

    def on_token(delta: str) -> None:
        # A slice of the gated, audited answer from the worker thread — cosmetic
        # only; the authoritative answer is still the terminal ``result`` frame.
        loop.call_soon_threadsafe(queue.put_nowait, ("token", delta))

    def on_draft(delta: str) -> None:
        # LIVE un-gated prose from the worker thread - provisional by
        # contract; the client renders it only as a draft.
        loop.call_soon_threadsafe(queue.put_nowait, ("draft", delta))

    def on_draft_reset() -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("draft_reset", None))

    async def _run() -> None:
        try:
            s = get_settings()
            draft_on = bool(s.live_draft_enabled and s.prose_synthesis_enabled and req.live_draft)
            result = await _dispatch_ask(
                question=req.question,
                filters=req.filters,
                k=req.k,
                session_id=req.session_id,
                user_id=user_id,
                origin=req.origin,
                on_progress=on_progress,
                on_token=on_token,
                on_draft=on_draft if draft_on else None,
                on_draft_reset=on_draft_reset if draft_on else None,
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
    draft_frames_sent = False
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
            if kind == "draft":
                draft_frames_sent = True
                yield _sse_event("draft", {"delta": payload})
                continue
            if kind == "draft_reset":
                yield _sse_event("draft_reset", {})
                continue
            if kind == "result":
                try:
                    # Recency enrichment does DB I/O — build the response off
                    # the event loop so a DB stall never freezes every stream.
                    response = await run_in_threadpool(_build_query_response, payload)
                except HTTPException:
                    log.warning("query_stream_missing_session_metadata")
                    return  # close without a result frame -> client falls back
                if draft_frames_sent:
                    from regwatch.generate.turn_gate import PARTIAL_DROP_DISCLOSURE

                    if response.status not in ("answer", "summary"):
                        response.draft_withdrawn = response.status
                    elif PARTIAL_DROP_DISCLOSURE in response.answer:
                        response.draft_withdrawn = "partial"
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
    as real HTTP statuses -- never mid-stream.
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


# ---------- /internal/query/compute (step-5 CompleteQuery) ----------
class InternalComputeRequest(BaseModel):
    """The Go control plane's compute request. Ids are minted by the caller;
    filters are already whitelisted at the Go edge (SESSION_FILTER_KEYS), so this
    internal, token-guarded endpoint trusts them."""

    question: str
    filters: dict[str, Any] | None = None
    k: int | None = None
    session_id: str
    turn_id: str
    user_id: str | None = None


def _require_internal_token(token: str | None) -> None:
    """Fail-closed guard: an unset INTERNAL_RAG_TOKEN disables the endpoint
    entirely (404, never confirming it exists); a set token must match exactly."""
    expected = get_settings().internal_rag_token
    if not expected or token != expected:
        raise HTTPException(status_code=404, detail="Not Found")


def _outcome_response_dict(outcome: RagOutcome) -> dict[str, Any]:
    """The wire QueryResponse for a computed outcome, MINUS audit_id.

    Built via the SAME _build_query_response the buffered/streaming paths use
    (so the shape can never drift), with the placeholder audit_id dropped -- the
    control plane owns the audit row and splices its real id onto the wire.
    """
    result = QAResult(
        answer=outcome.answer,
        citations=outcome.citations,
        refused=outcome.refused,
        model_name=outcome.model_name,
        audit_id=0,
        retrieved=outcome.retrieved,
        status=outcome.status,
        reason=outcome.reason,
        interpretation=outcome.interpretation,
        clarify=outcome.clarify,
        related=outcome.related,
        session_id=outcome.session_id,
        turn_id=outcome.turn_id,
    )
    body = _build_query_response(result).model_dump(mode="json")
    body.pop("audit_id", None)
    return body


def _persist_dict(audit: AuditPayload, patch: SessionPatch) -> dict[str, Any]:
    """The write instructions for the control plane: the query_log kwargs, the
    branch's audit failure semantics (allow_skip), the chat SessionPatch, and --
    for the strict answer path -- the fixed-copy fallback to write if the audit
    write fails (recursively serialized; its own fallback is always None)."""
    out: dict[str, Any] = {
        "audit_log_kwargs": audit.log_kwargs(),
        "allow_skip": audit.allow_skip,
        "patch": asdict(patch),
        "fallback": None,
    }
    if audit.failure_fallback is not None:
        fb_outcome, fb_audit, fb_patch = audit.failure_fallback
        out["fallback"] = {
            "response": _outcome_response_dict(fb_outcome),
            "audit_log_kwargs": fb_audit.log_kwargs(),
            "allow_skip": fb_audit.allow_skip,
            "patch": asdict(fb_patch),
        }
    return out


def _compute_payload(req: InternalComputeRequest) -> dict[str, Any]:
    """Runs the stateless core and serializes {response, persist}. All DB I/O
    (retrieval, resolver, the read-only citation-recency enrichment inside
    _build_query_response) happens here so it stays OFF the event loop."""
    outcome, audit, patch = compute_turn(
        req.question,
        filters=req.filters,
        k=req.k,
        session_id=req.session_id,
        turn_id=req.turn_id,
        user_id=req.user_id,
    )
    return {
        "response": _outcome_response_dict(outcome),
        "persist": _persist_dict(audit, patch),
    }


@app.post("/internal/query/compute", include_in_schema=False)
async def internal_query_compute(
    req: InternalComputeRequest,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Internal RAG compute for the Go control plane (step-5 CompleteQuery).

    Runs the STATELESS RAG core and returns {response, persist}; writes NOTHING
    (the caller owns the audit + chat-history writes, so INV-6 lives in one
    place). Token-guarded and fail-closed; never exposed at the public edge (the
    Go proxy 404s the /internal/ subtree). Compute runs OFF the event loop under
    the shared _ASK_LIMITER so a slow turn cannot starve the loop that still
    serves the relayed /query/stream and /healthz in the flag-gated phase.
    """
    _require_internal_token(x_internal_token)
    # Shed BEFORE queueing on the shared limiter (same 503 contract as
    # _dispatch_ask): without this, flag-on overload degrades to Go-side
    # deadline timeouts instead of the defined busy signal.
    _shed_if_ask_pool_saturated()
    return await anyio.to_thread.run_sync(partial(_compute_payload, req), limiter=_ASK_LIMITER)


# ---------- /sources/search ----------
class SourceKind(StrEnum):
    """The exact source universe exposed by the public API."""

    PSG = "psg"
    ORANGE_BOOK = "orange_book"
    DRUGSFDA = "drugsfda"
    ACTION_PACKAGE = "action_package"
    FDA_BE_GUIDANCE = "fda_be_guidance"


class SourceSearchRequest(BaseModel):
    # These fields are interpolated into outbound FDA query params; cap them so a
    # single authed caller can't push a multi-megabyte value into the request.
    query_text: str = Field("", max_length=1000)
    active_ingredient: str | None = Field(None, max_length=200)
    brand_name: str | None = Field(None, max_length=200)
    application_number: str | None = Field(None, max_length=40)
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
    # Fans out to authoritative FDA sources (Drugs@FDA and Orange Book), so
    # rate-limit it like the other outbound/expensive routes — an authed caller
    # must not be able to hammer FDA unthrottled (amplification / FDA-side block).
    _enforce_query_rate_limit(user)
    routed, records = search_sources(
        SourceQuery(
            query_text=req.query_text,
            active_ingredient=req.active_ingredient,
            brand_name=req.brand_name,
            application_number=req.application_number,
            dosage_form=req.dosage_form,
            route=req.route,
            limit=req.limit,
        ),
        sources=(
            [InternalSourceKind(source.value) for source in req.sources]
            if req.sources is not None
            else None
        ),
    )
    return SourceSearchResponse(
        routed_sources=[SourceKind(source.value) for source in routed],
        records=[
            SourceRecordResponse(
                source=SourceKind(r.source.value),
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


class WhitepaperResponse(BaseModel):
    """The populate result, verbatim.

    ``spine``/``sections`` are deliberately passthrough (plain dict/list, no
    nested models) for the same INV-3 reason as WhitepaperRunDetailResponse:
    ``_persist_whitepaper_run`` stores this exact payload BEFORE serialization,
    so a typed model that stripped or reshaped a field would make the stored
    run diverge from the HTTP response (tests pin the parity).
    """

    spine: dict[str, Any]
    sections: list[dict[str, Any]]
    warnings: list[str]
    audit_id: int
    # Always present: null only when persisting the run degraded (see
    # _persist_whitepaper_run), never absent.
    run_id: int | None


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


@protected.post("/whitepaper", response_model=WhitepaperResponse)
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
    except WhitepaperBuildTimeoutError as exc:
        # Audited inside build_whitepaper (status="error", reason=
        # "build_deadline_exceeded"). 504 matches the UI's own timeout
        # vocabulary -- api.ts maps its local aborts to status 504 too.
        raise HTTPException(status_code=504, detail=exc.detail) from exc
    _persist_whitepaper_run(_user_pk(user), req.rld_name, result)
    return result


# ---------- /resolve ----------
class ResolveRequest(BaseModel):
    rld_name: str = Field(..., min_length=1, max_length=200)
    application_number: str = Field(..., min_length=1, max_length=40)


class WhitepaperSplCandidate(BaseModel):
    setid: str
    title: str
    labeler: str | None
    published: str | None


class WhitepaperSpine(BaseModel):
    """The canonical spine ``populator._spine_from_ctx`` emits.

    Typed here (unlike the /whitepaper embedding, which must stay verbatim
    passthrough for stored-run parity) because /resolve persists nothing: the
    response IS the whole contract. application_type is a code-verified closed
    set - every assignment in populator resolves to an NDA/ANDA/BLA prefix.
    """

    application_number: str
    application_type: Literal["NDA", "ANDA", "BLA"]
    ingredient: str
    normalized_name: str
    product_numbers: list[str]
    setid: str | None
    spl_candidates: list[WhitepaperSplCandidate]
    approved_label_document_id: str | None
    approved_label_source_url: str | None
    approved_label_updated_at: str | None
    warnings: list[str]


@protected.post("/resolve", response_model=WhitepaperSpine)
def resolve(req: ResolveRequest, user: User = Depends(require_user)) -> dict[str, Any]:
    """Resolve an RLD name + application number to the canonical spine.

    Deterministic entity resolution, NOT an LLM turn: it writes NO audit row
    (success or failure) and returns no answer text -- it lets a surface pin a
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
    except WhitepaperBuildTimeoutError as exc:
        # Same fetch-phase deadline as /whitepaper; consistently, no audit row
        # (this surface writes none on success or failure).
        raise HTTPException(status_code=504, detail=exc.detail) from exc


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
    select, never the JSON payloads. Empty when the run vanished mid-request.

    DEFINED failure: this label only decorates the audit row, so a fault on this
    extra round-trip must not cost the row itself -- degrade to empty and let
    the INV-6 write proceed.
    """
    try:
        with session_scope() as s:
            appl_no = s.execute(
                sa_select(col(WhitepaperRun.application_number)).where(
                    col(WhitepaperRun.id) == run_id
                )
            ).scalar_one_or_none()
    except Exception as exc:
        log.warning(
            "whitepaper_run_appl_no_lookup_failed", run_id=run_id, error_type=type(exc).__name__
        )
        capture_exception(exc)
        return ""
    return appl_no or ""


def _log_query_safe(**kwargs: Any) -> None:
    """``log_query`` with a DEFINED failure: never raise.

    Mirrors ``assemble.dossier._log_query_safe`` / ``whitepaper.populator.
    _log_query_safe``. The finalize/reopen callers run this AFTER the store has
    COMMITTED the state transition, so a raising audit write would answer 500
    for a change the DB durably holds -- and the retry hits RunFinalizedError /
    RunNotFinalError -> 409, so no later request could write the row either.
    Log + Sentry-capture and return: a loud missing row, never a lying response.
    """
    try:
        log_query(**kwargs)
    except Exception as exc:
        log.warning("whitepaper_workflow_audit_write_failed", error_type=type(exc).__name__)
        capture_exception(exc)


def _log_run_workflow(user: User, run_id: int, *, status: str) -> None:
    """One QueryLog audit row per finalize/reopen -- the same generic audit
    trail docx_rendered rides on. model_name is a non-LLM marker (no model ran),
    consistent with "(docx-render)"."""
    appl_no = _run_application_number(run_id)
    _log_query_safe(
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


class WhitepaperRunDeleteResponse(BaseModel):
    deleted: bool
    run_id: int


@protected.delete("/whitepaper/runs/{run_id}", response_model=WhitepaperRunDeleteResponse)
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


class AlertRecord(BaseModel):
    """One durable alert as ``latest_digest_records`` returns it (the Alert
    dataclass fields plus the read-time-derived ``change_kind``)."""

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
    # Always set server-side today, but kept OPTIONAL in the schema on purpose:
    # the frontend deploys independently (Vercel vs Fly) and its consumers
    # deliberately tolerate an older backend that omits it (lib/api.ts falls
    # back to the prose marker). Requiring it here would invite client code
    # that breaks under that deploy skew.
    change_kind: Literal["new", "revised"] | None = None


class WatchRunSummary(BaseModel):
    """Telemetry of the newest COMPLETED watch run (the durable ledger row)."""

    started_at: str
    finished_at: str
    listings: int
    matched: int
    added: int
    revised: int
    unchanged: int
    errors: int
    alerts: int


class WatchLatestResponse(BaseModel):
    count: int
    total: int
    limit: int
    offset: int
    alerts: list[AlertRecord]
    # null = no watch run has ever recorded (never inferred; INV-4).
    last_run: WatchRunSummary | None


@protected.get("/watch/latest", response_model=WatchLatestResponse)
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


# ---------- /psg (reference library for the Compliance Studio rail) ----------
class PsgLibraryDoc(BaseModel):
    """One psg_document catalog row for the reference-library rail.

    ``stripped_name`` is derived at read time (text_normalize.stripped_name) so
    the client can salt-collapse ("albuterol sulfate" -> "albuterol") without
    shipping the salt-token table. ``psg_type`` stays a plain str (house
    precedent: AlertRecord's listing fields) -- the crawler only ever writes
    "draft"/"final", and a str never 500s on a legacy row.
    """

    id: int
    active_ingredient: str
    normalized_name: str
    stripped_name: str
    dosage_form: str | None
    route: str | None
    appl_no: str | None
    psg_type: str
    recommended_date: str | None
    source_url: str


class PsgDocumentListResponse(BaseModel):
    count: int
    total: int
    limit: int
    offset: int
    documents: list[PsgLibraryDoc]


class ChemistryStructure(BaseModel):
    """One drawable structure: a registry identity, never a model's words.

    ``match`` is "exact" when the stored row is the product's own salt/form
    and "parent" when only the salt-stripped compound is known; the client
    must say so in the caption. ``smiles`` is drawn client-side; ``source_url``
    is the PubChem page the figure can be checked against.
    """

    name: str
    pubchem_cid: int
    smiles: str
    inchikey: str | None = None
    molecular_formula: str | None = None
    molecular_weight: float | None = None
    iupac_name: str | None = None
    unii: str | None = None
    match: str
    source_url: str
    fetched_at: datetime


class ChemistryStructuresResponse(BaseModel):
    ingredient: str
    structures: list[ChemistryStructure]


@protected.get("/chemistry/structures", response_model=ChemistryStructuresResponse)
def chemistry_structures(
    # The citation's product_name (a normalized ingredient name, possibly
    # "a; b"). Bounded so the lookup key can never be a paragraph.
    ingredient: str = Query(..., min_length=1, max_length=200),
) -> dict[str, Any]:
    """Stored PubChem structures for a product name. DB read only, no egress.

    Empty ``structures`` means nothing is stored for that name (never looked
    up, not found, or ambiguous); the client hides the figure. A structure is
    never citable and is deliberately absent from the turn payload.
    """
    with session_scope() as s:
        views = chemistry_store.lookup_structures(s, ingredient)
    return {
        "ingredient": ingredient,
        "structures": [asdict(v) for v in views],
    }


@protected.get("/psg/documents", response_model=PsgDocumentListResponse)
def psg_documents(
    # One-shot by design: the rail buckets the whole catalog A-Z, and a partial
    # page cannot bucket correctly. Today's FDA index is ~1,795 PSGs (hard
    # ceiling ~2,400), so the default covers it in one call; offset stays as
    # the safety valve if the catalog ever outgrows the cap.
    limit: int = Query(2000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = list_psg_documents(limit=limit, offset=offset)
    total = count_psg_documents()
    return {
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": [
            {
                "id": r.id,
                "active_ingredient": r.active_ingredient,
                "normalized_name": r.normalized_name,
                "stripped_name": stripped_name(r.active_ingredient),
                "dosage_form": r.dosage_form,
                "route": r.route,
                "appl_no": r.appl_no,
                "psg_type": r.psg_type,
                "recommended_date": r.recommended_date,
                "source_url": r.source_url,
            }
            for r in rows
        ],
    }


# Digits-only guard so a corrupted appl_no can never reach Content-Disposition
# (same discipline as the docx route's application-number check).
_PSG_APPL_NO_RE = re.compile(r"\d{1,6}")
# source_url is server-ingested (the crawler builds it from the validated PSG
# template), but validate at the boundary anyway: this route must never be a
# generic fetch proxy.
_PSG_FDA_URL_PREFIX = "https://www.accessdata.fda.gov/"


def _psg_pdf_headers(*, appl_no: str | None, doc_id: int, etag: str) -> dict[str, str]:
    name = (
        f"PSG_{appl_no}.pdf"
        if appl_no and _PSG_APPL_NO_RE.fullmatch(appl_no)
        else f"psg-{doc_id}.pdf"
    )
    # `private`: authed content behind a cookie; ETag revalidation makes repeat
    # opens of the same PSG effectively free.
    return {
        "Content-Disposition": f'inline; filename="{name}"',
        "ETag": etag,
        "Cache-Control": "private, max-age=3600",
    }


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Lenient If-None-Match: tolerate weak validators and comma lists."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:]
        if value == etag or value == "*":
            return True
    return False


def _psg_proxy_client() -> httpx.Client:
    """Tighter budget than the crawler's ingest client: this fetch runs inside
    an interactive request holding an anyio threadpool token."""
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=5.0, read=20.0),
        headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
        follow_redirects=True,
    )


def _psg_local_candidates(
    pdf_path: str | None, appl_no: str | None, content_hash: str
) -> list[Path]:
    """Where a cached copy could live on this machine.

    Candidate 1 is the stored ``pdf_path`` (often an absolute path from the
    ingest machine -- expect misses on Fly's ephemeral disk). Candidate 2
    recomputes the crawler's own cache name, which is only derivable when
    ``appl_no`` is present (a NULL would build a "PSG_None_*" name that no
    crawler ever wrote).
    """
    candidates: list[Path] = []
    if pdf_path:
        candidates.append(Path(pdf_path))
    if appl_no:
        candidates.append(get_settings().raw_pdf_dir / f"PSG_{appl_no}_{content_hash[:8]}.pdf")
    return candidates


def _psg_local_pdf(pdf_path: str | None, appl_no: str | None, content_hash: str) -> bytes | None:
    """The VERIFIED cached bytes on this machine, or None.

    Reads and hash-checks the candidate before serving: an ETag derived from
    ``content_hash`` must never vouch for a truncated or foreign file sitting
    at the expected path. Unreadable paths and mismatched digests are misses,
    never raises -- the remote branch is the recovery path.
    """
    for candidate in _psg_local_candidates(pdf_path, appl_no, content_hash):
        try:
            if not candidate.is_file():
                continue
            data = candidate.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() == content_hash:
            return data
        log.warning("psg_pdf_cache_hash_mismatch", path=str(candidate))
    return None


def _psg_head_available(doc_pdf_path: str | None, appl_no: str | None, content_hash: str) -> bool:
    """Cheap (stat-only) availability answer for the HEAD probe.

    A row whose source_url is not an FDA PDF and that has no local file will
    502 on GET with certainty -- a 200 probe for it would defeat the probe.
    Existence alone (no read, no hash) keeps HEAD fast; a stale file that then
    fails GET-side verification still has the remote branch behind it.
    """
    for candidate in _psg_local_candidates(doc_pdf_path, appl_no, content_hash):
        try:
            if candidate.is_file():
                return True
        except OSError:
            continue
    return False


@protected.head("/psg/documents/{doc_id}/pdf")
def psg_document_pdf_head(doc_id: int) -> Response:
    """Availability probe for the Studio's PDF pane.

    Answers from the DB row plus at most two stat() calls -- never fda.gov,
    never the rate budget -- so the frontend can distinguish "this document
    can be served" from a transport failure before mounting the iframe.
    FastAPI does not auto-answer HEAD for GET routes, so this handler is
    load-bearing, not decoration.
    """
    doc = fetch_psg_pdf_source(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="psg document not found")
    if not doc.source_url.startswith(_PSG_FDA_URL_PREFIX) and not _psg_head_available(
        doc.pdf_path, doc.appl_no, doc.content_hash
    ):
        # The GET is guaranteed to fail for this row; say so now instead of
        # letting the pane frame an error body.
        raise HTTPException(status_code=502, detail="document has no fetchable FDA source")
    etag = f'"{doc.content_hash}"'
    return Response(
        status_code=200,
        media_type="application/pdf",
        headers=_psg_pdf_headers(appl_no=doc.appl_no, doc_id=doc.id, etag=etag),
    )


@protected.get("/psg/documents/{doc_id}/pdf")
def psg_document_pdf(
    request: Request,
    doc_id: int,
    user: User = Depends(require_user),
) -> Response:
    """Stream one PSG PDF inline: local cache first, else fetch from fda.gov.

    The remote branch reuses the crawler's hardened ``download_pdf`` (polite
    pause, streamed byte cap, %PDF validation, write-through cache in ingest's
    own naming scheme) under a tighter per-request timeout budget. Error
    details never include the upstream URL.
    """
    doc = fetch_psg_pdf_source(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="psg document not found")

    etag = f'"{doc.content_hash}"'
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=3600"},
        )

    local = _psg_local_pdf(doc.pdf_path, doc.appl_no, doc.content_hash)
    if local is not None:
        return Response(
            content=local,
            media_type="application/pdf",
            headers=_psg_pdf_headers(appl_no=doc.appl_no, doc_id=doc.id, etag=etag),
        )

    if not doc.source_url.startswith(_PSG_FDA_URL_PREFIX):
        raise HTTPException(status_code=502, detail="document has no fetchable FDA source")
    # Rate-gate the remote branch only: local-disk hits stay unmetered, and the
    # psgpdf: namespace keeps library browsing off the /query LLM budget.
    if not query_limiter.allow(f"psgpdf:user:{user.id}", get_settings().rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    try:
        with _psg_proxy_client() as fetch_client:
            _, data, digest = download_pdf(doc.source_url, client=fetch_client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="fda.gov timed out serving the PDF") from exc
    except httpx.HTTPStatusError as exc:
        # Status code only -- str(exc) embeds the full URL.
        raise HTTPException(
            status_code=502,
            detail=f"fda.gov returned {exc.response.status_code} for the PDF",
        ) from exc
    except _RetryableHTTP as exc:
        # Retried-out 5xx/429: tenacity reraises the crawler's internal marker,
        # not HTTPStatusError, so it needs its own arm or it escapes as a 500.
        raise HTTPException(
            status_code=502, detail="fda.gov kept failing to serve the PDF"
        ) from exc
    except httpx.HTTPError as exc:
        # The catch-all network arm. httpx.HTTPError, not TransportError:
        # TooManyRedirects (follow_redirects=True + a redirect cycle) and
        # DecodingError (corrupt Content-Encoding) subclass RequestError
        # directly and would otherwise escape as unmapped 500s.
        raise HTTPException(status_code=502, detail="could not reach fda.gov for the PDF") from exc
    except PdfTooLargeError as exc:
        raise HTTPException(status_code=502, detail="upstream PDF exceeds the size cap") from exc
    except PdfInvalidError as exc:
        raise HTTPException(status_code=502, detail="upstream did not return a PDF") from exc

    if digest != doc.content_hash:
        # FDA revised the PDF since the last watch run. Serve the current
        # official document; the daily watch cron owns reconciling the row.
        log.warning("psg_pdf_hash_drift", doc_id=doc.id, stored=doc.content_hash, fetched=digest)
        etag = f'"{digest}"'
    return Response(
        content=data,
        media_type="application/pdf",
        headers=_psg_pdf_headers(appl_no=doc.appl_no, doc_id=doc.id, etag=etag),
    )


class PsgContentBlock(BaseModel):
    """One block of a reference PSG, in the studio's own block vocabulary."""

    id: str
    type: Literal["title", "meta", "h2", "p"]
    text: str
    page: int


class PsgDocumentContentResponse(BaseModel):
    """One PSG rendered as a document the studio can open like a working file.

    ``file_name`` carries the .docx name the rail shows and the download
    produces, so the client never builds a second one that could disagree.
    ``truncated`` is surfaced rather than swallowed: a short body with no
    explanation would read as a short guidance.
    """

    id: int
    appl_no: str | None
    file_name: str
    active_ingredient: str
    dosage_form: str | None
    route: str | None
    psg_type: str
    recommended_date: str | None
    source_url: str
    page_count: int
    truncated: bool
    blocks: list[PsgContentBlock]


def _psg_body(doc_id: int) -> tuple[PsgDocumentDetail, PsgDocumentBody]:
    """Loads one PSG's row and rebuilt body, or raises the mapped HTTP error.

    Shared by the content and docx routes so the two can never disagree about
    which version they render or when a document has no text.
    """
    detail = fetch_psg_document_detail(doc_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="psg document not found")
    if detail.current_version_id is None:
        raise HTTPException(status_code=404, detail="psg document has no stored version")
    rows = document_chunks(detail.id, detail.current_version_id)
    body = build_body(
        detail.id,
        [PsgChunkText(ordinal=r[0], page=r[1], text=r[2]) for r in rows],
    )
    if not body.blocks:
        # The row exists but its text never landed (an ingest that stored the
        # version and died before chunking). 409, not 404: the document is
        # real and the client should offer the PDF, not report a bad id.
        raise HTTPException(status_code=409, detail="psg text is not available for this document")
    return detail, body


@protected.get("/psg/documents/{doc_id}/content", response_model=PsgDocumentContentResponse)
def psg_document_content(doc_id: int) -> dict[str, Any]:
    """One PSG as ordered blocks, rebuilt from the text ingest already stored.

    No PDF fetch and no parsing happen here: the chunks of the document's
    current version are read and reassembled, which is why this is cheap
    enough to serve on every open (a PSG averages three chunks).
    """
    detail, body = _psg_body(doc_id)
    return {
        "id": detail.id,
        "appl_no": detail.appl_no,
        "file_name": document_file_name(
            appl_no=detail.appl_no, active_ingredient=detail.active_ingredient
        ),
        "active_ingredient": detail.active_ingredient,
        "dosage_form": detail.dosage_form,
        "route": detail.route,
        "psg_type": detail.psg_type,
        "recommended_date": detail.recommended_date,
        "source_url": detail.source_url,
        "page_count": max((b.page for b in body.blocks), default=0),
        "truncated": body.truncated,
        "blocks": [
            {"id": b.id, "type": b.type, "text": b.text, "page": b.page} for b in body.blocks
        ],
    }


class PsgRequirementOut(BaseModel):
    """One extracted requirement, with the FDA words it was taken from."""

    key: str
    label: str
    value: str
    page: int | None
    quote: str | None


class PsgRequirementsResponse(BaseModel):
    """What one PSG requires of an ANDA, as ingest extracted it.

    ``extracted`` is False when no extraction row exists for this version
    (the ``--no-extract`` ingest path). The client must say so rather than
    render an empty list as "this guidance requires nothing" -- the two are
    opposite claims.
    """

    id: int
    extracted: bool
    requirements: list[PsgRequirementOut]


@protected.get(
    "/psg/documents/{doc_id}/requirements",
    response_model=PsgRequirementsResponse,
)
def psg_document_requirements(doc_id: int) -> dict[str, Any]:
    """The BE requirements ingest extracted from this PSG's current version.

    These are not compliance findings about the document: a PSG has no
    defects to report. They are what the guidance asks an applicant to do,
    each carrying the page and the verbatim quote it came from, so the studio
    can anchor them in the rendered text instead of restating them loose.
    """
    detail = fetch_psg_document_detail(doc_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="psg document not found")
    if detail.current_version_id is None:
        raise HTTPException(status_code=404, detail="psg document has no stored version")
    rows = fetch_psg_requirements(detail.id, detail.current_version_id)
    return {
        "id": detail.id,
        "extracted": bool(rows),
        "requirements": [
            {
                "key": r.key,
                "label": r.label,
                "value": r.value,
                "page": r.page,
                "quote": r.quote,
            }
            for r in rows
        ],
    }


@protected.get("/psg/documents/{doc_id}/docx")
def psg_document_docx(doc_id: int) -> Response:
    """The same PSG as a Word download, generated per request and never stored.

    Same source and same blocks as the content route, so what an analyst reads
    in the studio and what lands in their working folder cannot drift apart.
    """
    detail, body = _psg_body(doc_id)
    data = write_psg_docx(
        body.blocks,
        PsgDocxMeta(
            active_ingredient=detail.active_ingredient,
            dosage_form=detail.dosage_form,
            route=detail.route,
            appl_no=detail.appl_no,
            psg_type=detail.psg_type,
            recommended_date=detail.recommended_date,
            source_url=detail.source_url,
        ),
        truncated=body.truncated,
    )
    stem = safe_file_stem(
        document_file_name(
            appl_no=detail.appl_no, active_ingredient=detail.active_ingredient
        ).removesuffix(".docx")
    )
    return Response(
        content=data,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{stem}.docx"'},
    )


# ---------- /deficiency (DefPredict integration; DECISIONS.md 2026-07-30) ----------
# Upload -> analyze runs execute as background tasks INSIDE the API process --
# a deliberate, documented exception to "the Fly image never parses a PDF".
# The uploaded PDF lives only in a temp file for the duration of the run; the
# database keeps its sha256, never its bytes.

_DEFICIENCY_MAX_PDF_BYTES = 50 * 1024 * 1024
_DEFICIENCY_READ_CHUNK = 1024 * 1024
# Module-level like _ASK_LIMITER: bounds concurrent analyses (each one fans out
# LLM specialist calls); excess jobs queue on the limiter, bounded in turn by
# the per-user rate limit at submit time.
_DEFICIENCY_LIMITER = anyio.CapacityLimiter(max(1, get_settings().deficiency_analyze_concurrency))

_DEFICIENCY_RUN_NOT_FOUND = "deficiency run not found"


class DeficiencyAnalyzeResponse(BaseModel):
    run_id: int
    status: str


class DeficiencyRunSummary(BaseModel):
    """One run list row. ``status``/``error`` are the READ-TIME interpretation
    (``effective_status``): a row stranded pending/running by a process restart
    reads as failed after the stale cutoff instead of spinning forever."""

    id: int
    filename: str
    status: str
    created_at: datetime
    completed_at: datetime | None
    page_count: int | None
    fault_count: int | None
    error: str | None


class DeficiencyRunListResponse(BaseModel):
    count: int
    total: int
    limit: int
    offset: int
    runs: list[DeficiencyRunSummary]


class DeficiencyRunDetailResponse(DeficiencyRunSummary):
    """``report`` is a deliberately passthrough dict: the response must be
    VERBATIM what the audited run stored (same discipline as the white-paper
    detail route). Null unless the run is complete."""

    report: dict[str, Any] | None


def _deficiency_summary_fields(run: Any) -> dict[str, Any]:
    status, error = deficiency_run_store.effective_status(run)
    return {
        "id": run.id,
        "filename": run.filename,
        "status": status,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "page_count": run.page_count,
        "fault_count": run.fault_count,
        "error": error,
    }


async def _deficiency_background(run_id: int, tmp_path: str) -> None:
    """Background wrapper: limiter slot -> deadline -> worker thread -> cleanup.

    ``fail_after`` cancels the AWAIT, not the worker thread
    (``abandon_on_cancel=True``): the orphaned thread keeps running to its end,
    but its ``complete_run`` is a compare-and-set that loses against the
    ``fail_run`` written here, so a timed-out run can never flip back to
    complete. The temp file outlives the abandoned thread deliberately --
    unlinking it here on timeout would hand the still-parsing thread a
    vanished file; the unlink-on-exit branch below only runs when the thread
    finished or never started.
    """
    s = get_settings()
    abandoned = False
    try:
        async with _DEFICIENCY_LIMITER:
            try:
                with anyio.fail_after(s.deficiency_analyze_timeout_s):
                    await anyio.to_thread.run_sync(
                        partial(run_deficiency_analysis, run_id, tmp_path),
                        abandon_on_cancel=True,
                    )
            except TimeoutError:
                abandoned = True
                log.error(
                    "deficiency_run_timeout",
                    run_id=run_id,
                    timeout_s=s.deficiency_analyze_timeout_s,
                )
                deficiency_run_store.fail_run(
                    run_id,
                    error=(f"analysis timed out after {int(s.deficiency_analyze_timeout_s)}s"),
                )
    except Exception as exc:
        # run_deficiency_analysis records its own failures; this catches the
        # wrapper machinery itself (limiter/threadpool) so the row never
        # strands in pending on an infrastructure fault.
        log.error("deficiency_background_failed", run_id=run_id, error_type=type(exc).__name__)
        capture_exception(exc)
        deficiency_run_store.fail_run(run_id, error=f"{type(exc).__name__}: {exc}")
    finally:
        if not abandoned:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning(
                    "deficiency_tmpfile_cleanup_failed",
                    run_id=run_id,
                    error_type=type(exc).__name__,
                )


@protected.post("/deficiency/analyze", response_model=DeficiencyAnalyzeResponse, status_code=202)
async def deficiency_analyze(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
) -> DeficiencyAnalyzeResponse:
    """Accept a submission PDF, create the run row, schedule the analysis.

    The whole body is size-capped and magic-checked while streaming to a temp
    file -- never buffered in memory, never trusted from Content-Length. 202 +
    run_id; the UI polls GET /deficiency/runs/{id}.
    """
    _enforce_query_rate_limit(user)
    filename = os.path.basename(file.filename or "upload.pdf")[:200] or "upload.pdf"
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="deficiency-")
    scheduled = False
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_DEFICIENCY_READ_CHUNK):
                if size == 0 and not chunk.startswith(b"%PDF"):
                    raise HTTPException(status_code=400, detail="not a PDF (missing %PDF header)")
                size += len(chunk)
                if size > _DEFICIENCY_MAX_PDF_BYTES:
                    raise HTTPException(status_code=400, detail="PDF exceeds the 50 MB limit")
                hasher.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="empty upload")
        run = deficiency_run_store.create_run(
            user_id=_user_pk(user), filename=filename, sha256=hasher.hexdigest()
        )
        if run.id is None:  # pragma: no cover - flush always assigns the PK
            raise RuntimeError("deficiency run insert returned no id")
        background.add_task(_deficiency_background, run.id, tmp_path)
        scheduled = True
        return DeficiencyAnalyzeResponse(run_id=run.id, status="pending")
    finally:
        if not scheduled:
            # Refused/failed before scheduling: the temp file has no owner left.
            with suppress(OSError):
                os.unlink(tmp_path)


@protected.get("/deficiency/runs", response_model=DeficiencyRunListResponse)
def deficiency_runs_list(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> DeficiencyRunListResponse:
    """Org-shared runs, newest first (same product decision as white-paper
    runs: any authenticated analyst sees every run)."""
    rows, total = deficiency_run_store.list_runs(limit=limit, offset=offset)
    runs = [DeficiencyRunSummary(**_deficiency_summary_fields(r)) for r in rows]
    return DeficiencyRunListResponse(
        count=len(runs), total=total, limit=limit, offset=offset, runs=runs
    )


@protected.get("/deficiency/runs/{run_id}", response_model=DeficiencyRunDetailResponse)
def deficiency_run_detail(run_id: int) -> DeficiencyRunDetailResponse:
    """One run + its verbatim stored fault report (null until complete).

    Uploads only. Studio checks share this table but are private to their
    creator, so serving one here would let any analyst read a colleague's
    draft by asking the org-shared route for the same id.
    """
    run = deficiency_run_store.get_run(run_id)
    if run is None or run.source is not None:
        raise HTTPException(status_code=404, detail=_DEFICIENCY_RUN_NOT_FOUND)
    fields = _deficiency_summary_fields(run)
    report = run.report_json if fields["status"] == "complete" else None
    return DeficiencyRunDetailResponse(**fields, report=report)


# ---------- /studio/check (the Compliance Studio check) ----------
# Same engine, same table and same background machinery as /deficiency, over a
# document assembled from editor blocks instead of a parsed PDF. Two things are
# deliberately different: the document arrives as JSON in the request rather
# than as an upload (so there is no temp file to own), and a run is PRIVATE to
# the analyst who made it, because its report quotes an unfinished draft.

_STUDIO_SOURCE = "studio"
_STUDIO_RUN_NOT_FOUND = "studio check not found"
# Bounds on a JSON body that becomes an in-memory document and then an LLM
# fan-out. Both are refusals at the boundary, not truncations: silently
# checking part of a document would report a clean result for text nobody read.
_STUDIO_MAX_BLOCKS = 5000
_STUDIO_MAX_CHARS = 1_000_000


class StudioRowIn(BaseModel):
    """One row of a Studio table block."""

    cells: list[str] = Field(default_factory=list)
    head: bool = False


class StudioBlockIn(BaseModel):
    """One editable block of a Studio document, as the canvas holds it."""

    id: str
    type: str
    text: str
    # Carried through because the deterministic oracles read table cells, never
    # block prose: a table sent as text alone is invisible to them.
    rows: list[StudioRowIn] | None = None


class StudioCheckRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    blocks: list[StudioBlockIn]


async def _studio_check_background(run_id: int, name: str, blocks: list[dict[str, Any]]) -> None:
    """Background wrapper: limiter slot -> deadline -> worker thread.

    Shares _DEFICIENCY_LIMITER with the upload path on purpose -- both fan out
    LLM specialist calls to the same serving endpoint, so one budget is the
    honest one. The timeout semantics are identical to _deficiency_background
    (the await is cancelled, the thread is abandoned, and its complete_run
    loses the compare-and-set), minus the temp-file lifecycle: this document
    lives in the request, so there is nothing to unlink.
    """
    s = get_settings()
    try:
        async with _DEFICIENCY_LIMITER:
            try:
                with anyio.fail_after(s.deficiency_analyze_timeout_s):
                    await anyio.to_thread.run_sync(
                        partial(run_studio_check, run_id, name, blocks),
                        abandon_on_cancel=True,
                    )
            except TimeoutError:
                log.error(
                    "studio_check_timeout",
                    run_id=run_id,
                    timeout_s=s.deficiency_analyze_timeout_s,
                )
                deficiency_run_store.fail_run(
                    run_id,
                    error=(f"analysis timed out after {int(s.deficiency_analyze_timeout_s)}s"),
                )
    except Exception as exc:
        # run_studio_check records its own failures; this catches the wrapper
        # machinery so the row never strands in pending on an infra fault.
        log.error("studio_check_background_failed", run_id=run_id, error_type=type(exc).__name__)
        capture_exception(exc)
        deficiency_run_store.fail_run(run_id, error=f"{type(exc).__name__}: {exc}")


def _studio_run_for(run_id: int, user: User) -> Any:
    """The caller's own Studio run, or 404. Same status for a missing id, an
    upload run and another analyst's check: the response must not reveal that
    an id it may not read exists."""
    run = deficiency_run_store.get_run(run_id)
    if run is None or run.source != _STUDIO_SOURCE or run.created_by_user_id != _user_pk(user):
        raise HTTPException(status_code=404, detail=_STUDIO_RUN_NOT_FOUND)
    return run


@protected.post("/studio/check", response_model=DeficiencyAnalyzeResponse, status_code=202)
def studio_check(
    body: StudioCheckRequest,
    background: BackgroundTasks,
    user: User = Depends(require_user),
) -> DeficiencyAnalyzeResponse:
    """Check one Studio document for candidate deficiencies.

    202 + run_id; the UI polls GET /studio/check/{id}. The document is hashed
    from the text that will actually be checked, so two checks of an unedited
    draft are comparable and an edit is visibly a different document.
    """
    _enforce_query_rate_limit(user)
    if len(body.blocks) > _STUDIO_MAX_BLOCKS:
        raise HTTPException(status_code=400, detail=f"document exceeds {_STUDIO_MAX_BLOCKS} blocks")

    blocks = [b.model_dump(exclude_none=True) for b in body.blocks]
    text = "\n".join(b["text"] for b in blocks)
    if len(text) > _STUDIO_MAX_CHARS:
        raise HTTPException(
            status_code=400, detail=f"document is too large ({_STUDIO_MAX_CHARS} character limit)"
        )
    if not text.strip():
        raise HTTPException(status_code=400, detail="document carries no text to check")

    run = deficiency_run_store.create_run(
        user_id=_user_pk(user),
        filename=body.name[:200],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source=_STUDIO_SOURCE,
    )
    if run.id is None:  # pragma: no cover - flush always assigns the PK
        raise RuntimeError("studio check insert returned no id")
    background.add_task(_studio_check_background, run.id, body.name, blocks)
    return DeficiencyAnalyzeResponse(run_id=run.id, status="pending")


@protected.get("/studio/check/{run_id}", response_model=DeficiencyRunDetailResponse)
def studio_check_detail(
    run_id: int, user: User = Depends(require_user)
) -> DeficiencyRunDetailResponse:
    """One of the caller's own checks + its verbatim stored report."""
    run = _studio_run_for(run_id, user)
    fields = _deficiency_summary_fields(run)
    report = run.report_json if fields["status"] == "complete" else None
    return DeficiencyRunDetailResponse(**fields, report=report)


app.include_router(protected)
