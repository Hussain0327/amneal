"""Handlers and routing for the authoritative FDA source universe."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
import respx

from regwatch.sources import router as router_mod
from regwatch.sources._utils import application_number_candidates, clean_application_number
from regwatch.sources.be_guidance import FdaBeGuidanceHandler
from regwatch.sources.orange_book import (
    ORANGE_BOOK_ZIP_URL,
    OrangeBookHandler,
    parse_products_text,
    reset_products_cache,
)
from regwatch.sources.policy import SourcePolicyError
from regwatch.sources.psg import PsgHandler
from regwatch.sources.router import route_sources, search_sources
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion

ORANGE_PRODUCTS = """Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name
ALBUTEROL SULFATE~AEROSOL, METERED;INHALATION~PROAIR HFA~TEVA~0.09MG/INH~N~020503~001~AB~Oct 29, 2004~RLD~RS~RX~TEVA BRANDED PHARM
BECLOMETHASONE DIPROPIONATE~AEROSOL, METERED;INHALATION~QVAR~TEVA~0.04MG/INH~N~020911~001~~~Sep 15, 2000~RLD~RS~RX~TEVA BRANDED PHARM
"""


def test_route_sources_uses_only_approved_families() -> None:
    assert route_sources(
        SourceQuery(query_text="Show the clinical review and bioequivalence guidance")
    ) == [
        SourceKind.ACTION_PACKAGE,
        SourceKind.PSG,
        SourceKind.FDA_BE_GUIDANCE,
    ]
    assert route_sources(SourceQuery(query_text="What TE code is in the Orange Book?")) == [
        SourceKind.ORANGE_BOOK
    ]
    assert route_sources(SourceQuery(query_text="rs.")) == [
        SourceKind.DRUGSFDA,
        SourceKind.ORANGE_BOOK,
        SourceKind.PSG,
        SourceKind.FDA_BE_GUIDANCE,
    ]


@pytest.mark.parametrize(
    "retired",
    [SourceKind.NDC, SourceKind.SHORTAGE, SourceKind.REMS, SourceKind.DAILYMED],
)
def test_router_rejects_retired_source_kinds(retired: SourceKind) -> None:
    with pytest.raises(ValueError, match="outside the authoritative FDA policy"):
        route_sources(SourceQuery(query_text="anything"), requested=[retired])


def test_search_sources_continues_when_one_approved_handler_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomHandler:
        def search(self, query: SourceQuery, *, client: httpx.Client | None = None) -> list:
            raise httpx.TimeoutException("boom")

    class _OkHandler:
        def search(
            self,
            query: SourceQuery,
            *,
            client: httpx.Client | None = None,
        ) -> list[SourceRecord]:
            return [
                SourceRecord(
                    source=SourceKind.PSG,
                    title="ok",
                    source_url="https://www.accessdata.fda.gov/drugsatfda_docs/psg/test.pdf",
                )
            ]

    monkeypatch.setitem(router_mod._HANDLERS, SourceKind.ACTION_PACKAGE, _BoomHandler())
    monkeypatch.setitem(router_mod._HANDLERS, SourceKind.PSG, _OkHandler())

    routed, records = search_sources(
        SourceQuery(query_text="clinical review PSG"),
        sources=[SourceKind.ACTION_PACKAGE, SourceKind.PSG],
    )
    assert routed == [SourceKind.ACTION_PACKAGE, SourceKind.PSG]
    assert [record.source for record in records] == [SourceKind.PSG]


def test_parse_orange_book_products_text() -> None:
    rows = parse_products_text(ORANGE_PRODUCTS)
    assert rows[0]["appl_no"] == "020503"
    assert rows[0]["te_code"] == "AB"
    assert rows[0]["rld"] == "RLD"
    assert rows[0]["rs"] == "RS"


def test_orange_book_handler_filters_by_application_number() -> None:
    records = OrangeBookHandler(products_text=ORANGE_PRODUCTS).search(
        SourceQuery(application_number="NDA020503")
    )
    assert len(records) == 1
    assert records[0].source == SourceKind.ORANGE_BOOK
    assert records[0].identifiers["application_number"] == "020503"
    assert records[0].fields["te_code"] == "AB"


def _orange_book_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("products.txt", ORANGE_PRODUCTS)
        archive.writestr(
            "patent.txt",
            "Appl_Type~Appl_No~Product_No~Patent_No~Patent_Expire_Date_Text"
            "~Drug_Substance_Flag~Drug_Product_Flag~Patent_Use_Code~Delist_Flag~Submission_Date\n",
        )
        archive.writestr(
            "exclusivity.txt",
            "Appl_Type~Appl_No~Product_No~Exclusivity_Code~Exclusivity_Date\n",
        )
    return buffer.getvalue()


def test_orange_book_handler_caches_one_validated_snapshot() -> None:
    reset_products_cache()
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(ORANGE_BOOK_ZIP_URL).mock(
                return_value=httpx.Response(200, content=_orange_book_zip_bytes())
            )
            handler = OrangeBookHandler()
            first = handler.search(SourceQuery(application_number="NDA020503"))
            second = handler.search(SourceQuery(application_number="NDA020503"))
        assert route.call_count == 1
        assert first[0].fields["te_code"] == second[0].fields["te_code"] == "AB"
    finally:
        reset_products_cache()


def test_psg_handler_returns_local_structured_rows() -> None:
    init_db()
    with session_scope() as session:
        document = PsgDocument(
            appl_no="020503",
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            rld_or_rs_number="020503,020983,021457",
            psg_type="draft",
            recommended_date="2026-05-21",
            source_url="https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm",
            content_hash="hash",
        )
        session.add(document)
        session.flush()
        assert document.id is not None
        session.add(
            PsgVersion(
                psg_document_id=document.id,
                content_hash="hash",
                captured_at=datetime.now(UTC),
                diff_summary="Initial capture.",
            )
        )

    records = PsgHandler().search(SourceQuery(active_ingredient="albuterol sulfate"))
    assert len(records) == 1
    assert records[0].identifiers["appl_no"] == "020503"
    assert records[0].fields["latest_diff_summary"] == "Initial capture."


def test_reviewed_be_guidance_handler_is_manifest_backed() -> None:
    records = FdaBeGuidanceHandler().search(SourceQuery(query_text="bioequivalence statistics"))
    assert records
    assert all(record.source == SourceKind.FDA_BE_GUIDANCE for record in records)
    assert all(record.source_url.startswith("https://www.fda.gov/media/") for record in records)


@pytest.mark.parametrize("appl", ["N020503", "NDA020503", "NDA 020503", "020503"])
def test_psg_handler_matches_prefixed_application_number(appl: str) -> None:
    init_db()
    with session_scope() as session:
        session.add(
            PsgDocument(
                appl_no="020503",
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol sulfate",
                dosage_form="Aerosol, Metered",
                route="Inhalation",
                rld_or_rs_number="020503",
                psg_type="draft",
                recommended_date="2026-05-21",
                source_url="https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm",
                content_hash="hash-prefixed",
            )
        )
    assert (
        PsgHandler().search(SourceQuery(application_number=appl))[0].identifiers["appl_no"]
        == "020503"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("NDA020503", "NDA020503"),
        ("nda 20503", "NDA020503"),
        ("N020503", "NDA020503"),
        ("a208677", "ANDA208677"),
        ("B 761034", "BLA761034"),
        ("020503", "020503"),
        ("not-a-number", None),
        (None, None),
    ],
)
def test_clean_application_number(value: str | None, expected: str | None) -> None:
    assert clean_application_number(value) == expected


def test_prefixed_input_yields_exactly_its_application() -> None:
    assert application_number_candidates("N020503") == ["NDA020503"]
    assert application_number_candidates("ANDA208677") == ["ANDA208677"]
    assert application_number_candidates("020503") == [
        "NDA020503",
        "ANDA020503",
        "BLA020503",
        "020503",
    ]


def test_retired_handler_raises_policy_error() -> None:
    from regwatch.sources.ndc import NdcHandler

    with pytest.raises(SourcePolicyError):
        NdcHandler().search(SourceQuery(ndc="12345-678"))
