"""Postgres-mode ingest atomicity: version + doc fields + chunks + BE in ONE txn.

These need a real Postgres with pgvector (same opt-in as
tests/test_pgvector_store.py):

    TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:5499/postgres \
        uv run pytest tests/test_pipeline_atomic_pg.py

Everything is driven through ``ingest_listing`` so the transaction wiring
itself is under test: a failure at the chunk upsert must roll back the version
row and the doc's content fields (the crash-between-stores gap the atomic
commit closes), and the duplicate-revision insert race must surface as the
0014 unique index and be handled as the existing skip path (INV-4).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from regwatch.ingest import pipeline as pipeline_mod
from regwatch.process.extractor import extract_be as real_extract_be
from regwatch.store import db as db_module
from regwatch.store import vector_store as vs_module
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from tests.test_pipeline_idempotent import (
    PAGES,
    _listing,
    _patch_pipeline_state,
    _row_count,
)

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


class _Embedder1536:
    """Deterministic 1536-dim provider: the pipeline embeds for real in these
    tests and the K6 dim assert must pass, but no network may be touched."""

    name = "openai"
    dim = 1536

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text_item in texts:
            v = [0.0] * self.dim
            v[hash(text_item) % self.dim] = 1.0
            out.append(v)
        return out


@pytest.fixture(autouse=True)
def _pg_pipeline_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point store/db AND the vector store at the test Postgres, wiped clean."""
    import config.settings as cs

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    vs_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must be a postgres URL"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    stub = _Embedder1536()
    # Import pgvector_store BEFORE patching: it binds get_embedding_provider at
    # module scope, so a first import that happened while embedder is already
    # patched would snapshot the stub as the "original" and monkeypatch's undo
    # would leak the stub into every later test in the session.
    from regwatch.store import pgvector_store  # noqa: F401

    # The provider symbol is imported into three namespaces; patch each so the
    # pipeline, the K6 dim assert, and any direct embedder use all see the stub.
    monkeypatch.setattr("regwatch.process.embedder.get_embedding_provider", lambda *a, **k: stub)
    monkeypatch.setattr("regwatch.ingest.pipeline.get_embedding_provider", lambda *a, **k: stub)
    monkeypatch.setattr(
        "regwatch.store.pgvector_store.get_embedding_provider", lambda *a, **k: stub
    )
    yield
    db_module.reset_for_tests()
    vs_module.reset_for_tests()


def _doc_state() -> tuple[int, str]:
    with db_module.session_scope() as s:
        doc = s.scalars(select(PsgDocument)).one()
        assert doc.id is not None
        return doc.id, doc.content_hash


def _latest_version_id() -> int:
    with db_module.session_scope() as s:
        rows = list(s.scalars(select(PsgVersion.id).order_by(col(PsgVersion.id))))
    assert rows and rows[-1] is not None
    return int(rows[-1])


def test_revision_lands_atomically_and_cleans_stale_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)

    assert pipeline_mod.ingest_listing(_listing()) == "added"
    doc_id, _ = _doc_state()
    v1 = _latest_version_id()
    assert vs_module.chunks_exist(doc_id, v1)
    assert _row_count(BeRequirement) == 1

    state["hash"] = "new-hash"
    state["pages"] = [PAGES[0], PAGES[1] + "\nRevised marker."]
    assert pipeline_mod.ingest_listing(_listing()) == "revised"

    assert _row_count(PsgVersion) == 2
    v2 = _latest_version_id()
    _, doc_hash = _doc_state()
    assert doc_hash == "new-hash"
    assert vs_module.chunks_exist(doc_id, v2)
    # Stale-version cleanup still runs (post-commit) on the atomic path.
    assert not vs_module.chunks_exist(doc_id, v1)
    with db_module.session_scope() as s:
        be_versions = set(s.scalars(select(BeRequirement.version_id)))
    assert v2 in be_versions


