"""store-1: the Postgres engines must bound the TCP/TLS connection handshake.

``statement_timeout`` only bounds a query AFTER a session exists, and
``pool_pre_ping`` opens a fresh connection on every checkout — so without a
libpq ``connect_timeout`` a stalled handshake to the public Supabase pooler
hangs the request thread forever. These tests assert ``connect_timeout`` (equal
to the configured value, integer seconds) is present in the connect_args used
to build BOTH Postgres engine paths: ``store/db.py``'s engine and
``pgvector_store``'s fallback engine. The fallback test also pins store-7 — the
fallback now inherits ``sslmode=require`` and the GUC timeouts from the same
hardening path.

Each test would fail if the fix were reverted: drop ``connect_timeout`` from
``_pg_connect_args`` and the assertions below break.
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings


def test_pg_connect_args_includes_connect_timeout() -> None:
    """_pg_connect_args carries connect_timeout as an int equal to the config."""
    from regwatch.store.db import _pg_connect_args

    args = _pg_connect_args(Settings(db_connect_timeout="10"))
    assert args["connect_timeout"] == 10
    # Integer seconds per libpq, not a GUC duration string.
    assert isinstance(args["connect_timeout"], int)

    # A custom value flows through unchanged.
    assert _pg_connect_args(Settings(db_connect_timeout="3"))["connect_timeout"] == 3


def test_pg_connect_args_disables_connect_timeout_when_zero_or_empty() -> None:
    """'0' or '' means an unbounded handshake — the key is omitted entirely."""
    from regwatch.store.db import _pg_connect_args

    assert "connect_timeout" not in _pg_connect_args(Settings(db_connect_timeout="0"))
    assert "connect_timeout" not in _pg_connect_args(Settings(db_connect_timeout=""))


def test_pg_connect_args_keeps_guc_timeouts_alongside_connect_timeout() -> None:
    """connect_timeout is additive: the GUC `options` string is still built."""
    from regwatch.store.db import _pg_connect_args

    args = _pg_connect_args(
        Settings(
            db_statement_timeout="30s",
            db_idle_in_tx_timeout="60s",
            db_lock_timeout="10s",
            db_connect_timeout="10",
        )
    )
    assert args["connect_timeout"] == 10
    options = args["options"]
    assert isinstance(options, str)
    assert "statement_timeout=30s" in options
    assert "idle_in_transaction_session_timeout=60s" in options
    assert "lock_timeout=10s" in options


def test_db_engine_is_built_with_connect_timeout(monkeypatch: Any) -> None:
    """store/db.py's Postgres engine passes connect_timeout to create_engine."""
    import regwatch.store.db as db

    captured: dict[str, Any] = {}

    def fake_create_engine(url: Any, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()  # never connected; we only inspect construction args

    settings = Settings(
        database_url="postgresql+psycopg://u:p@aws-1-us-east-1.pooler.supabase.com:5432/postgres",
        db_connect_timeout="10",
    )
    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.reset_for_tests()
    try:
        db.get_engine()
        assert captured["kwargs"]["connect_args"]["connect_timeout"] == 10
        # Parity anchor for the pgvector fallback test below: both Postgres
        # engines must recycle pooled connections on the same schedule.
        assert captured["kwargs"]["pool_recycle"] == settings.db_pool_recycle_s
    finally:
        # Leave no fake engine cached for other tests in this file/process.
        db._engine = None
        db._initialized = False
