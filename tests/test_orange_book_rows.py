"""Orange Book patent/exclusivity/product row tests (mocked ZIP download).

Fixture headers mirror the live EOBZIP files byte-for-byte (verified against
the May 2026 snapshot): ``products.txt``, ``patent.txt``, ``exclusivity.txt``
are tilde-delimited with the column names pinned in ``orange_book.py``.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from regwatch.sources.orange_book import (
    ORANGE_BOOK_ZIP_URL,
    _select_member,
    exclusivity_rows,
    patent_rows,
    product_rows,
    reset_products_cache,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _zip_with(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def _zip_bytes() -> bytes:
    return _zip_with(
        {
            "products.txt": (_FIXTURES / "orange_book_products.txt").read_text(),
            "patent.txt": (_FIXTURES / "orange_book_patent.txt").read_text(),
            "exclusivity.txt": (_FIXTURES / "orange_book_exclusivity.txt").read_text(),
        }
    )


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    reset_products_cache()
    yield
    reset_products_cache()


def test_product_rows_filter_by_application_type_and_number() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=_zip_bytes()))
        result = product_rows("NDA 020503")

    assert [row["product_no"] for row in result.rows] == ["001", "002"]
    assert {row["appl_type"] for row in result.rows} == {"N"}
    assert result.rows[0]["ingredient"] == "ALBUTEROL SULFATE"
    # The live RLD/RS columns are Yes/No flags (never the literal "RLD"/"RS").
    assert result.rows[0]["rld"] == "Yes"
    assert result.rows[0]["rs"] == "Yes"
    assert result.rows[1]["rld"] == "No"
    assert result.fetched_at.tzinfo is not None


def test_patent_rows_surface_raw_columns_only() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=_zip_bytes()))
        result = patent_rows("NDA020503")

    assert [row["patent_no"] for row in result.rows] == ["6868851", "7105152"]
    first = result.rows[0]
    assert first["patent_expire_date"] == "Dec 6, 2025"
    assert first["drug_product_flag"] == "Y"
    assert first["patent_use_code"] == "U-141"
    assert first["submission_date"] == "Jun 30, 2005"
    # Raw rows only: no classification keys ever appear (INV-3).
    assert not {"paragraph", "eligibility", "classification"} & set(first)


def test_bare_digits_match_across_application_types() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=_zip_bytes()))
        result = patent_rows("020503")

    assert [row["patent_no"] for row in result.rows] == ["6868851", "7105152", "9999999"]
    assert {row["appl_type"] for row in result.rows} == {"N", "A"}


def test_exclusivity_rows_for_application() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=_zip_bytes()))
        result = exclusivity_rows("NDA020503")

    assert result.rows == [
        {
            "appl_type": "N",
            "appl_no": "020503",
            "product_no": "001",
            "exclusivity_code": "NCE",
            "exclusivity_date": "Oct 29, 2009",
        }
    ]


def test_three_row_apis_share_one_zip_download() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ORANGE_BOOK_ZIP_URL).mock(
            return_value=httpx.Response(200, content=_zip_bytes())
        )
        products = product_rows("NDA020503")
        patents = patent_rows("NDA020503")
        exclusivity = exclusivity_rows("NDA020503")

    assert route.call_count == 1
    # One download → one auditable snapshot timestamp across all three.
    assert products.fetched_at == patents.fetched_at == exclusivity.fetched_at


def test_zero_rows_means_queried_and_absent() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=_zip_bytes()))
        result = exclusivity_rows("NDA999999")

    assert result.rows == []
    assert result.fetched_at is not None


def test_unparseable_application_number_raises() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        patent_rows("not-a-number")


def test_missing_patent_and_exclusivity_degrade_to_empty_rows() -> None:
    """Only products.txt is required (B4): a ZIP without patent/exclusivity
    files yields empty row sets for those APIs instead of failing the whole
    snapshot — but the degradation is SURFACED on ``member_missing`` so the
    empty rows never read as "queried and absent" or replace a previous
    durable snapshot (INV-5)."""
    content = _zip_with({"products.txt": (_FIXTURES / "orange_book_products.txt").read_text()})
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=content))
        products = product_rows("NDA020503")
        patents = patent_rows("NDA020503")
        exclusivity = exclusivity_rows("NDA020503")

    assert products.rows  # the required file still parses
    assert products.member_missing is False
    assert patents.rows == []
    assert patents.member_missing is True
    assert exclusivity.rows == []
    assert exclusivity.member_missing is True


def test_present_but_empty_member_is_not_flagged_missing() -> None:
    """An empty patent.txt IS a successful (zero-row) query — only an absent
    file carries the member_missing degradation signal."""
    content = _zip_with(
        {
            "products.txt": (_FIXTURES / "orange_book_products.txt").read_text(),
            "patent.txt": "Appl_Type~Appl_No~Product_No~Patent_No\n",
            "exclusivity.txt": (_FIXTURES / "orange_book_exclusivity.txt").read_text(),
        }
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=content))
        patents = patent_rows("NDA020503")

    assert patents.rows == []
    assert patents.member_missing is False


def test_missing_products_file_raises() -> None:
    content = _zip_with(
        {
            "patent.txt": (_FIXTURES / "orange_book_patent.txt").read_text(),
            "exclusivity.txt": (_FIXTURES / "orange_book_exclusivity.txt").read_text(),
        }
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=content))
        with pytest.raises(RuntimeError, match=r"products\.txt"):
            product_rows("NDA020503")


def test_nested_or_renamed_members_never_shadow_top_level() -> None:
    """Regression (B4): the old loop matched any name *ending* in the member
    string and let the LAST archive entry win, so 'zz_old_products.txt' or a
    nested copy could shadow the real top-level products.txt."""
    real = (_FIXTURES / "orange_book_products.txt").read_text()
    header = real.splitlines()[0]
    decoy = header + "\nDECOY~DECOY~DECOY~DECOY~DECOY~N~999998~001~~Jan 1, 2001~No~No~RX~DECOY\n"
    content = _zip_with(
        {
            "products.txt": real,
            "patent.txt": (_FIXTURES / "orange_book_patent.txt").read_text(),
            "exclusivity.txt": (_FIXTURES / "orange_book_exclusivity.txt").read_text(),
            # Both decoys come AFTER the real member in the archive, where the
            # old last-match-wins loop would have shadowed it.
            "zarchive/products.txt": decoy,
            "zz_old_products.txt": decoy,
        }
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=httpx.Response(200, content=content))
        result = product_rows("NDA020503")

    assert [row["product_no"] for row in result.rows] == ["001", "002"]


def test_select_member_deterministic_first_preference() -> None:
    """Fewest path separators win, then lexicographic; basename must equal the
    member name exactly, so 'old_products.txt' is never a candidate at all."""
    names = ["b/products.txt", "a/products.txt", "x/y/products.txt", "old_products.txt"]
    assert _select_member(names, "products.txt") == "a/products.txt"
    assert _select_member(["EOBZIP/Products.txt"], "products.txt") == "EOBZIP/Products.txt"
    assert _select_member(["archive/products.txt", "products.txt"], "products.txt") == (
        "products.txt"
    )
    assert _select_member(["old_products.txt"], "products.txt") is None
