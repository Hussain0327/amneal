"""White-Paper template fetch - Supabase Storage fetch-and-cache lane.

Covers the failure discipline: every fetch problem (HTTP error, timeout,
oversize, wrong magic, unset URL) must return None with NO file written, so
the docx writer keeps its loud FALLBACK_MARKER behavior and the next render
retries. Success must be atomic (no .tmp remnants) and cached (second call
makes no HTTP request).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from config.settings import Settings

import regwatch.whitepaper.template_fetch as template_fetch
from regwatch.whitepaper.template_fetch import ensure_template, template_status

_URL = "https://storage.example.invalid/object/sign/regwatch-internal/cra.docx?token=SECRET"

# Smallest body that passes the docx (ZIP local-file-header) magic check.
_DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 64


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# ---------------------------------------------------------------- short-circuits


def test_existing_path_short_circuits_without_http(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"
    path.write_bytes(_DOCX_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when the template is on disk")

    assert ensure_template(path, _URL, client=_client(handler)) == path


def test_url_unset_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"
    assert ensure_template(path, None) is None
    assert ensure_template(path, "") is None
    assert not path.exists()


# --------------------------------------------------------------------- success


def test_fetch_success_caches_and_second_call_skips_http(tmp_path: Path) -> None:
    # Nested dir proves mkdir(parents=True) on the write path.
    path = tmp_path / "templates" / "t.docx"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_DOCX_BYTES)

    with _client(handler) as client:
        assert ensure_template(path, _URL, client=client) == path
        assert path.read_bytes() == _DOCX_BYTES
        # Atomic write left no tmp remnants next to the template.
        assert [p.name for p in path.parent.iterdir()] == ["t.docx"]
        assert calls == 1
        # Cached: the second call must not re-fetch.
        assert ensure_template(path, _URL, client=client) == path
        assert calls == 1


# -------------------------------------------------------------------- failures


def _assert_failed_clean(result: Path | None, tmp_path: Path) -> None:
    """Failure contract: None returned, no template, no partial/tmp files."""
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_http_error_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client(handler) as client:
        _assert_failed_clean(ensure_template(path, _URL, client=client), tmp_path)


def test_timeout_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("storage endpoint stalled")

    with _client(handler) as client:
        _assert_failed_clean(ensure_template(path, _URL, client=client), tmp_path)


def test_oversize_declared_content_length_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.docx"
    monkeypatch.setattr(template_fetch, "_MAX_TEMPLATE_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        # Lying Content-Length: declared over the cap, tiny actual body.
        return httpx.Response(200, headers={"content-length": "1000000"}, content=b"PK\x03\x04")

    with _client(handler) as client:
        _assert_failed_clean(ensure_template(path, _URL, client=client), tmp_path)


def test_oversize_streamed_body_aborts_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.docx"
    monkeypatch.setattr(template_fetch, "_MAX_TEMPLATE_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        # No Content-Length so only the streamed byte-count guard can fire.
        resp = httpx.Response(200, content=b"PK\x03\x04" + b"x" * 200)
        resp.headers.pop("content-length", None)
        return resp

    with _client(handler) as client:
        _assert_failed_clean(ensure_template(path, _URL, client=client), tmp_path)


def test_fetch_failure_log_never_carries_the_signed_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """str() on httpx errors embeds the full request URL (HTTPStatusError
    interpolates response.url, token included): the failure log must carry
    only the redacted URL and a sanitized error, never the secret."""
    events: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        def warning(self, event: str, **kw: object) -> None:
            events.append((event, kw))

        def info(self, event: str, **kw: object) -> None:
            events.append((event, kw))

    monkeypatch.setattr(template_fetch, "log", _Recorder())
    path = tmp_path / "t.docx"

    def handler(request: httpx.Request) -> httpx.Response:
        # The realistic failure: the signed URL's token expired.
        return httpx.Response(403)

    with _client(handler) as client:
        assert ensure_template(path, _URL, client=client) is None

    assert [event for event, _ in events] == ["whitepaper_template_fetch_failed"]
    rendered = repr(events)
    assert "SECRET" not in rendered and "token=" not in rendered
    assert "HTTP 403" in rendered  # the status still lands for diagnosis


def test_bad_magic_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"

    def handler(request: httpx.Request) -> httpx.Response:
        # A storage error page served with 200 must never be cached as a docx.
        return httpx.Response(200, content=b"<html>expired token</html>")

    with _client(handler) as client:
        _assert_failed_clean(ensure_template(path, _URL, client=client), tmp_path)


# ------------------------------------------------------------- template_status


def test_template_status_present(tmp_path: Path) -> None:
    path = tmp_path / "t.docx"
    path.write_bytes(_DOCX_BYTES)
    assert template_status(path, None) == "present"
    assert template_status(path, _URL) == "present"


def test_template_status_fetchable(tmp_path: Path) -> None:
    assert template_status(tmp_path / "t.docx", _URL) == "fetchable"


def test_template_status_absent(tmp_path: Path) -> None:
    assert template_status(tmp_path / "t.docx", None) == "absent"
    assert template_status(tmp_path / "t.docx", "") == "absent"


# ------------------------------------------------------------------- settings


def test_settings_template_url_env_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHITEPAPER_TEMPLATE_URL", raising=False)
    # _env_file=None isolates from the repo's .env (house pattern: direct
    # Settings kwargs need the call-arg ignore, see test_ready_metrics).
    assert Settings(_env_file=None).whitepaper_template_url is None  # type: ignore[call-arg]
    monkeypatch.setenv("WHITEPAPER_TEMPLATE_URL", _URL)
    assert Settings(_env_file=None).whitepaper_template_url == _URL  # type: ignore[call-arg]
