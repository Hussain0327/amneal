"""Version scoping overlaps the query-embedding call in retrieve().

retrieve() used to run embed_query strictly before
_current_version_ids_for_filters even though scoping depends only on the
filters, never on the query vector. These tests pin down:

  * semantic parity on every path (scoped, authoritative, explicit version_id
    filter, empty current-version ids, vector-only mode),
  * the overlap itself: the scoping query starts before the embed finishes
    (this test FAILS on the old serial code),
  * error precedence identical to the old serial order (an embed error wins
    even when scoping also failed; a scoping error surfaces after a
    successful embed),
  * zero thread overhead on the paths that never scope.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from regwatch.retrieve import retriever as retriever_module
from regwatch.retrieve.retriever import retrieve
from regwatch.sources.policy import allowed_source_families
from regwatch.store.vector_store import Hit

_QV = [0.5, 0.25, 0.125]


class _EmbedBoom(RuntimeError):
    pass


class _ScopeBoom(RuntimeError):
    pass


class _SearchSpy:
    """Stands in for similarity_search and records every call's `where`."""

    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def __call__(self, qv: list[float], *, k: int, where: dict[str, Any] | None) -> list[Hit]:
        self.calls.append({"qv": qv, "k": k, "where": where})
        return self.hits


class _ForbiddenExecutor:
    """A ThreadPoolExecutor stand-in that fails the test if instantiated."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("ThreadPoolExecutor must not be used on this path")


def _hit(version_id: int = 1) -> Hit:
    return Hit(
        chunk_id=f"chunk-{version_id}",
        text="albuterol fasting BE study",
        metadata={
            "doc_id": 1,
            "version_id": version_id,
            "page": 1,
            "normalized_name": "albuterol sulfate",
            "appl_no": "020503",
            "source_url": "https://example.invalid/PSG_020503.pdf",
            "section_path": "",
        },
        score=0.9,
    )


def test_scoped_path_applies_current_version_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    scope_calls: list[dict[str, Any] | None] = []

    def fake_scope(filters: dict[str, Any] | None) -> list[int]:
        scope_calls.append(filters)
        return [5, 7]

    search = _SearchSpy([_hit(version_id=5)])
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fake_scope)
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    passages = retrieve("fasting BE study", k=5, filters={"normalized_name": "albuterol sulfate"})

    assert scope_calls == [{"normalized_name": "albuterol sulfate"}]
    assert search.calls == [
        {
            "qv": _QV,
            "k": 5,
            "where": {
                "$and": [
                    {"normalized_name": {"$eq": "albuterol sulfate"}},
                    {"version_id": {"$in": [5, 7]}},
                ]
            },
        }
    ]
    assert [p.version_id for p in passages] == [5]
    assert passages[0].short_name == "PSG_020503"


def test_vector_only_mode_adds_no_version_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    # _current_version_ids_for_filters returns None when no PSG catalog exists;
    # that must keep meaning "no version clause", not "no results".
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", lambda filters: None)
    search = _SearchSpy([_hit()])
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    passages = retrieve("fasting BE study", k=5)

    assert search.calls == [{"qv": _QV, "k": 5, "where": None}]
    assert len(passages) == 1


def test_empty_current_version_ids_returns_no_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", lambda filters: [])
    search = _SearchSpy([_hit()])
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    assert retrieve("fasting BE study", k=5, filters={"normalized_name": "nope"}) == []
    assert search.calls == []


def test_explicit_version_id_filter_skips_scoping_and_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_scope(filters: dict[str, Any] | None) -> list[int]:
        raise AssertionError("scoping must not run for an explicit version_id filter")

    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fail_scope)
    monkeypatch.setattr(retriever_module, "ThreadPoolExecutor", _ForbiddenExecutor)
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    search = _SearchSpy([_hit(version_id=3)])
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    passages = retrieve("fasting BE study", k=5, filters={"version_id": 3})

    assert search.calls == [{"qv": _QV, "k": 5, "where": {"version_id": {"$eq": 3}}}]
    assert [p.version_id for p in passages] == [3]


def test_authoritative_corpus_skips_scoping_and_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as cs

    # validation_alias, not the field name: settings.py binds this field to
    # REGWATCH_RETRIEVAL_CORPUS (same pattern as test_embedding_profiles.py).
    monkeypatch.setenv("REGWATCH_RETRIEVAL_CORPUS", "authoritative_fda")
    cs.get_settings.cache_clear()

    def fail_scope(filters: dict[str, Any] | None) -> list[int]:
        raise AssertionError("scoping must not run on the authoritative corpus")

    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fail_scope)
    monkeypatch.setattr(retriever_module, "ThreadPoolExecutor", _ForbiddenExecutor)
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    search = _SearchSpy([_hit()])
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    passages = retrieve("fasting BE study", k=5)

    assert search.calls == [
        {
            "qv": _QV,
            "k": 5,
            "where": {"source_family": {"$in": list(allowed_source_families())}},
        }
    ]
    assert len(passages) == 1


def test_scoping_receives_folded_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    # The old serial order folded the filters BEFORE scoping; the overlapped
    # path must keep feeding the FOLDED filters to both the scoping query and
    # the vector-store `where`.
    raw = {"normalized_name": "albuterol sulfate", "psg_type": "DRAFT"}
    folded_marker = {"normalized_name": "albuterol sulfate", "psg_type": "draft"}
    fold_calls: list[dict[str, Any] | None] = []
    scope_calls: list[dict[str, Any] | None] = []

    def fake_fold(filters: dict[str, Any] | None) -> dict[str, Any] | None:
        fold_calls.append(filters)
        return folded_marker

    def fake_scope(filters: dict[str, Any] | None) -> list[int]:
        scope_calls.append(filters)
        return [9]

    search = _SearchSpy([_hit(version_id=9)])
    monkeypatch.setattr(retriever_module, "_fold_filter_casing", fake_fold)
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fake_scope)
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    retrieve("fasting BE study", k=5, filters=raw)

    assert fold_calls == [raw]
    assert scope_calls == [folded_marker]
    assert search.calls[0]["where"] == {
        "$and": [
            {"normalized_name": {"$eq": "albuterol sulfate"}},
            {"psg_type": {"$eq": "draft"}},
            {"version_id": {"$in": [9]}},
        ]
    }


def test_scoping_query_starts_before_embed_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The overlap proof: the scoping call must START while the embed call is
    # still in flight. On the old serial code the embed runs first, this wait
    # times out, and the test fails.
    scope_started = threading.Event()

    def fake_scope(filters: dict[str, Any] | None) -> list[int]:
        scope_started.set()
        return [1]

    def fake_embed(embedder: Any, query: str) -> list[float]:
        assert scope_started.wait(timeout=2.0), "scoping did not start during the embed call"
        return _QV

    search = _SearchSpy([_hit(version_id=1)])
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fake_scope)
    monkeypatch.setattr(retriever_module, "embed_query", fake_embed)
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    passages = retrieve("fasting BE study", k=5)

    assert [p.version_id for p in passages] == [1]
    assert search.calls[0]["where"] == {"version_id": {"$in": [1]}}


def test_embed_error_wins_even_when_scoping_also_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Old serial order: embed ran first, so its error always surfaced. The
    # handshake makes the concurrent case deterministic -- scoping has already
    # RAISED by the time the embed raises, and the embed error must still win.
    scope_failed = threading.Event()

    def fake_scope(filters: dict[str, Any] | None) -> list[int]:
        scope_failed.set()
        raise _ScopeBoom("scoping failed")

    def fake_embed(embedder: Any, query: str) -> list[float]:
        assert scope_failed.wait(timeout=2.0), "scoping did not fail during the embed call"
        raise _EmbedBoom("embed failed")

    search = _SearchSpy([_hit()])
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fake_scope)
    monkeypatch.setattr(retriever_module, "embed_query", fake_embed)
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    with pytest.raises(_EmbedBoom):
        retrieve("fasting BE study", k=5)
    assert search.calls == []


def test_scoping_error_propagates_after_successful_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_scope(filters: dict[str, Any] | None) -> list[int]:
        raise _ScopeBoom("scoping failed")

    search = _SearchSpy([_hit()])
    monkeypatch.setattr(retriever_module, "_current_version_ids_for_filters", fake_scope)
    monkeypatch.setattr(retriever_module, "embed_query", lambda embedder, query: _QV)
    monkeypatch.setattr(retriever_module, "similarity_search", search)

    with pytest.raises(_ScopeBoom):
        retrieve("fasting BE study", k=5)
    assert search.calls == []
