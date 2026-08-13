"""Faithful structured stub for White-Paper tests (offline, deterministic).

Stands in for the live FDA sources so the populator's cell logic is exercised
end to end with no network and no API key. Not a test module — imported by the
white-paper tests and the deterministic eval gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from regwatch.generate.grounded_qa import Citation, QAResult
from regwatch.sources import orange_book
from regwatch.sources.approved_label import ApprovedLabel, label_from_pages
from regwatch.sources.orange_book import OrangeBookRows
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.whitepaper import populator

APPL_NO = "020503"
RLD_NAME = "albuterol sulfate"

_LABEL_PAGES = [
    """1 INDICATIONS AND USAGE
Albuterol sulfate inhalation aerosol is indicated for bronchospasm.
2 DOSAGE AND ADMINISTRATION
Two inhalations every 4 to 6 hours.
5 WARNINGS AND PRECAUTIONS
Paradoxical bronchospasm may occur.
6 ADVERSE REACTIONS
Most common adverse reactions include tremor.""",
    """8 USE IN SPECIFIC POPULATIONS
8.1 PREGNANCY
To enroll, contact the pregnancy exposure registry at 1-800-555-0100.""",
]


def _product_rows() -> list[dict[str, str]]:
    return [
        {
            "appl_type": "N",
            "appl_no": APPL_NO,
            "product_no": "001",
            "ingredient": "ALBUTEROL SULFATE",
            "trade_name": "PROVENTIL HFA",
            "dosage_form_route": "AEROSOL, METERED;INHALATION",
            "strength": "0.09MG/INH",
            "rld": "Yes",
            "rs": "Yes",
            "te_code": "",
            "approval_date": "Apr 19, 1996",
            "applicant": "MERCK",
            "applicant_full_name": "MERCK SHARP DOHME CORP",
        }
    ]


def _patent_rows() -> list[dict[str, str]]:
    return [
        {
            "appl_type": "N",
            "appl_no": APPL_NO,
            "product_no": "001",
            "patent_no": "RE37410",
            "patent_expire_date": "Aug 22, 2017",
            "drug_substance_flag": "Y",
            "drug_product_flag": "",
            "patent_use_code": "U-123",
            "delist_flag": "",
            "submission_date": "",
        }
    ]


def _exclusivity_rows() -> list[dict[str, str]]:
    return [
        {
            "appl_type": "N",
            "appl_no": APPL_NO,
            "product_no": "001",
            "exclusivity_code": "NCE",
            "exclusivity_date": "Apr 19, 2001",
        }
    ]


def _drugsfda_record() -> SourceRecord:
    return SourceRecord(
        source=SourceKind.DRUGSFDA,
        title="Drugs@FDA: NDA020503",
        source_url="https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?ApplNo=020503",
        identifiers={"application_number": "NDA020503"},
        fields={
            "sponsor_name": "MERCK SHARP DOHME CORP",
            "products": [
                {
                    "brand_name": "PROVENTIL HFA",
                    "dosage_form": "AEROSOL, METERED",
                    "route": "INHALATION",
                    "active_ingredients": [{"name": "ALBUTEROL SULFATE", "strength": "0.09MG"}],
                }
            ],
        },
        raw={},
    )


def _ndc_record(dea_schedule: str | None, *, include_epc: bool) -> SourceRecord:
    raw: dict[str, Any] = {}
    if include_epc:
        raw["pharm_class"] = ["Adrenergic beta2-Agonists [EPC]"]
    if dea_schedule:
        raw["dea_schedule"] = dea_schedule
    return SourceRecord(
        source=SourceKind.NDC,
        title="NDC: 0085-1132-01",
        source_url="https://open.fda.gov/apis/drug/ndc/",
        identifiers={"product_ndc": "0085-1132", "application_number": "NDA020503"},
        fields={
            "brand_name": "PROVENTIL HFA",
            "packaging": [{"package_ndc": "0085-1132-01", "description": "1 inhaler in 1 carton"}],
        },
        raw=raw,
    )


def _approved_label() -> ApprovedLabel:
    return label_from_pages(
        canonical_id="drugs-at-fda:application-doc:77",
        title="PROVENTIL HFA approved labeling",
        application_number="NDA020503",
        source_url="https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/020503lbl.pdf",
        source_updated_at="2026-08-01",
        fetched_at=datetime.now(UTC),
        document_id=77,
        version_id=88,
        pages=_LABEL_PAGES,
    )


def _qa_result() -> QAResult:
    return QAResult(
        answer="A fasting single-dose two-way crossover in vivo study is recommended "
        "[PSG_020503, p.4].",
        citations=[
            Citation(
                short_name="PSG_020503",
                page=4,
                chunk_id="020503-4",
                doc_id=1,
                version_id=10,
                source_url="http://example/PSG_020503.pdf",
                snippet="fasting single-dose two-way crossover in vivo",
            )
        ],
        refused=False,
        model_name="stub",
        audit_id=0,
        retrieved=[],
        status="answer",
    )


def _seed_psg_store() -> None:
    init_db()
    with session_scope() as s:
        doc = PsgDocument(
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            appl_no=APPL_NO,
            rld_or_rs_number=APPL_NO,
            psg_type="draft",
            recommended_date="2020-01-01",
            source_url="http://example/PSG_020503.pdf",
            content_hash="hash-020503",
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        version = PsgVersion(psg_document_id=doc.id, content_hash="hash-020503")
        s.add(version)
        s.flush()
        assert version.id is not None
        s.add(
            BeRequirement(
                psg_document_id=doc.id,
                version_id=version.id,
                study_type="in vivo BE study",
                dissolution="USP apparatus profile",
                fields_json={
                    "study_type": "in vivo BE study",
                    "dissolution": "USP apparatus profile",
                },
                citations_json={"study_type": {"page": 4}, "dissolution": {"page": 3}},
            )
        )


def install_fake_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_shortage: bool = False,
    shortage_status: str = "Current Shortage",
    has_rems: bool = False,
    rems_ambiguous: bool = False,
    rems_index_rows: int = 47,
    ob_raises: bool = False,
    shortage_raises: bool = False,
    ndc_raises: bool = False,
    ndc_epc: bool = False,
    dea_schedule: str | None = None,
    setid: str | None = "abc-def-123",
    seed_psg: bool = True,
) -> None:
    """Wire the populator's source dependencies to deterministic fakes."""
    if seed_psg:
        _seed_psg_store()
    else:
        init_db()

    def fake_products(application_number: str, *, client: Any = None) -> OrangeBookRows:
        if ob_raises:
            raise RuntimeError("orange book down")
        return OrangeBookRows(rows=_product_rows(), fetched_at=datetime.now(UTC))

    def fake_patents(application_number: str, *, client: Any = None) -> OrangeBookRows:
        return OrangeBookRows(rows=_patent_rows(), fetched_at=datetime.now(UTC))

    def fake_exclusivity(application_number: str, *, client: Any = None) -> OrangeBookRows:
        return OrangeBookRows(rows=_exclusivity_rows(), fetched_at=datetime.now(UTC))

    def fake_drugsfda(query: SourceQuery) -> list[SourceRecord]:
        return [_drugsfda_record()]

    def fake_ndc(query: SourceQuery) -> list[SourceRecord]:
        if ndc_raises:
            raise RuntimeError("ndc down")
        return [_ndc_record(dea_schedule, include_epc=ndc_epc)]

    def fake_shortage(query: SourceQuery) -> list[SourceRecord]:
        if shortage_raises:
            raise RuntimeError("shortages rate-limited")
        if not on_shortage:
            return []
        return [
            SourceRecord(
                source=SourceKind.SHORTAGE,
                title="Drug Shortage: PROVENTIL HFA",
                source_url="https://open.fda.gov/apis/drug/drugshortages/",
                identifiers={"application_number": "NDA020503"},
                fields={"status": shortage_status},
                raw={},
            )
        ]

    def fake_rems_search(query: SourceQuery) -> tuple[list[SourceRecord], int]:
        # Mirrors the populator wrapper: (matched records, TOTAL parsed rows).
        # rems_index_rows=0 simulates a degraded scrape (nothing parsed at all).
        if not has_rems or rems_index_rows == 0:
            return [], rems_index_rows
        # The real handler extracts and CLEANS typed numbers ("NDA #020503" ->
        # "NDA020503") into identifiers; ambiguous rows carry none.
        identifiers = {} if rems_ambiguous else {"application_number": f"NDA{APPL_NO}"}
        return [
            SourceRecord(
                source=SourceKind.REMS,
                title="REMS: PROVENTIL HFA",
                source_url="https://www.accessdata.fda.gov/scripts/cder/rems/index.cfm",
                identifiers=identifiers,
                fields={},
                raw={},
            )
        ], rems_index_rows

    def fake_label(application_number: str) -> ApprovedLabel | None:
        if setid is None:
            return None
        assert application_number == "NDA020503"
        return _approved_label()

    def fake_ask(*args: Any, **kwargs: Any) -> QAResult:
        return _qa_result()

    monkeypatch.setattr(orange_book, "product_rows", fake_products)
    monkeypatch.setattr(orange_book, "patent_rows", fake_patents)
    monkeypatch.setattr(orange_book, "exclusivity_rows", fake_exclusivity)
    monkeypatch.setattr(populator, "_drugsfda_records", fake_drugsfda)
    monkeypatch.setattr(populator, "_ndc_records", fake_ndc)
    monkeypatch.setattr(populator, "_shortage_records", fake_shortage)
    monkeypatch.setattr(populator, "_rems_search", fake_rems_search)
    monkeypatch.setattr(populator, "load_latest_approved_label", fake_label)
    monkeypatch.setattr(populator, "ask", fake_ask)
