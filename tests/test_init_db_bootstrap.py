"""The bootstrap path through init_db: schema without the K6 provider assert.

`embedding-profile-register` and `embedding-profile-index` necessarily run
BEFORE a serving-ready embedding provider can exist -- one mints the profile id,
the other builds the HNSW index that activation-readiness requires. Both call
init_db purely to get the schema; neither embeds or searches.

Before init_db grew `assert_provider`, both were unreachable in a default
environment: with EMBEDDING_PROVIDER unset the provider resolution refuses
outright (required-explicit since 2026-08-17; before that the silent
local-bge-small fallback failed the legacy vector(1536) dim branch), and with
EMBEDDING_PROVIDER=qwen3 the "Qwen3 cannot write into the legacy space" branch
fires instead, because ACTIVE_EMBEDDING_PROFILE cannot yet name a profile that
has not been registered.

This went unnoticed because every real-DB fixture sets a 1536-dim provider to
get past that same assert (tests/test_embedding_profiles.py), and because the
CI eval steps that call these commands had never once executed.

The same flag now covers the REPAIR commands too (embedding-profile-list,
-coverage, -backfill and authoritative-corpus-status, -embed). Postmortem #224:
on 2026-08-13 ACTIVE_EMBEDDING_PROFILE named an incomplete profile, so
assert_profile_ready_for_activation failed closed inside every command an
operator reached for while diagnosing it -- including the backfill that WAS the
fix. The three tests at the bottom of this file pin all sides of that: the
deadlock is real, the repair commands survive it, and the serving-path commands
still refuse.
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any

import pytest
from config.settings import get_settings
from sqlalchemy import text
from typer.testing import CliRunner

from regwatch.cli import app
from regwatch.store.embedding_profiles import EmbeddingProfileSpec

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

runner = CliRunner()

# ep_ + sha256[:32]; the register command's --id-only output must be exactly this
# and nothing else, because CI captures it with PROFILE_ID=$(...).
_PROFILE_ID_RE = re.compile(r"^ep_[0-9a-f]{32}$")

# The legacy chunk.embedding column is vector(1536); the seeded chunk only has
# to be storable, never searched.
_LEGACY_DIM = 1536


def _ci_spec() -> EmbeddingProfileSpec:
    """The spec CI registers, built from committed defaults."""
    from regwatch.process.chunker import CHUNKING_VERSION
    from regwatch.process.embedder import QWEN3_DOCUMENT_PREPROCESSING_VERSION

    settings = get_settings()
    return EmbeddingProfileSpec(
        provider="qwen3",
        model=settings.qwen_embedding_model,
        revision=settings.qwen_embedding_revision,
        dimension=settings.qwen_embedding_dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version=settings.qwen_embedding_query_instruction_version,
        preprocessing_version=QWEN3_DOCUMENT_PREPROCESSING_VERSION,
        chunking_version=CHUNKING_VERSION,
        serving_runtime_version="databricks-model-service-2026-07-29",
    )


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

    NO EMBEDDING_PROVIDER at all is what a credential-free CI runner has (the
    variable is required-explicit with no default), and an earlier form of this
    state is what made `prepare databricks eval arm` exit 1 on its first-ever
    execution. The bootstrap flag must keep these commands runnable there.

    Both values are set EXPLICITLY rather than deleted. pydantic-settings reads
    the repo `.env` as well as the process environment, so monkeypatch.delenv
    would leave a developer's `.env` (EMBEDDING_PROVIDER=qwen3,
    ACTIVE_EMBEDDING_PROFILE=ep_...) supplying the values and the test would
    silently exercise a different branch of the same guard locally than it does
    in CI. setenv wins over the file; delenv does not.
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
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
def test_empty_profile_with_a_built_index_is_activatable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant CI's step ordering rests on.

    `regwatch seed` runs with the profile ACTIVE, so its init_db() calls
    assert_profile_ready_for_activation -- which wants a valid HNSW index. CI
    therefore builds the index BEFORE seeding, against an empty corpus. That is
    only sound because coverage of 0/0 counts as complete
    (ProfileEmbeddingCoverage.complete is total == embedded).

    Tighten `complete` to require total > 0 and CI breaks in the seed step with
    an error naming coverage, three steps away from the change that caused it.
    This test fails at the actual cause instead.
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store
    from regwatch.store.embedding_profiles import assert_profile_ready_for_activation
    from regwatch.store.vector_store import ensure_profile_hnsw_index, register_embedding_profile

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()

    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    try:
        db.init_db(assert_provider=False)
        profile = register_embedding_profile(_ci_spec())
        # concurrently=False: a CONCURRENTLY build cannot run inside the
        # transaction this test's engine hands out, and the distinction is
        # irrelevant to what is being asserted.
        ensure_profile_hnsw_index(profile.profile_id, concurrently=False)

        # No chunks exist. This must still pass, or CI's index-before-seed
        # ordering is unsound.
        assert_profile_ready_for_activation(profile.profile_id)
    finally:
        cs.get_settings.cache_clear()
        db.reset_for_tests()
        pgvector_store.reset_for_tests()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_serving_path_still_refuses_an_unset_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup guard must survive this change on the path it protects.

    A fix that made the bootstrap work by weakening the startup check would
    pass every test above while removing the thing that stops an unconfigured
    process from silently choosing an embedding space (2026-08-14 postmortem).
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="EMBEDDING_PROVIDER is not set"):
            db.init_db()
    finally:
        cs.get_settings.cache_clear()
        db.reset_for_tests()
        pgvector_store.reset_for_tests()


def _wedge_spec() -> EmbeddingProfileSpec:
    """A registrable profile with a 3-dim geometry, so a fake backfill stays readable."""
    return EmbeddingProfileSpec(
        provider="databricks",
        model="Qwen/Qwen3-Embedding-4B",
        revision="0123456789abcdef",
        dimension=3,
        dtype="float32",
        normalization="l2",
        query_instruction_version="psg-retrieval-v1",
        preprocessing_version="text-v1",
        chunking_version="page-window-1000-v1",
        serving_runtime_version="vllm-0.19.0",
    )


def _wedge_on_an_incomplete_profile(monkeypatch: pytest.MonkeyPatch) -> str:
    """Rebuild the 2026-08-13 outage state; return the wedged profile id.

    Registers a profile, seeds one chunk, embeds NOTHING for that profile, then
    points ACTIVE_EMBEDDING_PROFILE at it and drops both init_db memos so the
    next call re-runs the assert against this database.

    EMBEDDING_PROVIDER=echo is 1536-dim and network-free, so the legacy-geometry
    branch of assert_embedding_provider_dim passes and the profile-readiness
    branch -- the one under test -- is what fires. Every value is set
    EXPLICITLY, for the reason given at the top of this file: pydantic-settings
    also reads the repo `.env`, so deleting a variable would let a developer's
    file supply it.
    """
    import config.settings as cs

    from regwatch.store import db, pgvector_store, vector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "legacy")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()

    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    db.init_db(assert_provider=False)

    profile = vector_store.register_embedding_profile(_wedge_spec())
    vector_store.add_chunks(
        ids=["chunk-a"],
        embeddings=[[1.0] + [0.0] * (_LEGACY_DIM - 1)],
        documents=["fasting bioequivalence"],
        metadatas=[{"doc_id": 1, "version_id": 10, "page": 1, "normalized_name": "drug a"}],
    )

    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", profile.profile_id)
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()
    return profile.profile_id


def _release_wedge() -> None:
    """Drop the process-global caches the wedge installs, for the next test."""
    import config.settings as cs

    from regwatch.store import db, pgvector_store

    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_an_incomplete_active_profile_wedges_plain_init_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadlock the repair flag exists for, pinned at its cause.

    Without this the two tests below could pass vacuously: if the guard ever
    stopped firing on an incomplete ACTIVE_EMBEDDING_PROFILE, "the repair
    commands still run" would prove nothing.
    """
    from regwatch.store import db

    profile_id = _wedge_on_an_incomplete_profile(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="incomplete") as excinfo:
            db.init_db()
        assert profile_id in str(excinfo.value)
    finally:
        _release_wedge()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_repair_commands_stay_usable_and_backfill_clears_the_wedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every command an operator needs mid-incident must run in the wedged state.

    On 2026-08-13 each of these died inside init_db with the same message the
    operator was trying to read about, which is what turned one oversized
    embedding batch into 3h35m of downtime.
    """
    from regwatch.process import embedder as embedder_module
    from regwatch.store import vector_store

    profile_id = _wedge_on_an_incomplete_profile(monkeypatch)
    try:
        for argv in (
            ["embedding-profile-list"],
            ["embedding-profile-coverage", profile_id],
            ["authoritative-corpus-status"],
        ):
            result = runner.invoke(app, argv)
            assert result.exit_code == 0, f"{argv}: {result.output}{result.exception!r}"
        assert vector_store.profile_embedding_coverage(profile_id).complete is False

        # The serving endpoint is the ONE thing a test cannot reach. The pending
        # page, the content-hash check, the upsert and the coverage read all run
        # against the real database, so "the wedge clears" is an assertion about
        # the database rather than about a stub.
        monkeypatch.setattr(
            embedder_module,
            "get_embedding_provider_for_profile",
            lambda _profile: SimpleNamespace(dim=3),
        )
        monkeypatch.setattr(
            embedder_module,
            "embed_documents",
            lambda _provider, texts: [[1.0, 0.0, 0.0] for _ in texts],
        )
        backfill = runner.invoke(app, ["embedding-profile-backfill", profile_id])
        assert backfill.exit_code == 0, f"{backfill.output}{backfill.exception!r}"
        assert vector_store.profile_embedding_coverage(profile_id).complete is True
    finally:
        _release_wedge()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
def test_serving_path_commands_still_refuse_the_wedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair flag must never leak onto a command that WRITES vectors.

    seed and ingest-all embed everything they ingest. Give either one
    assert_provider=False and a wrong-geometry provider writes into the active
    space instead of refusing at boot -- the K6 hazard the guard exists for.
    """
    from regwatch.ingest import psg_crawler

    profile_id = _wedge_on_an_incomplete_profile(monkeypatch)

    # Both commands crawl the FDA catalog right after init_db. If the refusal
    # ever regresses, fail here instead of reaching the live index.
    def _no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("init_db must refuse before the FDA catalog is crawled")

    monkeypatch.setattr(psg_crawler, "fetch_all_listings", _no_network)
    try:
        for argv in (["seed"], ["ingest-all"]):
            result = runner.invoke(app, argv)
            assert result.exit_code != 0, f"{argv} must refuse: {result.output}"
            assert isinstance(result.exception, RuntimeError), repr(result.exception)
            assert "incomplete" in str(result.exception)
            assert profile_id in str(result.exception)
    finally:
        _release_wedge()
