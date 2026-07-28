"""H1: Sentry wiring — OFF unless SENTRY_DSN is set; safe options; capture points."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import sentry_sdk
from config.settings import get_settings

from regwatch.common import observability as obs


class _InitRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


# ---------- settings ----------


def test_sentry_settings_default_off() -> None:
    s = get_settings()
    assert s.sentry_dsn is None
    assert s.sentry_environment == "dev"


def test_sentry_dsn_blank_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("SENTRY_DSN", "   ")
    cs.get_settings.cache_clear()
    assert cs.get_settings().sentry_dsn is None


def test_sentry_environment_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("SENTRY_ENVIRONMENT", "prod")
    cs.get_settings.cache_clear()
    assert cs.get_settings().sentry_environment == "prod"


# ---------- init gating + options ----------


def test_init_sentry_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _InitRecorder()
    monkeypatch.setattr(sentry_sdk, "init", recorder)
    assert obs.init_sentry(get_settings()) is False
    assert recorder.calls == []


def test_init_sentry_initializes_with_safe_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("SENTRY_DSN", "https://key@o0.ingest.sentry.io/0")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    cs.get_settings.cache_clear()
    recorder = _InitRecorder()
    monkeypatch.setattr(sentry_sdk, "init", recorder)

    assert obs.init_sentry(cs.get_settings()) is True
    assert len(recorder.calls) == 1
    kwargs = recorder.calls[0]
    assert kwargs["dsn"] == "https://key@o0.ingest.sentry.io/0"
    assert kwargs["environment"] == "staging"
    assert kwargs["traces_sample_rate"] == 0.1
    assert kwargs["profiles_sample_rate"] == 0.0  # profiling off
    assert kwargs["send_default_pii"] is False  # no PII
    assert kwargs["max_request_body_size"] == "never"  # no question text in events
    # The SDK default (True) serializes traceback-frame locals into events —
    # /query frames hold question/answer/user_prompt, /auth frames the email.
    assert kwargs["include_local_variables"] is False
    assert kwargs["before_send"] is obs._scrub_event  # SQL echo scrubbing
    logging_integrations = [
        i for i in kwargs["integrations"] if type(i).__name__ == "LoggingIntegration"
    ]
    assert len(logging_integrations) == 1


def test_logging_integration_event_capture_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """event_level=None: a logger.error() in a fetcher must not become an event.

    Source fetchers catch-and-degrade their errors and log them; those logs
    must stay breadcrumbs only, so the integration installs NO event handler.
    """
    import config.settings as cs

    monkeypatch.setenv("SENTRY_DSN", "https://key@o0.ingest.sentry.io/0")
    cs.get_settings.cache_clear()
    recorder = _InitRecorder()
    monkeypatch.setattr(sentry_sdk, "init", recorder)
    obs.init_sentry(cs.get_settings())
    integration = next(
        i for i in recorder.calls[0]["integrations"] if type(i).__name__ == "LoggingIntegration"
    )
    assert integration._handler is None  # no event handler at all
    assert integration._breadcrumb_handler is not None  # breadcrumbs stay
    assert integration._breadcrumb_handler.level == logging.INFO


# ---------- API lifespan ----------


def test_lifespan_calls_init_sentry_through_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booting the API without a DSN never touches sentry_sdk.init (zero behavior change)."""
    from fastapi.testclient import TestClient

    from regwatch.api.main import app

    recorder = _InitRecorder()
    monkeypatch.setattr(sentry_sdk, "init", recorder)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert recorder.calls == []


def test_lifespan_inits_sentry_when_dsn_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs
    from fastapi.testclient import TestClient

    from regwatch.api.main import app

    monkeypatch.setenv("SENTRY_DSN", "https://key@o0.ingest.sentry.io/0")
    cs.get_settings.cache_clear()
    recorder = _InitRecorder()
    monkeypatch.setattr(sentry_sdk, "init", recorder)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["dsn"] == "https://key@o0.ingest.sentry.io/0"


# ---------- before_send scrubbing ----------


def test_scrub_event_truncates_sqlalchemy_statement_echo() -> None:
    """str(StatementError) embeds the SQL + a parameters preview (row payloads);
    the scrubber must cut both while keeping the error class/driver message."""
    event: Any = {
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": (
                        "(sqlite3.IntegrityError) UNIQUE constraint failed\n"
                        "[SQL: INSERT INTO ob_product (appl_no, trade_name) VALUES (?, ?)]\n"
                        "[parameters: ('020503', 'PROVENTIL HFA')]"
                    ),
                }
            ]
        }
    }
    scrubbed = obs._scrub_event(event, {})
    value = scrubbed["exception"]["values"][0]["value"]
    assert "INSERT INTO" not in value
    assert "PROVENTIL" not in value
    assert "020503" not in value
    assert "[parameters:" not in value
    assert value == "(sqlite3.IntegrityError) UNIQUE constraint failed [SQL: scrubbed]"


