"""FDA source handler tests."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
import respx

from regwatch.sources import router as router_mod
from regwatch.sources._utils import application_number_candidates, clean_application_number
from regwatch.sources.drugsfda import DRUGSFDA_ENDPOINT, DrugsFdaHandler
from regwatch.sources.ndc import NDC_ENDPOINT, NdcHandler
from regwatch.sources.orange_book import (
    ORANGE_BOOK_ZIP_URL,
    OrangeBookHandler,
    parse_products_text,
    reset_products_cache,
)
from regwatch.sources.psg import PsgHandler
from regwatch.sources.rems import RemsHandler, parse_rems_rows
from regwatch.sources.router import route_sources, search_sources
from regwatch.sources.shortages import SHORTAGES_ENDPOINT, ShortagesHandler
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion

ORANGE_PRODUCTS = """Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name
ALBUTEROL SULFATE~AEROSOL, METERED;INHALATION~PROAIR HFA~TEVA~0.09MG/INH~N~020503~001~AB~Oct 29, 2004~RLD~RS~RX~TEVA BRANDED PHARM
BECLOMETHASONE DIPROPIONATE~AEROSOL, METERED;INHALATION~QVAR~TEVA~0.04MG/INH~N~020911~001~~~Sep 15, 2000~RLD~RS~RX~TEVA BRANDED PHARM
"""


def test_route_sources_uses_obvious_rules() -> None:
    routed = route_sources(
        SourceQuery(query_text="Is there a REMS and NDC shortage record for clozapine?")
    )
    assert routed == [SourceKind.NDC, SourceKind.SHORTAGE, SourceKind.REMS]

    assert route_sources(SourceQuery(query_text="What TE code is in the Orange Book?")) == [
        SourceKind.ORANGE_BOOK
    ]


def test_route_sources_rs_requires_domain_context() -> None:
    assert route_sources(SourceQuery(query_text="rs.")) == [
        SourceKind.DRUGSFDA,
        SourceKind.ORANGE_BOOK,
        SourceKind.PSG,
    ]
    assert route_sources(
        SourceQuery(query_text="What is the RS?", active_ingredient="albuterol sulfate")
    ) == [SourceKind.ORANGE_BOOK]


def test_search_sources_continues_when_one_handler_fails(
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
                    source_url="https://example.invalid",
                    identifiers={},
                    fields={},
                )
            ]

    monkeypatch.setitem(router_mod._HANDLERS, SourceKind.REMS, _BoomHandler())
    monkeypatch.setitem(router_mod._HANDLERS, SourceKind.PSG, _OkHandler())

    routed, records = search_sources(
        SourceQuery(query_text="rems psg"),
        sources=[SourceKind.REMS, SourceKind.PSG],
    )

    assert routed == [SourceKind.REMS, SourceKind.PSG]
    assert [r.source for r in records] == [SourceKind.PSG]


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
    # The real EOBZIP ships all three files; only products.txt is required.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("products.txt", ORANGE_PRODUCTS)
        zf.writestr(
            "patent.txt",
            "Appl_Type~Appl_No~Product_No~Patent_No~Patent_Expire_Date_Text"
            "~Drug_Substance_Flag~Drug_Product_Flag~Patent_Use_Code~Delist_Flag~Submission_Date\n",
        )
        zf.writestr(
            "exclusivity.txt",
            "Appl_Type~Appl_No~Product_No~Exclusivity_Code~Exclusivity_Date\n",
        )
    return buffer.getvalue()


def test_orange_book_handler_caches_zip_across_searches() -> None:
    reset_products_cache()
    try:
        zip_bytes = _orange_book_zip_bytes()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(ORANGE_BOOK_ZIP_URL).mock(
                return_value=httpx.Response(200, content=zip_bytes)
            )
            handler = OrangeBookHandler()
            first = handler.search(SourceQuery(application_number="NDA020503"))
            second = handler.search(SourceQuery(application_number="NDA020503"))

        # Second search must be a cache hit: exactly one network fetch total.
        assert route.call_count == 1
        assert len(first) == 1
        assert len(second) == 1
        assert first[0].fields["te_code"] == second[0].fields["te_code"] == "AB"
    finally:
        reset_products_cache()


def test_orange_book_handler_injected_text_bypasses_cache() -> None:
    reset_products_cache()
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(500))
            records = OrangeBookHandler(products_text=ORANGE_PRODUCTS).search(
                SourceQuery(application_number="NDA020503")
            )
        # Pre-supplied text must never touch the network or the cache.
        assert route.call_count == 0
        assert len(records) == 1
    finally:
        reset_products_cache()


def test_psg_handler_returns_local_structured_rows() -> None:
    init_db()
    with session_scope() as s:
        doc = PsgDocument(
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
        s.add(doc)
        s.flush()
        assert doc.id is not None
        s.add(
            PsgVersion(
                psg_document_id=doc.id,
                content_hash="hash",
                captured_at=datetime.now(UTC),
                diff_summary="Initial capture.",
            )
        )

    records = PsgHandler().search(SourceQuery(active_ingredient="albuterol sulfate"))
    assert len(records) == 1
    assert records[0].source == SourceKind.PSG
    assert records[0].identifiers["appl_no"] == "020503"
    assert records[0].fields["psg_type"] == "draft"
    assert records[0].fields["latest_diff_summary"] == "Initial capture."


@pytest.mark.parametrize("appl", ["N020503", "NDA020503", "NDA 020503", "020503"])
def test_psg_handler_matches_prefixed_application_number_input(appl: str) -> None:
    # Regression: clean_application_number("N020503") now yields "NDA020503",
    # but the PSG store keys on bare digits (the crawler extracts digits only)
    # — without stripping the type prefix, the advertised single-letter form
    # silently returned zero PSG records.
    init_db()
    with session_scope() as s:
        s.add(
            PsgDocument(
                appl_no="020503",
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol sulfate",
                dosage_form="Aerosol, Metered",
                route="Inhalation",
                rld_or_rs_number="020503,020983,021457",
                psg_type="draft",
                recommended_date="2026-05-21",
                source_url="https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm",
                content_hash="hash-prefixed",
            )
        )

    records = PsgHandler().search(SourceQuery(application_number=appl))
    assert len(records) == 1
    assert records[0].identifiers["appl_no"] == "020503"


def test_drugsfda_handler_maps_openfda_application() -> None:
    payload = {
        "results": [
            {
                "application_number": "NDA020503",
                "sponsor_name": "TEVA BRANDED PHARM",
                "products": [
                    {
                        "brand_name": "PROAIR HFA",
                        "dosage_form": "AEROSOL, METERED",
                        "route": "INHALATION",
                        "marketing_status": ["Prescription"],
                        "active_ingredients": [
                            {"name": "ALBUTEROL SULFATE", "strength": "0.09MG/INH"}
                        ],
                    }
                ],
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DRUGSFDA_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        records = DrugsFdaHandler().search(SourceQuery(application_number="020503"))

    assert records[0].source == SourceKind.DRUGSFDA
    assert records[0].identifiers["application_number"] == "NDA020503"
    assert records[0].fields["products"][0]["brand_name"] == "PROAIR HFA"


def test_ndc_handler_maps_openfda_product() -> None:
    payload = {
        "results": [
            {
                "product_ndc": "12345-678",
                "application_number": "ANDA211091",
                "brand_name": "Sodium Bicarbonate",
                "generic_name": "SODIUM BICARBONATE",
                "labeler_name": "Civica",
                "dosage_form": "INJECTION, SOLUTION",
                "route": ["INTRAVENOUS"],
                "marketing_category": "ANDA",
                "packaging": [{"package_ndc": "12345-678-90"}],
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(NDC_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        records = NdcHandler().search(SourceQuery(ndc="12345-678-90"))

    assert records[0].source == SourceKind.NDC
    assert records[0].identifiers["product_ndc"] == "12345-678"
    assert records[0].identifiers["application_number"] == "ANDA211091"
    assert records[0].fields["dosage_form"] == "INJECTION, SOLUTION"


def test_shortages_handler_maps_openfda_shortage() -> None:
    payload = {
        "results": [
            {
                "generic_name": "ALBUTEROL SULFATE",
                "brand_name": "PROAIR HFA",
                "dosage_form": "AEROSOL",
                "status": "Current",
                "availability": "Limited supply",
                "openfda": {"application_number": ["NDA020503"]},
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(SHORTAGES_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        records = ShortagesHandler().search(SourceQuery(active_ingredient="albuterol sulfate"))

    assert records[0].source == SourceKind.SHORTAGE
    assert records[0].identifiers["application_number"] == "NDA020503"
    assert records[0].fields["status"] == "Current"


def test_shortages_application_number_is_whitespace_normalized() -> None:
    # first_str applies clean_text; the old local helper skipped it, leaving
    # shortages the only handler with an un-normalized identifier.
    payload = {
        "results": [
            {
                "generic_name": "ALBUTEROL SULFATE",
                "openfda": {"application_number": ["NDA  020503\n"]},
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(SHORTAGES_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        records = ShortagesHandler().search(SourceQuery(active_ingredient="albuterol sulfate"))

    assert records[0].identifiers["application_number"] == "NDA 020503"


def test_shortages_dosage_form_alone_does_not_query() -> None:
    assert ShortagesHandler().search(SourceQuery(dosage_form="tablet")) == []


def test_rems_parser_and_handler_return_structured_rows() -> None:
    # The live index renders application numbers in hash format: "NDA #019758".
    html = """
    <table>
      <tr><th>Drug Name</th><th>Application Number</th><th>REMS Document</th></tr>
      <tr>
        <td>Clozapine</td>
        <td>NDA #019758</td>
        <td><a href="/drugsatfda_docs/rems/clozapine.pdf">Document</a></td>
      </tr>
    </table>
    """
    rows = parse_rems_rows(html)
    assert rows[0]["drug_name"] == "Clozapine"
    assert rows[0]["application_number"] == "NDA #019758"

    records = RemsHandler(html=html).search(SourceQuery(brand_name="Clozapine"))
    assert len(records) == 1
    assert records[0].source == SourceKind.REMS
    assert records[0].identifiers["application_number"] == "NDA019758"
    assert records[0].source_url.endswith("/drugsatfda_docs/rems/clozapine.pdf")


def test_rems_application_number_query_matches_live_hash_format() -> None:
    """Regression (B3): the live index writes 'NDA #022549'; the old filter
    term was the raw substring 'nda022549', which can NEVER match that text —
    application-number queries always returned zero rows. Matching now
    compares cleaned, extracted identifiers exactly."""
    html = """
    <table>
      <tr><th>Drug Name</th><th>Application Number</th></tr>
      <tr><td>Embeda</td><td>NDA #022549</td></tr>
      <tr><td>Other Drug</td><td>NDA #999999</td></tr>
    </table>
    """
    records = RemsHandler(html=html).search(SourceQuery(application_number="NDA 022549"))
    assert [r.identifiers["application_number"] for r in records] == ["NDA022549"]

    # A bare-digit query matches the same row through candidate expansion.
    records = RemsHandler(html=html).search(SourceQuery(application_number="022549"))
    assert len(records) == 1

    # A prefixed query for an absent application matches nothing.
    assert RemsHandler(html=html).search(SourceQuery(application_number="NDA022550")) == []


def test_rems_row_with_multiple_application_numbers_surfaces_all() -> None:
    html = """
    <table>
      <tr><th>Drug Name</th><th>Application Number</th></tr>
      <tr><td>Shared System</td><td>NDA #205777, ANDA #210367</td></tr>
    </table>
    """
    records = RemsHandler(html=html).search(SourceQuery(application_number="ANDA210367"))
    assert len(records) == 1
    assert records[0].identifiers["application_number"] == "NDA205777"
    assert records[0].identifiers["application_numbers"] == "NDA205777, ANDA210367"


def test_rems_identifier_requires_application_shaped_value() -> None:
    """A bare name in the Application Number column never becomes a structured
    identifier — identifiers come only from application-shaped text."""
    html = """
    <table>
      <tr><th>Application Number</th><th>Drug Name</th></tr>
      <tr><td>Clozapine</td><td>Shared System REMS</td></tr>
    </table>
    """
    records = RemsHandler(html=html).search(SourceQuery(brand_name="Clozapine"))
    assert len(records) == 1
    assert "application_number" not in records[0].identifiers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("NDA020503", "NDA020503"),
        ("nda 20503", "NDA020503"),
        ("NDA #022549", "NDA022549"),
        ("N020503", "NDA020503"),  # single-letter prefixes (the UI placeholder)
        ("a208677", "ANDA208677"),
        ("B 761034", "BLA761034"),
        ("020503", "020503"),
        ("20503", "020503"),
        ("not-a-number", None),
        (None, None),
    ],
)
def test_clean_application_number_accepts_single_letter_prefixes(
    value: str | None, expected: str | None
) -> None:
    assert clean_application_number(value) == expected


def test_prefixed_input_yields_exactly_its_application() -> None:
    """A prefixed input — long or single-letter — yields exactly its own
    application; only genuinely bare digits expand to the three-way set."""
    assert application_number_candidates("N020503") == ["NDA020503"]
    assert application_number_candidates("ANDA208677") == ["ANDA208677"]
    assert application_number_candidates("020503") == [
        "NDA020503",
        "ANDA020503",
        "BLA020503",
        "020503",
    ]
