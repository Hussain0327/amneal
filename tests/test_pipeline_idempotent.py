"""Phase 1 DoD: idempotent ingest.

Running the pipeline twice for the same listing must:
  - produce exactly one psg_document
  - produce exactly one psg_version
  - produce exactly one be_requirement
  - produce some chunks (any positive count)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import col, select

from regwatch.ingest import pipeline as pipeline_mod
from regwatch.ingest.pdf_parser import ParsedPdf
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.store.vector_store import collection_size, get_collection

PAGES = [
    "I. Introduction\nThis Product-Specific Guidance describes the agency's "
    "current recommendations for bioequivalence (BE) studies for Albuterol "
    "Sulfate Inhalation Aerosol.",
    "II. Recommendations for BE Studies\n"
    "A. Type of study: A single-dose, randomized, in-vivo BE study is recommended.\n"
    "B. Subjects: Adult healthy non-smokers.\n"
    "C. Dissolution: USP Apparatus 2 at 50 RPM in 900 mL of pH 6.8 buffer.",
]


class _StubLLM:
    name = "stub"

    def complete(
        self,
        messages: object,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> object:
        from regwatch.generate.llm import LLMResponse

        # Simulate a BE-extraction prompt (response_format=json) and a
        # change-summary prompt (no response_format).
        if response_format == "json":
            payload = {
                "fields": {
                    "study_type": {
                        "value": "single-dose in-vivo",
                        "citation": {
                            "page": 2,
                            "quote": "A single-dose, randomized, in-vivo BE study is recommended",
                        },
                    },
                    "dissolution": {
                        "value": "USP Apparatus 2 at 50 RPM",
                        "citation": {
                            "page": 2,
                            "quote": "USP Apparatus 2 at 50 RPM in 900 mL of pH 6.8 buffer",
                        },
                    },
                }
            }
            return LLMResponse(text=json.dumps(payload), model="stub")
        return LLMResponse(text="Initial version ingested.", model="stub")


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass network + PDF rendering with deterministic stubs."""
    fake_pdf = b"%PDF-1.4 stub"
    fake_hash = "deadbeef" * 8

    def fake_download(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        # Returns (path, bytes, sha256_hex). We don't actually write a file.
        return Path("/tmp/regwatch-test.pdf"), fake_pdf, fake_hash

    def fake_parse(pdf_bytes: bytes) -> ParsedPdf:
        return ParsedPdf(text="\n\f\n".join(PAGES), pages=PAGES, engine="stub")

    # Patch download in the crawler module AND in pipeline (it imports the symbol).
    monkeypatch.setattr("regwatch.ingest.psg_crawler.download_pdf", fake_download)
    monkeypatch.setattr(pipeline_mod, "download_pdf", fake_download)
    monkeypatch.setattr(pipeline_mod, "parse_pdf", fake_parse)
    # Patch LLM in extractor + change_detector.
    monkeypatch.setattr("regwatch.process.extractor.get_llm_provider", lambda *a, **k: _StubLLM())
    monkeypatch.setattr(
        "regwatch.process.change_detector.get_llm_provider", lambda *a, **k: _StubLLM()
    )


def _patch_pipeline_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """Patch ingest with mutable pages/hash so tests can simulate a revision."""
    fake_pdf = b"%PDF-1.4 stub"

    def fake_download(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        return Path("/tmp/regwatch-test.pdf"), fake_pdf, str(state["hash"])

    def fake_parse(pdf_bytes: bytes) -> ParsedPdf:
        pages = list(state["pages"])
        return ParsedPdf(text="\n\f\n".join(pages), pages=pages, engine="stub")

    monkeypatch.setattr("regwatch.ingest.psg_crawler.download_pdf", fake_download)
    monkeypatch.setattr(pipeline_mod, "download_pdf", fake_download)
    monkeypatch.setattr(pipeline_mod, "parse_pdf", fake_parse)
    monkeypatch.setattr("regwatch.process.extractor.get_llm_provider", lambda *a, **k: _StubLLM())
    monkeypatch.setattr(
        "regwatch.process.change_detector.get_llm_provider", lambda *a, **k: _StubLLM()
    )


def _listing() -> PsgListing:
    return PsgListing(
        appl_no="020503",
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        stripped_name="albuterol",
        psg_type="draft",
        route="Inhalation",
        dosage_form="Aerosol, Metered",
        rld_or_rs_numbers=["020503", "020983", "021457"],
        recommended_date="2026-05-21",
        pdf_url="https://example.invalid/PSG_020503.pdf",
        source_url="https://example.invalid/index.cfm",
    )


def _listing_with_reversed_rld_numbers() -> PsgListing:
    li = _listing()
    li.rld_or_rs_numbers = list(reversed(li.rld_or_rs_numbers))
    return li


def _row_count(model: Any) -> int:
    from sqlalchemy import func

    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_save_be_requirement_coerces_list_scalars() -> None:
    init_db()
    with session_scope() as s:
        doc = PsgDocument(
            appl_no="020503",
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            rld_or_rs_number="020503",
            psg_type="draft",
            source_url="https://example.invalid/PSG_020503.pdf",
            content_hash="hash",
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        version = PsgVersion(psg_document_id=doc.id, content_hash="hash")
        s.add(version)
        s.flush()
        assert version.id is not None
        doc_id = doc.id
        version_id = version.id

    pipeline_mod._save_be_requirement(
        doc_id,
        version_id,
        fields={"strengths": ["10 mg", "20 mg"], "study_type": "fasting"},
        citations={},
    )

    with session_scope() as s:
        row = s.scalars(select(BeRequirement)).one()
        assert row.strengths == "10 mg, 20 mg"
        assert dict(row.fields_json)["strengths"] == ["10 mg", "20 mg"]


def test_pipeline_idempotent_run_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline(monkeypatch)
    init_db()

    outcome1 = pipeline_mod.ingest_listing(_listing())
    assert outcome1 == "added"

    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 1
    assert _row_count(BeRequirement) == 1
    assert collection_size() > 0

    outcome2 = pipeline_mod.ingest_listing(_listing())
    assert outcome2 == "unchanged"
    # No new rows.
    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 1
    assert _row_count(BeRequirement) == 1


def test_pipeline_rld_join_key_is_order_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline(monkeypatch)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    assert pipeline_mod.ingest_listing(_listing_with_reversed_rld_numbers()) == "unchanged"
    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 1
    assert _row_count(BeRequirement) == 1


def test_pipeline_be_fields_all_have_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-1 placeholder at the field level — every populated field has a verified citation."""
    _patch_pipeline(monkeypatch)
    init_db()
    pipeline_mod.ingest_listing(_listing())
    with session_scope() as s:
        be_rows = list(s.scalars(select(BeRequirement)))
        assert len(be_rows) == 1
        be = be_rows[0]
        fields = dict(be.fields_json)
        citations = dict(be.citations_json)
    # Every populated value must have a matching citation.
    for field, value in fields.items():
        if value:
            assert field in citations, f"populated field {field} missing citation"
            cite = citations[field]
            assert "page" in cite and "quote" in cite
            assert isinstance(cite["page"], int) and cite["page"] >= 1


def test_revised_ingest_removes_stale_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    old_pages = [
        PAGES[0],
        PAGES[1] + "\nObsolete marker only present in version one.",
    ]
    new_pages = [
        PAGES[0],
        PAGES[1] + "\nCurrent marker only present in version two.",
    ]
    state: dict[str, Any] = {"hash": "old-hash", "pages": old_pages}
    _patch_pipeline_state(monkeypatch, state)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    state["hash"] = "new-hash"
    state["pages"] = new_pages
    assert pipeline_mod.ingest_listing(_listing()) == "revised"

    with session_scope() as s:
        version_ids = list(s.scalars(select(PsgVersion.id).order_by(col(PsgVersion.id))))
    assert len(version_ids) == 2
    latest_version_id = version_ids[-1]
    assert isinstance(latest_version_id, int)

    indexed = get_collection().get(include=["documents", "metadatas"])
    metadatas = indexed.get("metadatas") or []
    documents = indexed.get("documents") or []

    assert metadatas
    indexed_version_ids: set[int] = set()
    for meta in metadatas:
        raw_version: object = (meta or {}).get("version_id") if isinstance(meta, dict) else None
        if isinstance(raw_version, str | int | float):
            indexed_version_ids.add(int(raw_version))
    assert indexed_version_ids == {latest_version_id}
    assert any("Current marker only present in version two" in doc for doc in documents)
    assert all("Obsolete marker only present in version one" not in doc for doc in documents)
