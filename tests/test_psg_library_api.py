"""Reference-library API tests: /psg/documents listing + PDF serving.

The listing feeds the Compliance Studio rail; the PDF route streams the
document inline. These cover the failure paths the routes exist to make
legible: auth, pagination determinism, local-cache hits vs the remote fda.gov
branch, the ETag short-circuit, and every mapped upstream failure.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
import respx
from config.settings import get_settings
from fastapi.testclient import TestClient

from regwatch.api.main import app
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument

_PDF = b"%PDF-1.4 tiny reference-library test body"
_PDF_HASH = hashlib.sha256(_PDF).hexdigest()
_FDA_URL = "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf"


def _mk_doc(**overrides: Any) -> int:
    """Insert one psg_document row directly; returns its id."""
    fields: dict[str, Any] = {
        "active_ingredient": "Albuterol Sulfate",
        "normalized_name": "albuterol sulfate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
        "appl_no": None,
        "psg_type": "final",
        "recommended_date": "2024-05-01",
        "source_url": _FDA_URL,
        "pdf_path": None,
        "content_hash": _PDF_HASH,
    }
    fields.update(overrides)
    with session_scope() as s:
        row = PsgDocument(**fields)
        s.add(row)
        s.flush()
        assert row.id is not None
        return row.id


# ---------------------------------------------------------------------------
# GET /psg/documents (listing)
# ---------------------------------------------------------------------------


def test_list_requires_auth_401() -> None:
    with TestClient(app) as client:
        assert client.get("/psg/documents").status_code == 401


def test_list_empty_db(auth_client: TestClient) -> None:
    body = auth_client.get("/psg/documents").json()
    assert body == {"count": 0, "total": 0, "limit": 2000, "offset": 0, "documents": []}


def test_list_fields_and_stripped_name(auth_client: TestClient) -> None:
    doc_id = _mk_doc(appl_no="020503")
    body = auth_client.get("/psg/documents").json()
    assert body["count"] == body["total"] == 1
    assert body["documents"] == [
        {
            "id": doc_id,
            "active_ingredient": "Albuterol Sulfate",
            "normalized_name": "albuterol sulfate",
            "stripped_name": "albuterol",
            "dosage_form": "Aerosol, Metered",
            "route": "Inhalation",
            "appl_no": "020503",
            "psg_type": "final",
            "recommended_date": "2024-05-01",
            "source_url": _FDA_URL,
        }
    ]


def test_list_ordering_deterministic(auth_client: TestClient) -> None:
    # Inserted deliberately out of order; the response must sort by
    # normalized_name, then NULL-as-"" dosage_form, then route, then psg_type.
    d_b = _mk_doc(normalized_name="budesonide", active_ingredient="Budesonide")
    d_a_tab_final = _mk_doc(normalized_name="albuterol sulfate", dosage_form="Tablet")
    d_a_null = _mk_doc(normalized_name="albuterol sulfate", dosage_form=None)
    d_a_tab_draft = _mk_doc(
        normalized_name="albuterol sulfate", dosage_form="Tablet", psg_type="draft"
    )
    body = auth_client.get("/psg/documents").json()
    got = [doc["id"] for doc in body["documents"]]
    # NULL dosage_form coalesces to "" (sorts first); draft < final on ties.
    assert got == [d_a_null, d_a_tab_draft, d_a_tab_final, d_b]


def test_list_pagination_gapless(auth_client: TestClient) -> None:
    ids = {
        _mk_doc(normalized_name=name, active_ingredient=name.title())
        for name in ("alpha", "bravo", "charlie")
    }
    page1 = auth_client.get("/psg/documents", params={"limit": 2, "offset": 0}).json()
    page2 = auth_client.get("/psg/documents", params={"limit": 2, "offset": 2}).json()
    assert page1["count"] == 2 and page1["total"] == 3
    assert page2["count"] == 1 and page2["total"] == 3
    union = [d["id"] for d in page1["documents"]] + [d["id"] for d in page2["documents"]]
    assert len(union) == len(set(union)) == 3
    assert set(union) == ids


def test_list_limit_bounds_422(auth_client: TestClient) -> None:
    assert auth_client.get("/psg/documents", params={"limit": 5001}).status_code == 422
    assert auth_client.get("/psg/documents", params={"limit": 0}).status_code == 422


# ---------------------------------------------------------------------------
# GET/HEAD /psg/documents/{doc_id}/pdf
# ---------------------------------------------------------------------------


def test_pdf_requires_auth_401() -> None:
    with TestClient(app) as client:
        assert client.get("/psg/documents/1/pdf").status_code == 401
        assert client.head("/psg/documents/1/pdf").status_code == 401


def test_pdf_unknown_id_404(auth_client: TestClient) -> None:
    assert auth_client.get("/psg/documents/999999/pdf").status_code == 404


@respx.mock  # no routes mocked: ANY network call raises and fails the test
def test_pdf_local_hit_via_pdf_path(auth_client: TestClient, tmp_path: Any) -> None:
    local = tmp_path / "stored.pdf"
    local.write_bytes(_PDF)
    doc_id = _mk_doc(appl_no="020503", pdf_path=str(local))
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'inline; filename="PSG_020503.pdf"'
    assert resp.headers["etag"] == f'"{_PDF_HASH}"'
    assert resp.content == _PDF


@respx.mock
def test_pdf_local_hit_via_deterministic_cache_path(auth_client: TestClient) -> None:
    raw_dir = get_settings().raw_pdf_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"PSG_020503_{_PDF_HASH[:8]}.pdf").write_bytes(_PDF)
    doc_id = _mk_doc(appl_no="020503", pdf_path=None)
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    assert resp.content == _PDF


@respx.mock
def test_pdf_etag_304_short_circuits(auth_client: TestClient, tmp_path: Any) -> None:
    # pdf_path points nowhere and raw_pdf_dir holds no file: only the DB row
    # can answer, so a 304 here proves neither disk nor network was consulted
    # (respx with zero routes raises on any request).
    doc_id = _mk_doc(appl_no="020503", pdf_path=str(tmp_path / "gone.pdf"))
    for header_value in (f'"{_PDF_HASH}"', f'W/"{_PDF_HASH}"', f'"other", "{_PDF_HASH}"'):
        resp = auth_client.get(
            f"/psg/documents/{doc_id}/pdf", headers={"If-None-Match": header_value}
        )
        assert resp.status_code == 304
        assert resp.headers["etag"] == f'"{_PDF_HASH}"'


@respx.mock
def test_pdf_remote_fetch_serves_and_caches(auth_client: TestClient) -> None:
    respx.get(_FDA_URL).mock(return_value=httpx.Response(200, content=_PDF))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    assert resp.content == _PDF
    assert resp.headers["etag"] == f'"{_PDF_HASH}"'
    # download_pdf's write-through cache: the next open on this machine is a
    # local hit under the crawler's own naming scheme.
    assert (get_settings().raw_pdf_dir / f"PSG_020503_{_PDF_HASH[:8]}.pdf").is_file()


@respx.mock
def test_pdf_remote_timeout_504(auth_client: TestClient) -> None:
    respx.get(_FDA_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 504
    assert "fda.gov" in resp.json()["detail"]
    assert _FDA_URL not in resp.json()["detail"]


@respx.mock
def test_pdf_remote_non_pdf_502(auth_client: TestClient) -> None:
    respx.get(_FDA_URL).mock(
        return_value=httpx.Response(200, content=b"<html>challenge page</html>")
    )
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream did not return a PDF"


@respx.mock
def test_pdf_remote_oversize_502(auth_client: TestClient) -> None:
    # Oversize by declared Content-Length: exercises the cap without a 60MB body.
    respx.get(_FDA_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-length": str(60_000_000)}, content=b"%PDF-"
        )
    )
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream PDF exceeds the size cap"


@respx.mock
def test_pdf_upstream_404_502_without_url(auth_client: TestClient) -> None:
    route = respx.get(_FDA_URL).mock(return_value=httpx.Response(404))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert "404" in resp.json()["detail"]
    assert _FDA_URL not in resp.json()["detail"]
    assert route.call_count == 1  # 4xx is terminal, never retried


@respx.mock
def test_pdf_retried_out_5xx_502(auth_client: TestClient) -> None:
    # A persistent 503 exhausts the crawler's bounded retry and reraises its
    # INTERNAL marker class, not HTTPStatusError -- the route must still map
    # it to a legible 502 instead of a 500.
    route = respx.get(_FDA_URL).mock(return_value=httpx.Response(503))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "fda.gov kept failing to serve the PDF"
    assert route.call_count == 3  # bounded retry, not a storm


@respx.mock
def test_pdf_remote_redirect_loop_502(auth_client: TestClient) -> None:
    # TooManyRedirects subclasses RequestError, NOT TransportError -- the
    # catch-all arm must be httpx.HTTPError or this escapes as a 500.
    respx.get(_FDA_URL).mock(side_effect=httpx.TooManyRedirects("loop"))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "could not reach fda.gov for the PDF"


@respx.mock
def test_pdf_remote_decoding_error_502(auth_client: TestClient) -> None:
    respx.get(_FDA_URL).mock(side_effect=httpx.DecodingError("bad gzip"))
    doc_id = _mk_doc(appl_no="020503")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "could not reach fda.gov for the PDF"


@respx.mock
def test_pdf_unwritable_cache_dir_still_serves(auth_client: TestClient) -> None:
    # A full/read-only disk must not turn a successful fetch into a 500: the
    # write-through cache is best-effort, the bytes in hand are the answer.
    raw_dir = get_settings().raw_pdf_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.chmod(0o500)
    try:
        respx.get(_FDA_URL).mock(return_value=httpx.Response(200, content=_PDF))
        doc_id = _mk_doc(appl_no="020503")
        resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
        assert resp.status_code == 200
        assert resp.content == _PDF
    finally:
        raw_dir.chmod(0o700)


@respx.mock
def test_pdf_corrupt_local_file_falls_through_to_remote(
    auth_client: TestClient, tmp_path: Any
) -> None:
    # A truncated/foreign file at the expected path must never be served under
    # the content_hash ETag; the verified miss recovers via fda.gov.
    bad = tmp_path / "truncated.pdf"
    bad.write_bytes(_PDF[: len(_PDF) // 2])
    respx.get(_FDA_URL).mock(return_value=httpx.Response(200, content=_PDF))
    doc_id = _mk_doc(appl_no="020503", pdf_path=str(bad))
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    assert resp.content == _PDF
    assert resp.headers["etag"] == f'"{_PDF_HASH}"'


@respx.mock
def test_pdf_hash_drift_serves_with_new_etag(auth_client: TestClient) -> None:
    respx.get(_FDA_URL).mock(return_value=httpx.Response(200, content=_PDF))
    doc_id = _mk_doc(appl_no="020503", content_hash="0" * 64)
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    # FDA revised since the row was written: the served ETag must describe the
    # bytes actually returned, not the stale row.
    assert resp.headers["etag"] == f'"{_PDF_HASH}"'


@respx.mock
def test_pdf_non_fda_source_url_502(auth_client: TestClient) -> None:
    doc_id = _mk_doc(appl_no="020503", source_url="https://evil.example/x.pdf")
    resp = auth_client.get(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "document has no fetchable FDA source"


@respx.mock
def test_pdf_remote_rate_limited_429_local_hits_unmetered(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import config.settings as cs

    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    cs.get_settings.cache_clear()
    respx.get(_FDA_URL).mock(return_value=httpx.Response(200, content=_PDF))
    remote_id = _mk_doc(appl_no="020503")
    assert auth_client.get(f"/psg/documents/{remote_id}/pdf").status_code == 200
    # Force the second request onto the remote branch again by removing the
    # write-through cache file the first request created.
    cached = get_settings().raw_pdf_dir / f"PSG_020503_{_PDF_HASH[:8]}.pdf"
    cached.unlink()
    assert auth_client.get(f"/psg/documents/{remote_id}/pdf").status_code == 429
    # A local-disk hit does not touch the remote budget.
    local = tmp_path / "local.pdf"
    local.write_bytes(_PDF)
    local_id = _mk_doc(appl_no="020504", pdf_path=str(local))
    assert auth_client.get(f"/psg/documents/{local_id}/pdf").status_code == 200


@respx.mock
def test_pdf_head_answers_from_db_row_only(auth_client: TestClient, tmp_path: Any) -> None:
    # No local file, no mocked network route: a 200 proves the probe never
    # leaves the DB row (an FDA source_url is enough to vouch for the GET).
    doc_id = _mk_doc(appl_no="020503", pdf_path=str(tmp_path / "gone.pdf"))
    resp = auth_client.head(f"/psg/documents/{doc_id}/pdf")
    assert resp.status_code == 200
    assert resp.headers["etag"] == f'"{_PDF_HASH}"'
    assert resp.headers["content-disposition"] == 'inline; filename="PSG_020503.pdf"'
    assert resp.content == b""
    assert auth_client.head("/psg/documents/999999/pdf").status_code == 404


@respx.mock
def test_pdf_head_refuses_guaranteed_fail_row(auth_client: TestClient, tmp_path: Any) -> None:
    # Non-FDA source_url and no local file: the GET would 502 with certainty,
    # so a 200 probe would defeat the probe. A local file rescues it.
    doc_id = _mk_doc(appl_no="020503", source_url="https://evil.example/x.pdf")
    assert auth_client.head(f"/psg/documents/{doc_id}/pdf").status_code == 502
    local = tmp_path / "cached.pdf"
    local.write_bytes(_PDF)
    rescued = _mk_doc(
        appl_no="020504", source_url="https://evil.example/y.pdf", pdf_path=str(local)
    )
    assert auth_client.head(f"/psg/documents/{rescued}/pdf").status_code == 200
