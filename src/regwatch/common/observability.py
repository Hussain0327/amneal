"""Sentry wiring (H1) — OFF unless SENTRY_DSN is set; zero behavior change otherwise.

Design notes:
- Init happens in the API lifespan only (api/main.py), never at import time.
- ``send_default_pii=False`` and ``max_request_body_size="never"``: no request
  bodies and no question text ever reach Sentry. ``query_text`` stays in our
  own audit log (``query_log``), which is the system of record (INV-6).
- ``include_local_variables=False``: the SDK default (True) serializes every
  traceback frame's locals into unhandled-exception events — frames in the
  /query path hold ``question``/``answer``/``user_prompt`` (which embeds the
  retrieved passage texts), so locals
  capture would defeat the body scrubbing above the moment anything 500s.
- ``before_send=_scrub_event``: defense-in-depth against SQLAlchemy exception
  messages — ``str(StatementError)`` embeds the failed SQL statement plus a
  parameters preview (the row payloads being written), which would otherwise
  ship as the event's exception value.
- ``LoggingIntegration(event_level=None)``: source fetchers already
  catch-and-degrade their errors and log them; logged errors must NOT become
  Sentry events. Only unhandled exceptions and the explicit capture points
  (populator persistence failure, migration-mode mismatches) reach Sentry.
"""

from __future__ import annotations

import logging

import sentry_sdk
from config.settings import Settings
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.types import Event, Hint


def _scrub_event(event: Event, _hint: Hint) -> Event:
    """Truncate SQLAlchemy's statement/parameters echo out of exception values.

    ``str(StatementError)`` (and subclasses like ``IntegrityError``) embeds the
    failed SQL plus a parameters preview — i.e. the very rows being written
    (whitepaper provenance, user-supplied input). Cutting at the ``[SQL:``
    marker keeps the error class and driver message while dropping the data;
    the ``[parameters: …]`` block always follows the SQL block, so one cut
    removes both.
    """
    # None-safe: some message/transaction events carry "exception": None, where
    # event.get("exception", {}) returns None and .get(...) would raise — which
    # the SDK swallows by DROPPING the event, silently defeating this scrubber.
    for exc in (event.get("exception") or {}).get("values") or []:
        value = exc.get("value")
        if isinstance(value, str) and "[SQL:" in value:
            exc["value"] = value.split("[SQL:", 1)[0].rstrip() + " [SQL: scrubbed]"
    return event


def init_sentry(s: Settings) -> bool:
    """Initialize Sentry iff a DSN is configured. Returns True when enabled."""
    if not s.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=s.sentry_dsn,
        environment=s.sentry_environment,
        traces_sample_rate=0.1,
        profiles_sample_rate=0.0,  # profiling off
        send_default_pii=False,
        max_request_body_size="never",  # never attach request bodies (question text)
        # Never serialize stack-frame locals into events: /query frames hold the
        # question, the answer, and the prompt (with retrieved passage texts);
        # /auth frames hold the email. The default EventScrubber denylist covers
        # none of those names, so locals stay off wholesale.
        include_local_variables=False,
        before_send=_scrub_event,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            # Logged errors stay breadcrumbs only — never standalone events.
            LoggingIntegration(level=logging.INFO, event_level=None),
        ],
    )
    return True


def capture_exception(exc: BaseException) -> None:
    """Explicit capture point — a no-op unless Sentry was initialized."""
    if sentry_sdk.is_initialized():
        sentry_sdk.capture_exception(exc)
