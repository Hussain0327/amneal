"""Shared test fixtures.

Postgres-only since R5 (docs/POLYGLOT_TARGET_2026-07-10.md): the suite runs
against the SAME datastore production runs -- Postgres + pgvector -- via a
DISPOSABLE database named by TEST_DATABASE_URL. There is no SQLite/Chroma
fallback anymore; without TEST_DATABASE_URL the run fails loudly (never
skip-green: a silently-skipped suite reads as passing).

Isolation model: the schema is bootstrapped once (init_db: create_all + stamp
head + pgvector DDL + RLS) and every test starts with one
``TRUNCATE ... RESTART IDENTITY CASCADE`` over all public tables except
alembic_version (~33ms). Tests that deliberately wreck the schema (the
bootstrap suite drops it, stamps wrong revisions, ...) are self-healing for
their neighbors: each test first checks the alembic stamp and rebuilds the
schema from scratch only when it is missing or wrong.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from regwatch.common import ratelimit
from regwatch.store import db as db_module
from regwatch.store import vector_store as vs_module

DEFAULT_USER_EMAIL = "analyst@example.com"
DEFAULT_USER_PASSWORD = "correct-horse-battery-staple"


def synth_turn_json(
    claims: list[tuple[str, list[tuple[str, int]]]] | None = None,
    *,
    turn_type: str = "ANSWER",
    unsupported: tuple[str, ...] = (),
) -> str:
    """The JSON string a stubbed synthesizer should return.

    ``claims`` is [(sentence, [(short_name, page), ...]), ...]. Keeping this in
    one place means a schema change is one edit rather than fifteen, and every
    stub exercises the SAME contract the real provider is held to.
    """
    import json as _json

    return _json.dumps(
        {
            "turn_type": turn_type,
            "claims": [
                {
                    "text": text,
                    "cites": [{"short_name": s, "page": p} for s, p in cites],
                }
                for text, cites in (claims or [])
            ],
            "unsupported": list(unsupported),
        }
    )


_TEST_DB_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
# Hosts we accept as "definitely a disposable database". The host .env carries
# the LIVE production Lakebase URL in DATABASE_URL; this guard makes it
# structurally impossible for the suite (which drops schemas and truncates
# every table) to reach anything remote even if an operator exports the wrong
# variable.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def pytest_configure(config: pytest.Config) -> None:
    if not _TEST_DB_URL:
        raise pytest.UsageError(
            "TEST_DATABASE_URL is not set. Since R5 the suite is Postgres-only: "
            "point TEST_DATABASE_URL at a DISPOSABLE local Postgres database "
            "with the pgvector extension available, e.g.\n"
            "  TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5499/regwatch_py_test uv run pytest\n"
            "(CI provides one via the pgvector service container; locally use "
            "Postgres.app / docker. The database's contents are DESTROYED.)"
        )
    url = make_url(_TEST_DB_URL)
    host = (url.host or "").strip("[]").lower()
    if host not in _LOCAL_HOSTS:
        raise pytest.UsageError(
            f"TEST_DATABASE_URL host {host!r} is not local ({sorted(_LOCAL_HOSTS)}). "
            "The suite DROPS SCHEMAS and TRUNCATES every table -- refusing to "
            "run against anything that could be a real database."
        )
    # libpq/psycopg gives ?host=/?hostaddr= query params precedence over the
    # netloc, so a crafted local-netloc URL would pass the host check above yet
    # dial a remote server. Reject those keys outright (same guard, same
    # reason, in tests_contract/conftest.py -- keep the two in sync).
    if any(key.lower() in ("host", "hostaddr") for key in url.query):
        raise pytest.UsageError(
            "TEST_DATABASE_URL must not carry 'host' or 'hostaddr' query "
            "parameters: libpq gives them precedence over the URL's netloc, "
            "which would bypass the local-host guard."
        )


def create_user(
    email: str = DEFAULT_USER_EMAIL,
    password: str = DEFAULT_USER_PASSWORD,
    *,
    display_name: str = "Test Analyst",
    role: str = "analyst",
    is_active: bool = True,
) -> int:
    """Insert a user directly (the CLI path is covered by its own tests)."""
    from regwatch.auth.passwords import hash_password
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import User

    init_db()
    with session_scope() as s:
        row = User(
            email=email.lower(),
            password_hash=hash_password(password),
            display_name=display_name,
            role=role,
            is_active=is_active,
        )
        s.add(row)
        s.flush()
        assert row.id is not None
        return row.id


class AuthedClient(TestClient):
    """An authenticated TestClient; user_id replaces the old GET /auth/me reads."""

    user_id: int


def session_client(user_id: int) -> AuthedClient:
    """An authenticated client whose session row is minted directly -- no HTTP.

    POST /auth/login lives in the Go proxy since the step-4 cutover
    (go/internal/api/auth.go); this Python app only VERIFIES cookies. The
    cookie set here is a real regwatch_session token minted by
    sessions.create_session -- the shared scheme both runtimes speak -- so
    require_user -> resolve_token runs for real on every request to the
    surviving protected router. No dependency_overrides, ever: that would be
    test theater.
    """
    from regwatch.api.main import app
    from regwatch.auth.deps import SESSION_COOKIE
    from regwatch.auth.sessions import create_session

    client = AuthedClient(app)
    client.__enter__()  # lifespan -> init_db (steady-state re-verify, ~13ms)
    raw, _ = create_session(user_id)
    client.cookies.set(SESSION_COOKIE, raw)
    client.user_id = user_id
    return client


@pytest.fixture
def auth_client() -> Iterator[AuthedClient]:
    """An authenticated client for a freshly created default user."""
    client = session_client(create_user())
    yield client
    client.__exit__(None, None, None)


_head_revision_cache: str | None = None


def _expected_head() -> str:
    global _head_revision_cache
    if _head_revision_cache is None:
        from alembic.script import ScriptDirectory

        _head_revision_cache = ScriptDirectory.from_config(
            db_module._alembic_config()
        ).get_current_head()
        assert _head_revision_cache is not None
    return _head_revision_cache


def _reset_database() -> None:
    """Start the test from a clean, current schema -- cheaply when possible.

    Fast path (~35ms): the schema is stamped at head, so one TRUNCATE over
    every public table except alembic_version resets all data and sequences.
    Slow path (~700ms, first test of the session and after any test that
    wrecked the schema): DROP SCHEMA + full init_db bootstrap.
    """
    engine = db_module.get_engine()
    stamped = None
    try:
        with engine.connect() as conn:
            stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        stamped = None
    if stamped != _expected_head():
        db_module.reset_for_tests()
        engine = db_module.get_engine()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        db_module.init_db()
        return
    with engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            joined = ", ".join(f'public."{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Network-free providers by default. Echo's 1536-dim output matches the
    # pgvector chunk column, so ingest/query tests embed for real.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    monkeypatch.setenv("LLM_PROVIDER", "echo")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    monkeypatch.setenv("EMBEDDING_SHADOW_PROFILE", "")
    # Rate limiting off by default; rate-limit tests opt in explicitly.
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    # The API fail-fast guard rejects echo providers over a non-empty corpus;
    # tests run echo against seeded corpora on purpose, so opt in explicitly.
    monkeypatch.setenv("REGWATCH_ALLOW_TEST_PROVIDERS", "1")
    # Pydantic-settings would otherwise load real keys from `.env`; clear them
    # so tests run from a clean slate regardless of the host's .env.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENFDA_API_KEY", "")
    monkeypatch.setenv("QWEN_EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("QWEN_EMBEDDING_TOKEN", "")
    monkeypatch.setenv("DATABRICKS_LLM_BASE_URL", "")
    monkeypatch.setenv("DATABRICKS_LLM_TOKEN", "")
    # Operator tuning knobs with default-value assertions in the suite. A dev
    # who exports these mid-incident (exactly what .env.example suggests) must
    # not see local-only failures CI can't reproduce.
    monkeypatch.delenv("DATABRICKS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("SYNTHESIZER_MAX_TOKENS", raising=False)
    # Sentry stays OFF in tests even if the host .env carries a DSN, and pinned
    # to the default environment so a host .env (SENTRY_ENVIRONMENT=development/
    # production) can't leak into the "default off" assertions.
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "dev")
    # The one and only datastore: the disposable TEST_DATABASE_URL database.
    # Setting DATABASE_URL explicitly (rather than clearing it) means a host
    # .env pointing at production Postgres can never leak into the suite.
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    # Prod (Lakebase) sessions run in UTC; a scratch Postgres initdb'd on a
    # laptop defaults to the LOCAL timezone, which would shift every
    # aware-datetime written into the naive timestamp columns. PGTZ pins the
    # libpq session timezone so both environments store identical values.
    monkeypatch.setenv("PGTZ", "UTC")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAW_PDF_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("PROCESSED_DIR", str(tmp_path / "processed"))
    # Force settings to re-resolve for this test's env.
    import config.settings as cs

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    # The engine and init_db memo stay WARM across tests (same database) --
    # _reset_database() rebuilds only when a previous test wrecked the schema.
    _reset_database()
    vs_module.reset_for_tests()
    # The in-memory query limiter is process-global; clear it so one test's
    # traffic cannot 429 the next test. (The login limiter moved to the Go
    # proxy with the step-4 auth cutover.)
    ratelimit.reset_for_tests()
    yield
    vs_module.reset_for_tests()


@pytest.fixture
def cleared_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fixture to wipe optional API keys so tests can assert provider errors."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENFDA_API_KEY"):
        if k in os.environ:
            monkeypatch.delenv(k, raising=False)
