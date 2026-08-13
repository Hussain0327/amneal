from __future__ import annotations

import httpx
import pytest

from regwatch.sources.http import SourceTooLargeError, get_authoritative_bytes
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
