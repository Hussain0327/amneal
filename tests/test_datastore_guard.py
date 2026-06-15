"""B1: production must refuse to boot on the SQLite fallback.

A missing/typo'd DATABASE_URL with REQUIRE_DATABASE_URL set would otherwise
silently run on the container's ephemeral disk and lose every user, session,
and query_log audit row (INV-6 evidence) on the next machine recycle — with
/health staying green throughout. The guard turns that into a loud refusal,
and /health exposes the active dialect so a misconfigured prod stack is visible.
"""

from __future__ import annotations

import config.settings as cs
import pytest
from fastapi.testclient import TestClient

from regwatch.store import db as db_module


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()


def test_require_database_url_refuses_sqlite_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest already clears DATABASE_URL; turn the production guard on.
    _reload(monkeypatch, REQUIRE_DATABASE_URL="1", DATABASE_URL="")
    with pytest.raises(RuntimeError, match="REQUIRE_DATABASE_URL"):
        db_module.get_engine()


def test_dev_default_still_uses_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the guard (the dev/test default), SQLite remains the backend.
    _reload(monkeypatch, REQUIRE_DATABASE_URL="0", DATABASE_URL="")
    assert db_module.engine_dialect() == "sqlite"


def test_health_exposes_db_dialect() -> None:
    from regwatch.api.main import app

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["components"]["db"]["dialect"] == "sqlite"
