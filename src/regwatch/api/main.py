"""FastAPI surface.

This is the clean boundary the IT/AI team will wrap or replace. Every
response is reproducible in Postman from a `.env` and a running instance.

Endpoints (per spec §10.16):
    POST  /query        — grounded Q&A
    POST  /assemble     — build a cited dossier for a target product
    GET   /watch/latest — recent alerts
    GET   /products     — list watchlist
    POST  /products     — add manual product
    GET   /health       — liveness
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from config.settings import get_settings
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from regwatch.assemble.dossier import build_dossier
from regwatch.common.logging import configure_logging
from regwatch.generate.grounded_qa import ask
from regwatch.store.db import init_db
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import ALLOWED_SOURCES, add_manual_product, list_watchlist

configure_logging()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
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


# ---------- /health ----------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- /query ----------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=2)
    filters: dict[str, Any] | None = None
    k: int | None = None


class QueryCitation(BaseModel):
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    source_url: str
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[QueryCitation]
    refused: bool
    model_name: str
    audit_id: int


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    result = ask(req.question, filters=req.filters, k=req.k)
    return QueryResponse(
        answer=result.answer,
        citations=[QueryCitation(**c.__dict__) for c in result.citations],
        refused=result.refused,
        model_name=result.model_name,
        audit_id=result.audit_id,
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
@app.get("/watch/latest")
def watch_latest(since: str | None = None) -> dict[str, Any]:
    records = latest_digest_records(limit=200)
    if since:
        records = [r for r in records if (r.get("captured_at") or "") >= since]
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
