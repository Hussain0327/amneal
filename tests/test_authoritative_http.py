from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from regwatch.sources.http import (
    SourceMissingError,
    SourceTooLargeError,
    download_authoritative_file,
    get_authoritative_bytes,
)
from regwatch.sources.policy import FdaSourceFamily, SourcePolicyError


def test_authoritative_fetch_returns_validated_fda_body() -> None:
    requested = "https://www.fda.gov/media/89850/download"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == requested
        return httpx.Response(200, content=b"official", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final_url, body, _ = get_authoritative_bytes(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=100,
        )
    assert final_url == requested
    assert body == b"official"


def test_authoritative_fetch_rejects_oversize_response() -> None:
    requested = "https://www.fda.gov/media/89850/download"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceTooLargeError),
    ):
        get_authoritative_bytes(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=10,
        )


def test_authoritative_fetch_exposes_only_exact_404_as_missing() -> None:
    requested = "https://www.fda.gov/media/89850/download"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceMissingError) as raised,
    ):
        get_authoritative_bytes(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=100,
        )
    assert raised.value.response.status_code == 404


def test_authoritative_fetch_does_not_classify_other_client_errors_as_missing() -> None:
    requested = "https://www.fda.gov/media/89850/download"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError) as raised,
    ):
        get_authoritative_bytes(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=100,
        )
    assert raised.value.response.status_code == 410
    assert not isinstance(raised.value, SourceMissingError)


def test_authoritative_fetch_rejects_redirect_outside_family() -> None:
    requested = "https://www.fda.gov/media/89850/download"
    redirected = "https://api.fda.gov/drug/drugsfda.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == requested:
            return httpx.Response(302, headers={"location": redirected}, request=request)
        return httpx.Response(200, content=b"not allowed", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client,
        pytest.raises(SourcePolicyError),
    ):
        get_authoritative_bytes(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=100,
        )


def test_authoritative_file_download_hashes_and_unlinks(tmp_path: Path) -> None:
    requested = "https://www.fda.gov/media/89850/download"
    body = b"%PDF-1.7\nstreamed FDA bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter((body[:8], body[8:])), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with download_authoritative_file(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=100,
            directory=tmp_path,
        ) as downloaded:
            staged_path = downloaded.path
            assert staged_path.read_bytes() == body
            assert downloaded.byte_size == len(body)
            assert downloaded.sha256 == hashlib.sha256(body).hexdigest()
        assert not staged_path.exists()


def test_authoritative_file_download_unlinks_partial_failure(tmp_path: Path) -> None:
    requested = "https://www.fda.gov/media/89850/download"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceTooLargeError),
        download_authoritative_file(
            client,
            requested,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=10,
            directory=tmp_path,
        ),
    ):
        raise AssertionError("oversized download unexpectedly yielded")
    assert list(tmp_path.iterdir()) == []
