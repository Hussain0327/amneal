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


def test_failed_extraction_is_backfilled_on_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient extractor outage on first ingest must not permanently drop citations.

    INV-1: missing extraction == missing citations. The first run swallows the
    extractor failure (still returns successfully, zero be_requirement rows). The
    second run — same content, so "unchanged" — must detect the missing row and
    backfill exactly one BeRequirement for the latest version rather than skipping.
    """
    _patch_pipeline(monkeypatch)
    init_db()

    def boom(_pages: list[str]) -> Any:
        raise RuntimeError("extractor outage")

    # First run: extractor is down. Ingest still succeeds, but no BE row is written.
    monkeypatch.setattr(pipeline_mod, "extract_be", boom)
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 1
    assert _row_count(BeRequirement) == 0

    # Second run: same content (so "unchanged") with a healthy extractor restored.
    # The missing BE row for the latest version must be backfilled exactly once.
    monkeypatch.undo()
    _patch_pipeline(monkeypatch)
    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"
    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 1
    assert _row_count(BeRequirement) == 1

    with session_scope() as s:
        be_version_id = s.scalars(select(BeRequirement.version_id)).one()
        latest_version_id = s.scalars(
            select(PsgVersion.id).order_by(col(PsgVersion.id).desc())
        ).first()
    assert be_version_id == latest_version_id

    # A third run with a healthy extractor must NOT create a duplicate BE row.
    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"
    assert _row_count(BeRequirement) == 1


def test_failed_parse_leaves_doc_row_on_prior_ingested_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision whose parse fails must NOT update the doc row's content fields.

    Otherwise sources/psg.py serves the NEW revision's psg_type/recommended_date/
    content_hash next to the OLD version's diff summary and chunks until the PDF
    becomes parseable (the doc row would describe content that was never ingested).
    """
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES, "boom": False}
    _patch_pipeline_state(monkeypatch, state)

    def flaky_parse(pdf_bytes: bytes) -> ParsedPdf:
        if state["boom"]:
            raise RuntimeError("simulated parser guard failure")
        pages = list(state["pages"])
        return ParsedPdf(text="\n\f\n".join(pages), pages=pages, engine="stub")

    monkeypatch.setattr(pipeline_mod, "parse_pdf", flaky_parse)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"

    # FDA revises draft -> final with a new PDF that trips the parser guard.
    revised = _listing()
    revised.psg_type = "final"
    revised.recommended_date = "2026-06-01"
    state["hash"] = "new-hash"
    state["boom"] = True
    assert pipeline_mod.ingest_listing(revised) == "error"

    with session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        # The doc row must still describe the version that was actually ingested.
        assert doc.content_hash == "old-hash"
        assert doc.psg_type == "draft"
        assert doc.recommended_date == "2026-05-21"
    assert _row_count(PsgVersion) == 1

    # Once the PDF parses, the content fields land together with the version row.
    state["boom"] = False
    assert pipeline_mod.ingest_listing(revised) == "revised"
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        assert doc.content_hash == "new-hash"
        assert doc.psg_type == "final"
        assert doc.recommended_date == "2026-06-01"
    assert _row_count(PsgVersion) == 2


def test_first_success_after_failed_first_attempt_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'added' vs 'revised' is judged by version history, not doc-row novelty.

    Day 1: parse fails after the doc row is created -> outcome "error", row
    persists with no version. Day 2: parse succeeds -> this is the first-ever
    ingested version and must be reported "added" (not "revised").
    """
    state: dict[str, Any] = {"hash": "h1", "pages": PAGES, "boom": True}
    _patch_pipeline_state(monkeypatch, state)

    def flaky_parse(pdf_bytes: bytes) -> ParsedPdf:
        if state["boom"]:
            raise RuntimeError("simulated parser guard failure")
        pages = list(state["pages"])
        return ParsedPdf(text="\n\f\n".join(pages), pages=pages, engine="stub")

    monkeypatch.setattr(pipeline_mod, "parse_pdf", flaky_parse)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "error"
    assert _row_count(PsgDocument) == 1
    assert _row_count(PsgVersion) == 0

    state["boom"] = False
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    assert _row_count(PsgVersion) == 1


def test_concurrent_duplicate_revision_is_not_inserted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision committed by an overlapping run during our parse+summarize
    window must not be inserted a second time (a duplicate never-alerted
    version row would re-alert the same FDA change the next day, INV-4)."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    init_db()

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    state["hash"] = "new-hash"

    def racing_summarize(
        previous_text: str | None, current_text: str, *, current_page_count: int
    ) -> str:
        # Simulate the overlapping run (e.g. watch-daily cron vs a manual
        # ingest-all) landing the identical revision between the early hash
        # check and the version insert.
        with session_scope() as s:
            doc = s.scalars(select(PsgDocument)).one()
            assert doc.id is not None
            s.add(
                PsgVersion(
                    psg_document_id=doc.id,
                    content_hash="new-hash",
                    diff_summary="committed by the overlapping run",
                )
            )
        return "Initial version ingested."

    monkeypatch.setattr(pipeline_mod, "summarize_change", racing_summarize)

    outcome = pipeline_mod.ingest_listing(_listing())

    # Exactly two versions total: v1 plus the overlapping run's v2 -- no third
    # duplicate row for the same content, and the sighting is not re-reported
    # as a change.
    assert _row_count(PsgVersion) == 2
    assert outcome == "unchanged"


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


def test_be_extraction_skip_log_carries_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """_extract_and_save_be swallows LLM and DB-write failures under one log, and
    the unchanged-content backfill re-pays the LLM cost every run until the row
    exists -- so error_type (the triage key the sibling ingest_failed log already
    carries) must distinguish "bad LLM JSON" from "broken DB write" in logs."""

    class _LogRecorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def warning(self, event: str, **kwargs: Any) -> None:
            self.events.append((event, kwargs))

    def boom(pages: list[str]) -> object:
        raise ValueError("bad extraction json")

    recorder = _LogRecorder()
    monkeypatch.setattr(pipeline_mod, "extract_be", boom)
    monkeypatch.setattr(pipeline_mod, "log", recorder)

    pipeline_mod._extract_and_save_be(1, 1, PAGES, "ANDA076170")

    assert recorder.events, "no warning logged for the failed extraction"
    event, kwargs = recorder.events[0]
    assert event == "be_extraction_skipped"
    assert kwargs["error_type"] == "ValueError"
    assert kwargs["appl_no"] == "ANDA076170"