def test_chunk_upsert_failure_rolls_back_version_doc_and_be(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE atomicity property: a crash at the chunk upsert (after the version
    insert flushed) must leave NO trace of the revision -- no version row, doc
    still describing v1, no v2 BE row, index still serving v1's chunks. On the
    pre-atomic pipeline the version row survived, which is exactly the torn
    state this PR removes."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    doc_id, _ = _doc_state()
    v1 = _latest_version_id()

    state["hash"] = "new-hash"

    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash between version commit and chunk upsert")

    monkeypatch.setattr(pipeline_mod, "add_chunks", crash)
    assert pipeline_mod.ingest_listing(_listing()) == "error"

    assert _row_count(PsgVersion) == 1  # the v2 row must NOT have landed
    _, doc_hash = _doc_state()
    assert doc_hash == "old-hash"  # doc fields rolled back with it
    assert _row_count(BeRequirement) == 1  # no v2 BE row either
    assert vs_module.chunks_exist(doc_id, v1)

    # The revision is fully retryable once the failure clears.
    monkeypatch.setattr(pipeline_mod, "add_chunks", vs_module.add_chunks)
    assert pipeline_mod.ingest_listing(_listing()) == "revised"
    assert _row_count(PsgVersion) == 2
    v2 = _latest_version_id()
    assert vs_module.chunks_exist(doc_id, v2)
    _, doc_hash = _doc_state()
    assert doc_hash == "new-hash"


def test_late_txn_failure_rolls_back_chunks_written_through_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the conn= wiring itself, not just the call order: here the REAL
    ``add_chunks(conn=...)`` upsert executes inside the commit transaction, and
    the transaction then fails at the BE row (the last in-txn step), so the
    phantom version's chunk rows must vanish with the rollback. If
    pgvector_store.add_chunks ever ignores ``conn`` and commits on its own
    engine transaction again (the pre-atomic behavior), the stray rows survive
    and this test fails -- the crash test above cannot see that regression
    because it replaces add_chunks entirely."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    doc_id, _ = _doc_state()
    v1 = _latest_version_id()

    state["hash"] = "new-hash"
    seen: dict[str, int] = {}
    real_be_row = pipeline_mod._be_requirement_row

    def spy_add_chunks(**kwargs: Any) -> None:
        # The atomic path must hand over its transaction's connection; a None
        # here would mean the self-committing engine fallback ran instead.
        assert kwargs.get("conn") is not None
        seen["n"] = len(kwargs["ids"])
        vs_module.add_chunks(**kwargs)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated failure after the in-txn chunk upsert")

    monkeypatch.setattr(pipeline_mod, "add_chunks", spy_add_chunks)
    # _be_requirement_row runs AFTER the chunk upsert, still inside the txn.
    monkeypatch.setattr(pipeline_mod, "_be_requirement_row", boom)
    assert pipeline_mod.ingest_listing(_listing()) == "error"

    # Vacuity guard: the real upsert really ran (inside the txn) before the
    # failure; without this the test could pass while add_chunks never executed.
    assert seen.get("n", 0) > 0
    assert _row_count(PsgVersion) == 1
    _, doc_hash = _doc_state()
    assert doc_hash == "old-hash"
    assert vs_module.chunks_exist(doc_id, v1)
    with db_module.get_engine().connect() as conn:
        stray = conn.execute(
            text("SELECT count(*) FROM chunk WHERE doc_id = :d AND version_id != :v"),
            {"d": doc_id, "v": v1},
        ).scalar_one()
    assert stray == 0  # the phantom version's chunks rolled back with it

    # Fully retryable once the failure clears -- the rolled-back upsert left no
    # half-state the ON CONFLICT retry could trip on. Selective restore
    # (monkeypatch.undo would also strip the fixture's env).
    monkeypatch.setattr(pipeline_mod, "_be_requirement_row", real_be_row)
    monkeypatch.setattr(pipeline_mod, "add_chunks", vs_module.add_chunks)
    assert pipeline_mod.ingest_listing(_listing()) == "revised"
    assert _row_count(PsgVersion) == 2
    v2 = _latest_version_id()
    assert vs_module.chunks_exist(doc_id, v2)


def test_extraction_failure_still_lands_version_and_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE extraction runs BEFORE the transaction (it is a paid LLM call that
    must never hold the txn open); its failure is swallowed, the revision still
    lands atomically, and the unchanged-path backfill heals the BE gap next
    run (INV-1)."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    doc_id, _ = _doc_state()

    state["hash"] = "new-hash"

    def boom(_pages: list[str]) -> Any:
        raise RuntimeError("extractor outage")

    monkeypatch.setattr(pipeline_mod, "extract_be", boom)
    assert pipeline_mod.ingest_listing(_listing()) == "revised"
    assert _row_count(PsgVersion) == 2
    v2 = _latest_version_id()
    assert vs_module.chunks_exist(doc_id, v2)
    assert _row_count(BeRequirement) == 1  # v1's only; v2's extraction failed

    # Selective restore (monkeypatch.undo would also strip the fixture's env);
    # the pipeline binds extractor.extract_be at import, so the direct import
    # IS the original object.
    monkeypatch.setattr(pipeline_mod, "extract_be", real_extract_be)
    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"
    with db_module.session_scope() as s:
        be_versions = list(s.scalars(select(BeRequirement.version_id)))
    assert sorted(be_versions)[-1] == v2
    assert len(be_versions) == 2


def test_duplicate_insert_race_hits_unique_index_and_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlapping run commits AFTER our in-transaction hash re-check:
    simulated by pinning the re-check at the stale value while the racing row
    already exists. The INSERT must collide with uq_psg_version_doc_hash, the
    WHOLE transaction (doc fields + chunks + BE) must roll back, and the run
    reports "unchanged" -- one version row per revision, so the next watch run
    cannot double-alert the same FDA change (INV-4)."""
    state: dict[str, Any] = {"hash": "old-hash", "pages": PAGES}
    _patch_pipeline_state(monkeypatch, state)
    assert pipeline_mod.ingest_listing(_listing()) == "added"
    doc_id, _ = _doc_state()

    state["hash"] = "new-hash"
    with db_module.session_scope() as s:
        s.add(
            PsgVersion(
                psg_document_id=doc_id,
                content_hash="new-hash",
                diff_summary="committed by the overlapping run",
            )
        )
    racing_v2 = _latest_version_id()

    real_latest_hash = pipeline_mod._latest_hash_in_session
    monkeypatch.setattr(pipeline_mod, "_latest_hash_in_session", lambda s, d: "old-hash")
    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"

    assert _row_count(PsgVersion) == 2  # v1 + the racing row; no third
    _, doc_hash = _doc_state()
    assert doc_hash == "old-hash"  # our rolled-back txn left the doc alone
    # The racing row landed chunkless in this simulation, and OUR run never
    # reached the chunk upsert (the INSERT collided at flush, before the chunk
    # block; rolled-back in-txn chunk writes are pinned by
    # test_late_txn_failure_rolls_back_chunks_written_through_conn). The next
    # honest run's backfill owns racing_v2's chunks.
    assert not vs_module.chunks_exist(doc_id, racing_v2)
    assert _row_count(BeRequirement) == 1  # and our BE row rolled back too

    # Next (honest) run: same content -> unchanged, and the standing backfill
    # heals the racing row's missing chunks/BE instead of re-reporting a change.
    # Selective restore (monkeypatch.undo would also strip the fixture's env).
    monkeypatch.setattr(pipeline_mod, "_latest_hash_in_session", real_latest_hash)
    assert pipeline_mod.ingest_listing(_listing()) == "unchanged"
    assert _row_count(PsgVersion) == 2
    assert vs_module.chunks_exist(doc_id, racing_v2)
    with db_module.session_scope() as s:
        be_versions = set(s.scalars(select(BeRequirement.version_id)))
    assert racing_v2 in be_versions
