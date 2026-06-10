"""Shared test fixtures.

We force a per-test temp data directory so that SQLite, Chroma, and raw PDFs
are isolated. We default LLM_PROVIDER=echo and EMBEDDING_PROVIDER=echo so
tests run with no network and no model downloads.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regwatch.common import ratelimit
from regwatch.store import db as db_module
from regwatch.store import vector_store as vs_module

DEFAULT_USER_EMAIL = "analyst@example.com"
DEFAULT_USER_PASSWORD = "correct-horse-battery-staple"


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


def login_client(
    email: str = DEFAULT_USER_EMAIL, password: str = DEFAULT_USER_PASSWORD
) -> TestClient:
    """A TestClient logged in through POST /auth/login (httpx keeps the cookie)."""
    from regwatch.api.main import app

    client = TestClient(app)
    client.__enter__()  # lifespan → init_db on the per-test DB
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def auth_client() -> Iterator[TestClient]:
    """An authenticated TestClient for a freshly created default user."""
    create_user()
    client = login_client()
    yield client
    client.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Network-free providers by default.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    monkeypatch.setenv("LLM_PROVIDER", "echo")
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
    # Per-test storage.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "regwatch.db"))
    monkeypatch.setenv("RAW_PDF_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("PROCESSED_DIR", str(tmp_path / "processed"))
    # Force settings + storage modules to re-init for the test.
    import config.settings as cs

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    vs_module.reset_for_tests()
    # The in-memory rate limiters are process-global; clear them so one test's
    # logins cannot 429 the next test (the login guard is always on).
    ratelimit.reset_for_tests()
    # Also clear any cached env that pydantic-settings stashed in process.
    yield
    # Cleanup (Chroma keeps a file lock; reset clears it).
    vs_module.reset_for_tests()
    db_module.reset_for_tests()


@pytest.fixture
def cleared_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fixture to wipe optional API keys so tests can assert provider errors."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENFDA_API_KEY"):
        if k in os.environ:
            monkeypatch.delenv(k, raising=False)