def test_scrub_event_truncates_provider_error_body() -> None:
    """The OpenAI-compatible client renders APIStatusError as
    ``Error code: <status> - <response body>``. A provider that echoes any part
    of the offending request back would otherwise put the analyst question and
    the retrieved passages into the event value — a third egress on the same
    hot path as the two the D1 work is closing."""
    event: Any = {
        "exception": {
            "values": [
                {
                    "type": "BadRequestError",
                    "value": (
                        "Error code: 400 - {'error': {'message': \"invalid input: "
                        "'what dissolution method does the ALBUTEROL PSG require'\"}}"
                    ),
                }
            ]
        }
    }
    scrubbed = obs._scrub_event(event, {})
    value = scrubbed["exception"]["values"][0]["value"]
    assert "ALBUTEROL" not in value
    assert "dissolution" not in value
    assert value == "Error code: scrubbed"


def test_scrub_event_cuts_both_markers_in_one_value() -> None:
    """A driver echo nested inside a provider error must lose both payloads."""
    event: Any = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": (
                        "wrapped\nError code: 400 - {'echo': 'ALBUTEROL'}\n"
                        "[SQL: INSERT INTO query_log (query_text) VALUES (?)]"
                    ),
                }
            ]
        }
    }
    value = obs._scrub_event(event, {})["exception"]["values"][0]["value"]
    assert "INSERT INTO" not in value
    assert "ALBUTEROL" not in value
    assert value == "wrapped Error code: scrubbed"


def test_scrub_event_leaves_non_sql_values_untouched() -> None:
    event: Any = {"exception": {"values": [{"type": "RuntimeError", "value": "boom"}]}}
    assert obs._scrub_event(event, {}) == event
    assert event["exception"]["values"][0]["value"] == "boom"


def test_scrub_event_tolerates_events_without_exceptions() -> None:
    event: Any = {"message": "hello"}
    assert obs._scrub_event(event, {}) == {"message": "hello"}


# ---------- capture_exception gating ----------


def test_capture_exception_noop_when_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[BaseException] = []
    monkeypatch.setattr(sentry_sdk, "is_initialized", lambda: False)
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)
    obs.capture_exception(RuntimeError("nope"))
    assert captured == []


def test_capture_exception_forwards_when_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[BaseException] = []
    monkeypatch.setattr(sentry_sdk, "is_initialized", lambda: True)
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)
    exc = RuntimeError("boom")
    obs.capture_exception(exc)
    assert captured == [exc]


# ---------- explicit capture point: populator persistence failure ----------


def test_populator_persist_failure_hits_capture_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """_persist degrades gracefully AND routes a SANITIZED exception to the
    capture point: str() on a SQLAlchemy persistence failure embeds the failed
    SQL plus the snapshot rows being inserted, none of which may reach Sentry."""
    from regwatch.whitepaper import populator as pop

    captured: list[BaseException] = []
    monkeypatch.setattr(obs, "capture_exception", captured.append)

    def _boom(**_kw: Any) -> None:
        raise RuntimeError(
            "db down [SQL: INSERT INTO ob_product ...] [parameters: ('020503', 'PROVENTIL')]"
        )

    monkeypatch.setattr(pop, "persist_whitepaper_snapshot", _boom)
    from datetime import UTC, datetime

    ctx = pop._Ctx(
        rld_name="Proventil",
        application_number_input="NDA020503",
        appl_no="020503",
        application_type="NDA",
        ingredient="ALBUTEROL SULFATE",
        normalized_name="albuterol sulfate",
        now=datetime.now(UTC),
        user_id="1",
        ob_failed=True,  # skip the OB snapshot assembly — persistence still runs
    )
    pop._persist(ctx)
    assert any("Persistence write-through failed" in w for w in ctx.warnings)
    assert len(captured) == 1
    sanitized = captured[0]
    assert isinstance(sanitized, RuntimeError)
    # Class name only — never the original message (SQL echo, row payloads).
    assert str(sanitized) == "whitepaper persistence failed: RuntimeError"
    assert "020503" not in str(sanitized)
    # No chain for Sentry to walk back to the raw exception.
    assert sanitized.__cause__ is None
    assert sanitized.__context__ is None or sanitized.__suppress_context__
