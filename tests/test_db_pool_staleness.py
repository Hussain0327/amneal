"""The pool's liveness check is idle-gated, not paid on every checkout.

``pool_pre_ping`` round-tripped the server on EVERY pool checkout -- 24 of one
traced ask turn's 61 round trips, spent proving that a connection returned to
the pool milliseconds earlier was still alive. ``_register_pool_idle_ping``
replaces it with a checkout listener that pings only after a real idle gap
(``DB_POOL_IDLE_PING_S``, default 30s), which is the policy pgx already runs by
default against this same endpoint on the Go edge.

These tests pin what that swap buys and what must survive it:

* warm checkouts inside one request cost NO ping (the savings are real),
* a connection killed while parked is replaced transparently at checkout, and
  the caller's scope runs exactly ONCE -- never replayed,
* a connection killed inside the un-pinged window fails exactly once with
  ``connection_invalidated`` set, and the pool self-heals on the next request,
* a broken ping degrades into that same reconnect instead of escaping raw,
* the pool is constructed without ``pool_pre_ping`` and with keepalives.

They need a real Postgres, which conftest guarantees via TEST_DATABASE_URL
(host-restricted to localhost). Tests that change the gate rebuild the process
engine and MUST restore it in a finally, because conftest keeps the engine warm
across tests.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import config.settings as cs
import pytest
from config.settings import Settings
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

import regwatch.store.db as db

# A throwaway table used to prove the caller's scope executed exactly once.
# Spelled as literals (not f-strings) at every use site so flake8-bandit's
# hardcoded-SQL rule stays honest about the rest of the suite.
_SCRATCH_TABLE = "rw_pool_staleness_scratch"


@contextmanager
def _rebuilt_engine(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[Engine]:
    """Rebuilds the process engine with `env` applied, then restores it.

    conftest keeps one warm engine for the whole session, so a test that needs a
    different pool configuration has to drop and rebuild it -- and must put the
    reset in a finally, or every later test inherits this test's engine.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        **env: Environment variables to apply while the engine is rebuilt.

    Yields:
        The freshly built engine.
    """
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db.reset_for_tests()
    try:
        yield db.get_engine()
    finally:
        for name in env:
            monkeypatch.delenv(name, raising=False)
        cs.get_settings.cache_clear()
        cs.settings = cs.get_settings()
        db.reset_for_tests()


def _spy_on_ping(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts checkout pings without changing what they do.

    The ping is issued against the raw DBAPI connection, so it is invisible to
    SQLAlchemy's cursor events; spying on our own helper is what makes the gate's
    DECISION observable. It proves the decision, not the socket.

    Args:
        monkeypatch: The test's monkeypatch fixture.

    Returns:
        A list appended to once per ping. Its length is the ping count.
    """
    calls: list[int] = []
    real = db._ping_pooled_connection

    def spy(engine: Engine, dbapi_connection: Any) -> None:
        calls.append(1)
        real(engine, dbapi_connection)

    monkeypatch.setattr(db, "_ping_pooled_connection", spy)
    return calls


def _backend_pid(session: Any) -> int:
    """Returns the Postgres backend PID serving this session's connection."""
    return int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())


def _terminate_backend(pid: int) -> None:
    """Kills one Postgres backend from a throwaway connection.

    A backend cannot terminate the one it is speaking on, so this opens its own
    NullPool engine. pg_terminate_backend only SIGNALS, so this waits for the
    backend to actually disappear -- otherwise the next statement could still
    land on a live socket and the test would prove nothing.

    AUTOCOMMIT is load-bearing, not tidiness: Postgres snapshots the backend
    status array once per transaction (pgstat_read_current_status), so inside
    one open transaction every poll returns the SAME cached row. A first poll
    that races asynchronous SIGTERM delivery and sees the victim would then
    latch at 1 for the whole loop and this helper would fail a healthy kill.
    One transaction per poll means one fresh snapshot per poll.

    Args:
        pid: The backend PID to terminate.

    Raises:
        AssertionError: The backend was still alive after 10 seconds.
    """
    url = cs.get_settings().database_url
    assert url is not None, "conftest pins DATABASE_URL to the disposable test DB"
    killer = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with killer.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                still_there = conn.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                ).scalar_one()
                if not still_there:
                    return
                time.sleep(0.05)
        raise AssertionError(f"backend {pid} still alive 10s after pg_terminate_backend")
    finally:
        killer.dispose()


def test_warm_checkouts_do_not_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five back-to-back scopes reuse one connection and cost zero pings."""
    calls = _spy_on_ping(monkeypatch)
    pids: list[int] = []
    with _rebuilt_engine(monkeypatch, DB_POOL_IDLE_PING_S="30"):
        for _ in range(5):
            with db.session_scope() as session:
                pids.append(_backend_pid(session))

    assert calls == [], f"idle-gated ping fired {len(calls)}x on warm checkouts"
    # Identical PIDs prove the gate was actually exercised: the same pooled
    # connection really was handed back out, rather than a fresh one (which
    # would skip the ping trivially).
    assert len(set(pids)) == 1, f"expected one reused backend, got {pids}"


def test_checkin_stamp_means_in_use_time_is_not_counted_as_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle means PARKED, not in use: a slow scope does not arm the next ping."""
    calls = _spy_on_ping(monkeypatch)
    # A 1s gate with a scope held open past it. The checkin listener is the only
    # thing that resets the clock when the connection goes BACK to the pool; with
    # only the checkout-time fallback stamp, this scope's own duration would be
    # billed as idleness and the next checkout would ping for nothing.
    with _rebuilt_engine(monkeypatch, DB_POOL_IDLE_PING_S="1"):
        with db.session_scope() as session:
            _backend_pid(session)
            time.sleep(1.6)

        with db.session_scope() as session:
            _backend_pid(session)

    assert calls == [], "in-use time was billed as idle; the checkin stamp is missing"


