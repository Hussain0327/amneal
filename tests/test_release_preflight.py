"""The Fly release phase must migrate before it asserts serving readiness."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.runtime.migration import MigrationContext
from typer.testing import CliRunner


def test_prepare_release_database_migrates_before_serving_guard(
    monkeypatch: Any,
) -> None:
    from alembic import command

    from regwatch.store import db

    calls: list[tuple[str, object | None]] = []
    config = object()

    monkeypatch.setattr(db, "_alembic_config", lambda: config)
    monkeypatch.setattr(
        command,
        "upgrade",
        lambda received, revision: calls.append(("upgrade", (received, revision))),
    )
    monkeypatch.setattr(db, "init_db", lambda: calls.append(("init_db", None)))

    db.prepare_release_database()

    assert calls == [
        ("upgrade", (config, "head")),
        ("init_db", None),
    ]


def test_prepare_release_database_propagates_serving_guard_failure(
    monkeypatch: Any,
) -> None:
    from alembic import command

    from regwatch.store import db

    def fail_serving_guard() -> None:
        raise RuntimeError("no ready HNSW index")

    monkeypatch.setattr(db, "_alembic_config", object)
    monkeypatch.setattr(command, "upgrade", lambda _config, _revision: None)
    monkeypatch.setattr(db, "init_db", fail_serving_guard)

    with pytest.raises(RuntimeError, match="no ready HNSW index"):
        db.prepare_release_database()


def test_release_cli_invokes_release_preflight(monkeypatch: Any) -> None:
    from regwatch import cli
    from regwatch.store import db

    calls: list[str] = []
    monkeypatch.setattr(db, "prepare_release_database", lambda: calls.append("release_preflight"))

    result = CliRunner().invoke(cli.app, ["release"])

    assert result.exit_code == 0, result.stdout
    assert calls == ["release_preflight"]
    assert "release database and serving profile ready" in result.stdout


def test_prepare_release_database_advances_a_real_behind_schema() -> None:
    """A behind stamp is migrated before the normal cold-boot guard sees it."""
    from alembic import command

    from regwatch.store import db

    cfg = db._alembic_config()
    command.downgrade(cfg, "0024_fda_streaming_lifecycle")
    with db.get_engine().connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == (
            "0024_fda_streaming_lifecycle"
        )

    # Simulate the fresh one-off release process: it has no warm schema/provider
    # memo from the process that prepared this disposable test database.
    db.reset_for_tests()
    db.prepare_release_database()

    with db.get_engine().connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == db._head_revision(
            db._alembic_config()
        )
