"""Orange Book ZIP + REMS HTML fetches honor the polite-crawler 429/5xx backoff.

Regression for sources-2: both raw ``active_client.get()`` calls were replaced
with ``get_with_retry``, so a transient 503 is retried instead of surfacing as a
hard failure — matching every openFDA/DailyMed fetch. These tests fail if either
fetch reverts to a single un-retried GET.

Mocked transport only; no network. The backoff ``time.sleep`` is stubbed so the
retry path runs instantly.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx

from regwatch.sources.orange_book import (
    ORANGE_BOOK_ZIP_URL,
    product_rows,
    reset_products_cache,
)
from regwatch.sources.rems import REMS_INDEX_URL, fetch_rems_index_html

# Minimal valid Orange Book ZIP: only products.txt is required (orange_book.py).
_PRODUCTS_TXT = (
    "Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~"
    "Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name\n"
    "ALBUTEROL SULFATE~AEROSOL;INHALATION~PROAIR HFA~TEVA~0.09MG~N~020503~001~"
    "AB~Oct 29, 2004~RLD~RS~RX~TEVA BRANDED PHARM\n"
)
_REMS_HTML = "<table><tr><th>Drug Name</th></tr><tr><td>PROAIR (NDA #020503)</td></tr></table>"


def _products_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("products.txt", _PRODUCTS_TXT)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_with_retry sleeps between attempts; stub it so the retry runs instantly.
    monkeypatch.setattr("regwatch.sources._utils.time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _fresh_orange_book_cache() -> None:
    # The Orange Book ZIP is cached in-process; clear it so the mock is hit.
    reset_products_cache()


def _transient_then_ok(ok: httpx.Response) -> list[httpx.Response]:
    """A single transient 503 followed by the real 200 response."""
    return [httpx.Response(503), ok]


def test_orange_book_zip_fetch_retries_transient_503() -> None:
    ok = httpx.Response(200, content=_products_zip_bytes())
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ORANGE_BOOK_ZIP_URL).mock(side_effect=_transient_then_ok(ok))
        result = product_rows("NDA020503")

    # Two GETs: the 503 was retried, the 200 succeeded — proving get_with_retry.
    assert route.call_count == 2
    assert result.rows  # the retried success actually parsed rows
    assert result.rows[0]["appl_no"] == "020503"


def test_rems_html_fetch_retries_transient_503() -> None:
    ok = httpx.Response(200, text=_REMS_HTML)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(REMS_INDEX_URL).mock(side_effect=_transient_then_ok(ok))
        html = fetch_rems_index_html()

    assert route.call_count == 2
    assert html == _REMS_HTML


def test_orange_book_zip_fetch_single_get_on_success() -> None:
    """No needless retry when the first response is already 200."""
    ok = httpx.Response(200, content=_products_zip_bytes())
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ORANGE_BOOK_ZIP_URL).mock(return_value=ok)
        product_rows("NDA020503")

    assert route.call_count == 1