def test_terminated_backend_is_replaced_at_checkout_without_replaying_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead parked connection is swapped at checkout; the scope runs once."""
    calls = _spy_on_ping(monkeypatch)
    # Gate wide open, so every checkout pings -- the DB_POOL_IDLE_PING_S=0
    # rollback setting, and the only way to exercise this path without waiting
    # 30 real seconds.
    with _rebuilt_engine(monkeypatch, DB_POOL_IDLE_PING_S="0"):
        try:
            with db.session_scope() as session:
                session.execute(text("CREATE TABLE rw_pool_staleness_scratch (n integer)"))
                first_pid = _backend_pid(session)

            _terminate_backend(first_pid)

            # No exception: the ping fails at checkout, the pool reconnects, and
            # the INSERT below runs on the replacement connection.
            with db.session_scope() as session:
                session.execute(text("INSERT INTO rw_pool_staleness_scratch (n) VALUES (1)"))
                second_pid = _backend_pid(session)

            assert second_pid != first_pid, "the dead backend was handed out again"
            assert calls, "no ping fired, so nothing detected the dead connection"

            with db.session_scope() as session:
                rows = session.execute(
                    text("SELECT count(*) FROM rw_pool_staleness_scratch")
                ).scalar_one()
            # The load-bearing assertion: the retry lives INSIDE checkout, before
            # the scope's first statement, so the INSERT executed exactly once.
            # A retry wrapped around session_scope would write 2 rows.
            assert rows == 1, f"the scope was replayed: {rows} rows"
        finally:
            with db.session_scope() as session:
                session.execute(text("DROP TABLE IF EXISTS rw_pool_staleness_scratch"))


def test_undetected_stale_connection_fails_once_then_self_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the un-pinged window the caller sees one invalidated error."""
    calls = _spy_on_ping(monkeypatch)
    with _rebuilt_engine(monkeypatch, DB_POOL_IDLE_PING_S="30"):
        with db.session_scope() as session:
            first_pid = _backend_pid(session)

        _terminate_backend(first_pid)

        # The gap is milliseconds, so the 30s gate stays shut and nothing pings:
        # the first statement is what discovers the dead socket.
        with pytest.raises(OperationalError) as caught, db.session_scope() as session:
            _backend_pid(session)

        assert caught.value.connection_invalidated is True
        # session_scope's rollback did not mask it: this is the original error,
        # still carrying the statement that hit the dead connection.
        assert "pg_backend_pid" in (caught.value.statement or "")

        # The pool's generation stamp caps the blast radius at the request that
        # was in flight -- the very next one succeeds on a fresh connection.
        with db.session_scope() as session:
            third_pid = _backend_pid(session)
        assert third_pid != first_pid

    assert calls == [], "the 30s gate must not have pinged inside this window"


def test_ping_failure_never_escapes_as_a_raw_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ping that raises something odd degrades into a reconnect, not a 500."""
    with _rebuilt_engine(monkeypatch, DB_POOL_IDLE_PING_S="0") as engine:
        with db.session_scope() as session:
            first_pid = _backend_pid(session)

        def boom(dbapi_connection: Any) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(engine.dialect, "do_ping", boom)

        # SQLAlchemy re-raises ANY non-DisconnectionError a checkout listener
        # throws straight at the caller, so a helper that let RuntimeError
        # through would break every checkout in the process.
        with db.session_scope() as session:
            second_pid = _backend_pid(session)
        assert second_pid != first_pid


def test_engine_pool_construction_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool is built without pre-ping, with LIFO reuse and keepalives."""
    captured: dict[str, Any] = {}

    def fake_create_engine(url: Any, **kwargs: Any) -> Engine:
        captured["kwargs"] = kwargs
        # A REAL (never connected) engine, not a sentinel: get_engine registers
        # pool event listeners on whatever create_engine hands back.
        return create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")

    settings = Settings(
        database_url="postgresql+psycopg://u:p@dbc-example.cloud.databricks.com:5432/regwatch",
    )
    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.reset_for_tests()
    try:
        db.get_engine()
        kwargs = captured["kwargs"]
        assert "pool_pre_ping" not in kwargs, "pre-ping is back; the idle gate is bypassed"
        assert kwargs["pool_use_lifo"] is True
        assert kwargs["pool_size"] == 5
        assert kwargs["max_overflow"] == 5
        # Lifetime bound, unchanged by the move off pre-ping.
        assert kwargs["pool_recycle"] == settings.db_pool_recycle_s == 1800
        connect_args = kwargs["connect_args"]
        assert connect_args["keepalives"] == 1
        assert connect_args["keepalives_idle"] == settings.db_keepalives_idle_s == 30
        assert connect_args["keepalives_interval"] == 10
        assert connect_args["keepalives_count"] == 3
    finally:
        db.reset_for_tests()


def test_keepalives_are_omitted_when_disabled() -> None:
    """DB_KEEPALIVES_IDLE_S=0 drops all four keywords, not just the idle one."""
    args = db._pg_connect_args(Settings(db_keepalives_idle_s=0))

    for key in ("keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count"):
        assert key not in args
    # The rest of the hardening is untouched by disabling keepalives.
    assert args["connect_timeout"] == 10
