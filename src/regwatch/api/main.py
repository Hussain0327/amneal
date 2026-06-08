"""FastAPI surface.

This is the clean boundary the IT/AI team will wrap or replace. Every
response is reproducible in Postman from a `.env` and a running instance.

Endpoints (per spec §10.16):
    POST  /query        — grounded Q&A
    POST  /sources/search — structured FDA source lookup
    POST  /assemble     — build a cited dossier for a target product
    GET   /watch/latest — recent alerts
    GET   /products     — list watchlist
    POST  /products     — add manual product
    GET   /health       — liveness
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from config.settings import get_settings
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from regwatch.assemble.dossier import build_dossier
from regwatch.common.logging import configure_logging
from regwatch.generate.grounded_qa import ask
from regwatch.sources.router import search_sources
from regwatch.sources.types import SourceKind, SourceQuery
from regwatch.store.db import init_db
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import ALLOWED_SOURCES, add_manual_product, list_watchlist

configure_logging()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if os.getenv("REGWATCH_DB_INITIALIZED") != "1":
        init_db()
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
)

# CORS — the Next.js UI (regwatch/frontend/) calls this API from the browser.
# Allowlist comes from settings; there is no auth layer yet, so the allowlist is
# the boundary.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------- /health ----------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- /query ----------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=2)
    filters: dict[str, Any] | None = None
    k: int | None = None
    session_id: str | None = None
    user_id: str | None = None


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


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    result = ask(
        req.question,
        filters=req.filters,
        k=req.k,
        session_id=req.session_id,
        user_id=req.user_id,
    )
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


@app.post("/sources/search", response_model=SourceSearchResponse)
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


@app.post("/assemble", response_model=AssembleResponse)
def assemble(req: AssembleRequest) -> AssembleResponse:
    dossier = build_dossier(
        active_ingredient=req.active_ingredient,
        dosage_form=req.dosage_form,
        rld=req.rld,
    )
    return AssembleResponse(**dossier)


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


@app.get("/watch/latest")
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


@app.get("/products")
def list_products() -> dict[str, Any]:
    items = list_watchlist()
    return {"count": len(items), "products": items}


@app.post("/products", status_code=201)
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


# ---------- /settings (read-only, no secrets) ----------
@app.get("/settings")
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
