"""Additive embedding-profile storage and resumable backfill primitives."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from regwatch.store.embedding_profiles import (
    EmbeddingProfile,
    EmbeddingProfileSpec,
    _index_spec,
)

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
LEGACY_DIM = 1536


def _spec(
    *,
    model: str = "Qwen/Qwen3-Embedding-4B",
    dimension: int = 3,
) -> EmbeddingProfileSpec:
    return EmbeddingProfileSpec(
        provider="databricks",
        model=model,
        revision="0123456789abcdef",
        dimension=dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version="psg-retrieval-v1",
        preprocessing_version="text-v1",
        chunking_version="page-window-1000-v1",
        serving_runtime_version="vllm-0.19.0",
    )


def _profile(dimension: int) -> EmbeddingProfile:
    spec = _spec(dimension=dimension)
    return EmbeddingProfile(
        profile_id=spec.profile_id,
        fingerprint=spec.fingerprint,
        created_at=datetime.now(UTC),
        **{key: value for key, value in spec.__dict__.items()},
    )


def _legacy_unit(axis: int) -> list[float]:
    vector = [0.0] * LEGACY_DIM
    vector[axis] = 1.0
    return vector


def _meta(doc_id: int, version_id: int, name: str) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "version_id": version_id,
        "page": 1,
        "normalized_name": name,
        "dosage_form": "tablet",
        "route": "oral",
        "appl_no": f"{doc_id:06d}",
    }


def test_profile_fingerprint_is_deterministic_and_geometry_bound() -> None:
    first = _spec()
    same = _spec()
    different_dimension = _spec(dimension=1536)

    assert first.profile_id == same.profile_id
    assert first.fingerprint == same.fingerprint
    assert first.profile_id.startswith("ep_")
    assert len(first.profile_id) == 35
    assert different_dimension.profile_id != first.profile_id


@pytest.mark.parametrize(
    ("dimension", "dtype"),
    [(1536, "vector"), (2000, "vector"), (2001, "halfvec"), (2560, "halfvec")],
)
def test_index_plan_covers_regular_and_native_qwen_dimensions(
    dimension: int,
    dtype: str,
) -> None:
    plan = _index_spec(_profile(dimension), concurrently=True)
    assert plan.dimension == dimension
    assert plan.index_dtype == dtype
    assert plan.uses_halfvec is (dtype == "halfvec")


def test_index_plan_rejects_unindexable_dimension() -> None:
    with pytest.raises(ValueError, match="halfvec limit"):
        _index_spec(_profile(4096), concurrently=False)


@pytest.fixture()
def pg_profile_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")

    import config.settings as cs

    from regwatch.store import db, pgvector_store, vector_store

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    cs.get_settings.cache_clear()
    db.reset_for_tests()
    pgvector_store.reset_for_tests()
    engine = db.get_engine()
    assert engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    db.init_db()
    yield vector_store
    db.reset_for_tests()
    pgvector_store.reset_for_tests()


def _seed_two_chunks(vector_store: Any) -> None:
    vector_store.add_chunks(
        ids=["chunk-a", "chunk-b"],
        embeddings=[_legacy_unit(0), _legacy_unit(1)],
        documents=["fasting bioequivalence", "fed crossover study"],
        metadatas=[_meta(1, 10, "drug a"), _meta(2, 20, "drug b")],
    )


def test_profile_registry_is_idempotent_and_database_immutable(
    pg_profile_store: Any,
) -> None:
    from regwatch.store import db

    spec = _spec()
    first = pg_profile_store.register_embedding_profile(spec)
    second = pg_profile_store.register_embedding_profile(spec)

    assert first == second
    assert first.spec == spec
    assert pg_profile_store.list_embedding_profiles() == [first]

    with pytest.raises(SQLAlchemyError), db.get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE embedding_profile SET model = 'mutated' " "WHERE profile_id = :profile_id"
            ),
            {"profile_id": first.profile_id},
        )

    assert pg_profile_store.get_embedding_profile(first.profile_id) == first


def test_backfill_is_resumable_and_text_changes_invalidate_shadow_rows(
    pg_profile_store: Any,
) -> None:
    from regwatch.store import db

    _seed_two_chunks(pg_profile_store)
    profile = pg_profile_store.register_embedding_profile(_spec())

    first_page = pg_profile_store.pending_profile_chunks(profile.profile_id, limit=1)
    assert [chunk.chunk_id for chunk in first_page] == ["chunk-a"]
    second_page = pg_profile_store.pending_profile_chunks(
        profile.profile_id,
        limit=1,
        after_chunk_id="chunk-a",
    )
    assert [chunk.chunk_id for chunk in second_page] == ["chunk-b"]

    pending = first_page[0]
    with pytest.raises(ValueError, match="content hash mismatch"):
        pg_profile_store.upsert_profile_embeddings(
            profile.profile_id,
            [pending.chunk_id],
            [[1.0, 0.0, 0.0]],
            ["0" * 64],
        )

    pg_profile_store.upsert_profile_embeddings(
        profile.profile_id,
        [pending.chunk_id],
        [[1.0, 0.0, 0.0]],
        [pending.content_hash],
    )
    coverage = pg_profile_store.profile_embedding_coverage(profile.profile_id)
    assert (coverage.total_chunks, coverage.embedded_chunks, coverage.pending_chunks) == (2, 1, 1)
    assert [
        chunk.chunk_id for chunk in pg_profile_store.pending_profile_chunks(profile.profile_id)
    ] == ["chunk-b"]

    with db.get_engine().connect() as conn:
        stored = (
            conn.execute(
                text(
                    "SELECT content_hash, embedded_at FROM chunk_embedding "
                    "WHERE profile_id = :profile_id AND chunk_id = 'chunk-a'"
                ),
                {"profile_id": profile.profile_id},
            )
            .mappings()
            .one()
        )
    assert stored["content_hash"] == pending.content_hash
    assert stored["embedded_at"] is not None

    # The active legacy upsert remains the source of chunk text.  Its UPDATE
    # trigger deletes stale profile rows, making the changed chunk pending.
    pg_profile_store.add_chunks(
        ids=["chunk-a"],
        embeddings=[_legacy_unit(0)],
        documents=["fasting and fed bioequivalence"],
        metadatas=[_meta(1, 10, "drug a")],
    )
    assert pg_profile_store.profile_embedding_coverage(profile.profile_id).embedded_chunks == 0
    changed = pg_profile_store.pending_profile_chunks(profile.profile_id)
    assert {chunk.chunk_id for chunk in changed} == {"chunk-a", "chunk-b"}
    assert next(chunk for chunk in changed if chunk.chunk_id == "chunk-a").content_hash != (
        pending.content_hash
    )


def test_profile_search_never_mixes_vector_spaces(pg_profile_store: Any) -> None:
    _seed_two_chunks(pg_profile_store)
    profile_a = pg_profile_store.register_embedding_profile(_spec(model="model-a"))
    profile_b = pg_profile_store.register_embedding_profile(_spec(model="model-b"))
    pending_a = pg_profile_store.pending_profile_chunks(profile_a.profile_id)
    hashes = [chunk.content_hash for chunk in pending_a]
    ids = [chunk.chunk_id for chunk in pending_a]

    pg_profile_store.upsert_profile_embeddings(
        profile_a.profile_id,
        ids,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        hashes,
    )
    pg_profile_store.upsert_profile_embeddings(
        profile_b.profile_id,
        ids,
        [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
        hashes,
    )

    hits_a = pg_profile_store.similarity_search_profile(
        profile_a.profile_id,
        [1.0, 0.0, 0.0],
        k=2,
    )
    hits_b = pg_profile_store.similarity_search_profile(
        profile_b.profile_id,
        [1.0, 0.0, 0.0],
        k=2,
    )
    assert [hit.chunk_id for hit in hits_a] == ["chunk-a", "chunk-b"]
    assert [hit.chunk_id for hit in hits_b] == ["chunk-b", "chunk-a"]

    filtered = pg_profile_store.similarity_search_profile(
        profile_a.profile_id,
        [0.0, 1.0, 0.0],
        k=2,
        where={"normalized_name": {"$eq": "drug a"}},
    )
    assert [hit.chunk_id for hit in filtered] == ["chunk-a"]


def test_profile_search_revalidates_profile_id_at_the_sql_boundary(
    pg_profile_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQL boundary must not trust a NON-LOCAL validation.

    profile_id is interpolated into a SQL string literal rather than bound,
    deliberately: binding loses the partial-index plan match. Today it is safe
    only because get_embedding_profile() validates a few statements earlier in
    the same function -- an ordering accident, not an enforced contract.

    This pins the boundary itself. It simulates exactly the refactor that would
    remove the guarantee (a resolver that returns a profile without validating
    the caller's id) and asserts the search still refuses. Without the
    re-assert, the quote in the injected id closes the literal and reaches
    Postgres as query text.
    """
    from regwatch.store import embedding_profiles

    _seed_two_chunks(pg_profile_store)
    profile = pg_profile_store.register_embedding_profile(_spec(model="model-a"))

    # similarity_search_profile resolves this name from its OWN module globals,
    # so patch it there -- the fixture hands back the vector_store re-export.
    monkeypatch.setattr(embedding_profiles, "get_embedding_profile", lambda _profile_id: profile)

    with pytest.raises(ValueError, match="profile_id"):
        embedding_profiles.similarity_search_profile(
            "ep_' OR '1'='1",
            [1.0, 0.0, 0.0],
            k=1,
        )


