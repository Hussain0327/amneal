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

from sqlmodel import select

from regwatch.ingest import pipeline as pipeline_mod
from regwatch.ingest.pdf_parser import ParsedPdf
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.store.vector_store import collection_size

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

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, response_format=None):
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


def _patch_pipeline(monkeypatch) -> None:
    """Bypass network + PDF rendering with deterministic stubs."""
    fake_pdf = b"%PDF-1.4 stub"
    fake_hash = "deadbeef" * 8

    def fake_download(url: str, *, client=None):
        # Returns (path, bytes, sha256_hex). We don't actually write a file.
        return Path("/tmp/regwatch-test.pdf"), fake_pdf, fake_hash

    def fake_parse(pdf_bytes: bytes):
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


def _row_count(model) -> int:
    from sqlalchemy import func

    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_pipeline_idempotent_run_twice(monkeypatch) -> None:
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


def test_pipeline_be_fields_all_have_citations(monkeypatch) -> None:
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
