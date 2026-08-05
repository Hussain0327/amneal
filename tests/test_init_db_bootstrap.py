"""The bootstrap path through init_db: schema without the K6 provider assert.

`embedding-profile-register` and `embedding-profile-index` necessarily run
BEFORE a serving-ready embedding provider can exist -- one mints the profile id,
the other builds the HNSW index that activation-readiness requires. Both call
init_db purely to get the schema; neither embeds or searches.

Before init_db grew `assert_provider`, both were unreachable in a default
environment: with EMBEDDING_PROVIDER unset the provider is local-bge-small
(384-dim) and the legacy vector(1536) branch of assert_embedding_provider_dim
fires, and with EMBEDDING_PROVIDER=qwen3 the "Qwen3 cannot write into the legacy
space" branch fires instead, because ACTIVE_EMBEDDING_PROFILE cannot yet name a
profile that has not been registered.

This went unnoticed because every real-DB fixture sets EMBEDDING_PROVIDER=openai
to get past that same assert (tests/test_embedding_profiles.py), and because the
CI eval steps that call these commands had never once executed.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from regwatch.cli import app

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

runner = CliRunner()

# ep_ + sha256[:32]; the register command's --id-only output must be exactly this
# and nothing else, because CI captures it with PROFILE_ID=$(...).
_PROFILE_ID_RE = re.compile(r"^ep_[0-9a-f]{32}$")


def test_bootstrap_skips_the_assert_without_suppressing_it_for_later_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memoization hole the two-flag split exists to close.

    A single `_initialized` flag would let one `assert_provider=False` call mark
    the process initialized, so a later serving-path `init_db()` would return
    early and never assert -- converting a startup failure into a wrong-vectors
    failure at query time. Schema work must still happen exactly once.
    """
    from regwatch.store import db as db_module
    from regwatch.store import pgvector_store

    asserts: list[int] = []
    schema: list[int] = []

    monkeypatch.setattr(db_module, "get_engine", lambda: object())
    monkeypatch.setattr(db_module, "_init_postgres", lambda _engine: schema.append(1))
    monkeypatch.setattr(
        pgvector_store,
        "assert_embedding_provider_dim",
        lambda: asserts.append(1),
    )

    db_module._schema_ready = False
    db_module._provider_asserted = False
    try:
        db_module.init_db(assert_provider=False)
        assert asserts == [], "bootstrap call must not run the serving-path assert"
        assert schema == [1], "bootstrap call must still apply the schema"

        # Same process, serving path. This is the regression that matters.
        db_module.init_db()
        assert asserts == [1], "a later serving-path caller must still be asserted"
        assert schema == [1], "schema work must not be repeated"

        # And the assert itself stays memoized after it has run once.
        db_module.init_db()
        assert asserts == [1]
    finally:
        db_module._schema_ready = False
        db_module._provider_asserted = False


def test_bootstrap_call_is_memoized_across_repeat_bootstrap_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from regwatch.store import db as db_module

    schema: list[int] = []
    monkeypatch.setattr(db_module, "get_engine", lambda: object())
    monkeypatch.setattr(db_module, "_init_postgres", lambda _engine: schema.append(1))

    db_module._schema_ready = False
    db_module._provider_asserted = False
    try:
        db_module.init_db(assert_provider=False)
        db_module.init_db(assert_provider=False)
        assert schema == [1], "repeat bootstrap calls must not redo the schema work"
    finally:
        db_module._schema_ready = False
        db_module._provider_asserted = False


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_register_succeeds_with_the_default_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact CI condition, end to end through the CLI.

    local-bge-small (384-dim) against the legacy vector(1536) column with no
    active profile is what a credential-free CI runner has, and what made
    `prepare databricks eval arm` exit 1 on its first-ever execution.

    Both values are set EXPLICITLY rather than deleted. pydantic-settings reads
    the repo `.env` as well as the process environment, so monkeypatch.delenv
    would leave a developer's `.env` (EMBEDDING_PROVIDER=openai,
    ACTIVE_EMBEDDING_PROFILE=ep_...) supplying the values and the test would
    silently exercise a different branch of the same guard locally than it does
    in CI. setenv wins over the file; delenv does not.
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-bge-small")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()

    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    try:
        result = runner.invoke(
            app,
            [
                "embedding-profile-register",
                "--serving-runtime-version",
                "databricks-model-service-2026-07-29",
                "--id-only",
            ],
        )
        assert result.exit_code == 0, result.output
        printed = result.stdout.strip()
        assert _PROFILE_ID_RE.match(printed), f"unparseable --id-only output: {printed!r}"

        # Content-addressed and idempotent: CI re-runs this on every build, so a
        # second call must return the same id and not fail on a duplicate write.
        again = runner.invoke(
            app,
            [
                "embedding-profile-register",
                "--serving-runtime-version",
                "databricks-model-service-2026-07-29",
                "--id-only",
            ],
        )
        assert again.exit_code == 0, again.output
        assert again.stdout.strip() == printed
    finally:
        cs.get_settings.cache_clear()
        db.reset_for_tests()
        pgvector_store.reset_for_tests()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_serving_path_still_refuses_a_mismatched_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The K6 guard must survive this change on the path it actually protects.

    A fix that made the bootstrap work by weakening the startup check would pass
    every test above while removing the thing that stops a 384-dim provider from
    writing into a vector(1536) column.
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-bge-small")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="384-dim"):
            db.init_db()
    finally:
        cs.get_settings.cache_clear()
        db.reset_for_tests()
        pgvector_store.reset_for_tests()