def test_profile_indexes_use_vector_1536_and_halfvec_2560(
    pg_profile_store: Any,
) -> None:
    from regwatch.store import db

    regular = pg_profile_store.register_embedding_profile(_spec(model="regular", dimension=1536))
    native = pg_profile_store.register_embedding_profile(_spec(model="native", dimension=2560))

    regular_plan = pg_profile_store.ensure_profile_hnsw_index(
        regular.profile_id,
        concurrently=False,
    )
    native_plan = pg_profile_store.ensure_profile_hnsw_index(
        native.profile_id,
        concurrently=False,
    )

    with db.get_engine().connect() as conn:
        definitions = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'chunk_embedding' AND indexname LIKE '%_hnsw'"
                )
            )
        }
    assert "vector_cosine_ops" in definitions[regular_plan.index_name]
    assert "vector(1536)" in definitions[regular_plan.index_name]
    assert "halfvec_cosine_ops" in definitions[native_plan.index_name]
    assert "halfvec(2560)" in definitions[native_plan.index_name]


def test_activation_gate_requires_complete_coverage_and_ready_index(
    pg_profile_store: Any,
) -> None:
    _seed_two_chunks(pg_profile_store)
    profile = pg_profile_store.register_embedding_profile(_spec())
    pending = pg_profile_store.pending_profile_chunks(profile.profile_id)

    pg_profile_store.ensure_profile_hnsw_index(profile.profile_id, concurrently=False)
    assert pg_profile_store.profile_hnsw_index_ready(profile.profile_id) is True
    with pytest.raises(RuntimeError, match="incomplete"):
        pg_profile_store.assert_profile_ready_for_activation(profile.profile_id)

    pg_profile_store.upsert_profile_embeddings(
        profile.profile_id,
        [chunk.chunk_id for chunk in pending],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        [chunk.content_hash for chunk in pending],
    )

    assert pg_profile_store.assert_profile_ready_for_activation(profile.profile_id) == profile
