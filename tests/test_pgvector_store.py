"""pgvector chunk store — interface + score parity with the retired Chroma backend.

These tests need a real Postgres with the pgvector extension available.
Set TEST_DATABASE_URL (e.g. a `pgvector/pgvector:pg17` docker container) to
run them; without it the whole module skips, keeping the default gates
network/DB-free.

Everything is exercised through `regwatch.store.vector_store` so the
DATABASE_URL dispatch itself is under test, not just the pg implementation.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text as sa_text

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


DIM = 1536


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _blend(a: int, b: int) -> list[float]:
    """Unit vector halfway between two axes (cosine 0.7071 to either)."""
    v = [0.0] * DIM
    v[a] = v[b] = 1.0 / math.sqrt(2.0)
    return v


def _meta(doc_id: int, version_id: int, name: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doc_id": doc_id,
        "version_id": version_id,
        "page": 1,
        "section_path": "II.A",
        "normalized_name": name,
        "dosage_form": "tablet",
        "route": "oral",
        "source_url": "https://example.invalid/psg.pdf",
        "psg_type": "final",
        "appl_no": f"{doc_id:06d}",
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _pg_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app at the test Postgres with a clean chunk table."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # A 1536-dim provider so the pg store's fail-fast dim assert passes; no
    # API key is needed because these tests pass embeddings in directly.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    import config.settings as cs

    cs.get_settings.cache_clear()

    from regwatch.store import pgvector_store

    pgvector_store.reset_for_tests()
    engine = pgvector_store.get_engine()
    with engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS chunk CASCADE"))
    yield
    pgvector_store.reset_for_tests()


def test_facade_round_trip() -> None:
    from regwatch.store import vector_store as vs

    texts = ["fasting bioequivalence", "single-dose crossover", "dissolution method 2"]
    vs.add_chunks(
        ids=["a", "b", "c"],
        embeddings=[_unit(0), _blend(0, 1), _unit(1)],
        documents=texts,
        metadatas=[_meta(1, 10, "drug x"), _meta(1, 10, "drug x"), _meta(2, 20, "drug y")],
    )
    assert vs.collection_size() == 3
    hits = vs.similarity_search(_unit(0), k=3)
    assert [h.chunk_id for h in hits] == ["a", "b", "c"]
    assert hits[0].text == "fasting bioequivalence"


def test_score_mapping_matches_chroma_convention() -> None:
    """score = 1 - cosine_distance/2: 1.0 identical, 0.5 orthogonal, 0.0 opposite."""
    from regwatch.store import vector_store as vs

    opposite = [-x for x in _unit(0)]
    vs.add_chunks(
        ids=["same", "halfway", "ortho", "anti"],
        embeddings=[_unit(0), _blend(0, 1), _unit(1), opposite],
        documents=["s", "h", "o", "a"],
        metadatas=[_meta(1, 1, "n")] * 4,
    )
    hits = {h.chunk_id: h.score for h in vs.similarity_search(_unit(0), k=4)}
    assert hits["same"] == pytest.approx(1.0, abs=1e-6)
    assert hits["halfway"] == pytest.approx(0.5 + math.sqrt(2.0) / 4.0, abs=1e-6)
    assert hits["ortho"] == pytest.approx(0.5, abs=1e-6)
    assert hits["anti"] == pytest.approx(0.0, abs=1e-6)
    assert all(0.0 <= s <= 1.0 for s in hits.values())


def test_metadata_round_trip_types() -> None:
    from regwatch.store import vector_store as vs

    vs.add_chunks(
        ids=["m1"],
        embeddings=[_unit(3)],
        documents=["text body"],
        metadatas=[_meta(7, 70, "drug m", page=4, section_path="III.B")],
    )
    (hit,) = vs.similarity_search(_unit(3), k=1)
    m = hit.metadata
    assert m["doc_id"] == 7
    assert m["version_id"] == 70
    assert m["page"] == 4
    assert m["section_path"] == "III.B"
    assert m["normalized_name"] == "drug m"
    assert m["dosage_form"] == "tablet"
    assert m["route"] == "oral"
    assert m["psg_type"] == "final"
    assert m["appl_no"] == "000007"
    assert m["source_url"] == "https://example.invalid/psg.pdf"


def test_filtered_search_eq_and_in() -> None:
    from regwatch.store import vector_store as vs

    vs.add_chunks(
        ids=["x1", "x2", "y1"],
        embeddings=[_unit(0), _unit(1), _unit(0)],
        documents=["x one", "x two", "y one"],
        metadatas=[_meta(1, 10, "drug x"), _meta(1, 11, "drug x"), _meta(2, 20, "drug y")],
    )
    # $eq filter: only drug x, even though y1 is the closest vector overall.
    hits = vs.similarity_search(_unit(0), k=5, where={"normalized_name": {"$eq": "drug x"}})
    assert [h.chunk_id for h in hits] == ["x1", "x2"]
    # $and of $eq + $in (the retriever's current-version scoping shape).
    hits = vs.similarity_search(
        _unit(0),
        k=5,
        where={"$and": [{"normalized_name": {"$eq": "drug x"}}, {"version_id": {"$in": [11]}}]},
    )
    assert [h.chunk_id for h in hits] == ["x2"]
    # Empty $in matches nothing (parity with Chroma).
    assert vs.similarity_search(_unit(0), k=5, where={"version_id": {"$in": []}}) == []


def test_unknown_filter_field_fails_loud() -> None:
    from regwatch.store import vector_store as vs

    vs.add_chunks(ids=["z"], embeddings=[_unit(0)], documents=["z"], metadatas=[_meta(1, 1, "z")])
    with pytest.raises(ValueError, match="unsupported chunk filter field"):
        vs.similarity_search(_unit(0), k=1, where={"not_a_column": {"$eq": "v"}})


def test_delete_chunks_for_doc_except_version() -> None:
    from regwatch.store import vector_store as vs

    no_version = _meta(1, 0, "drug x")
    del no_version["version_id"]  # NULL version_id must be treated as stale
    vs.add_chunks(
        ids=["old1", "old2", "cur", "null-v", "other-doc"],
        embeddings=[_unit(0), _unit(1), _unit(2), _unit(3), _unit(4)],
        documents=["o1", "o2", "c", "nv", "od"],
        metadatas=[
            _meta(1, 10, "drug x"),
            _meta(1, 10, "drug x"),
            _meta(1, 11, "drug x"),
            no_version,
            _meta(2, 20, "drug y"),
        ],
    )
    deleted = vs.delete_chunks_for_doc_except_version(doc_id=1, keep_version_id=11)
    assert deleted == 3
    assert vs.collection_size() == 2
    remaining = {h.chunk_id for h in vs.similarity_search(_unit(2), k=10)}
    assert remaining == {"cur", "other-doc"}
    # Idempotent: nothing stale left.
    assert vs.delete_chunks_for_doc_except_version(doc_id=1, keep_version_id=11) == 0


def test_add_chunks_upserts_on_id() -> None:
    from regwatch.store import vector_store as vs

    vs.add_chunks(
        ids=["u"], embeddings=[_unit(0)], documents=["before"], metadatas=[_meta(1, 1, "a")]
    )
    vs.add_chunks(
        ids=["u"], embeddings=[_unit(1)], documents=["after"], metadatas=[_meta(1, 2, "b")]
    )
    assert vs.collection_size() == 1
    (hit,) = vs.similarity_search(_unit(1), k=1)
    assert hit.text == "after"
    assert hit.metadata["normalized_name"] == "b"
    assert hit.score == pytest.approx(1.0, abs=1e-6)


def test_distinct_metadata_values() -> None:
    from regwatch.store import vector_store as vs

    vs.add_chunks(
        ids=["1", "2", "3"],
        embeddings=[_unit(0), _unit(1), _unit(2)],
        documents=["a", "b", "c"],
        metadatas=[_meta(1, 1, "drug x"), _meta(1, 1, "drug x"), _meta(2, 2, "drug y")],
    )
    assert vs.distinct_metadata_values("normalized_name") == {"drug x", "drug y"}
    # Unknown / non-text key behaves like a metadata key no chunk carries.
    assert vs.distinct_metadata_values("active_ingredient") == set()
    # Cache invalidation on write.
    vs.add_chunks(
        ids=["4"], embeddings=[_unit(3)], documents=["d"], metadatas=[_meta(3, 3, "drug z")]
    )
    assert vs.distinct_metadata_values("normalized_name") == {"drug x", "drug y", "drug z"}


def test_wrong_embedding_dim_rejected() -> None:
    from regwatch.store import vector_store as vs

    with pytest.raises(ValueError, match="1536"):
        vs.add_chunks(
            ids=["bad"], embeddings=[[0.1] * 384], documents=["b"], metadatas=[_meta(1, 1, "n")]
        )
    with pytest.raises(ValueError, match="1536"):
        vs.similarity_search([0.1] * 384, k=1)


def test_provider_dim_mismatch_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """K6: a wrong-dim provider must refuse to serve.

    No shipped provider has a non-1536 legacy dimension any more (local-bge
    was removed 2026-08-17), so a stub stands in for a future misconfigured
    one; the assert under test is unchanged.
    """
    import config.settings as cs

    from regwatch.store import pgvector_store
    from regwatch.store import vector_store as vs

    class _Stub384:
        name = "stub-384"
        dim = 384

    monkeypatch.setattr(pgvector_store, "get_embedding_provider", lambda: _Stub384())
    cs.get_settings.cache_clear()
    pgvector_store.reset_for_tests()
    with pytest.raises(RuntimeError, match="vector\\(1536\\)"):
        vs.collection_size()


def test_schema_has_hnsw_index_and_rls() -> None:
    from regwatch.store import pgvector_store
    from regwatch.store import vector_store as vs

    vs.add_chunks(ids=["s"], embeddings=[_unit(0)], documents=["s"], metadatas=[_meta(1, 1, "n")])
    engine = pgvector_store.get_engine()
    with engine.connect() as conn:
        indexdefs = [
            r[0]
            for r in conn.execute(
                sa_text("SELECT indexdef FROM pg_indexes WHERE tablename = 'chunk'")
            )
        ]
        rls = conn.execute(
            sa_text("SELECT relrowsecurity FROM pg_class WHERE relname = 'chunk'")
        ).scalar()
    assert any("hnsw" in d for d in indexdefs)
    assert any("normalized_name" in d for d in indexdefs)
    assert any("doc_id" in d for d in indexdefs)
    assert any("appl_no" in d for d in indexdefs)
    assert rls is True
