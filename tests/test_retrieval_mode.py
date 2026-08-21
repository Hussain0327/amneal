"""The mode/SQL contract, provable without Postgres.

These tests exist because the thing being fixed was invisible: the retrieval
ALGORITHM used to be a side effect of whether a metadata filter happened to be
present, and nothing recorded which one ran.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.retrieve.mode import (
    ApproximateSearchNotPermitted,
    RetrievalMode,
    RetrievalPlan,
    RetrievalScope,
    ScopeNotResolved,
    assert_mode_permitted,
    default_mode_for_scope,
)
from regwatch.store.embedding_profiles import _TIEBREAK, _where_filter_keys, build_search_sql


def _build(
    mode: RetrievalMode,
    clause: str = "",
    *,
    dimension: int = 1024,
    index_dtype: str = "vector",
    k: int = 8,
) -> tuple[str, list[str], dict[str, Any]]:
    return build_search_sql(
        mode=mode,
        profile_predicate="ce.profile_id = 'ep_x'",
        select_cols="c.id, c.text",
        clause=clause,
        schema="extensions",
        dimension=dimension,
        index_dtype=index_dtype,
        k=k,
    )


def _sql(mode: RetrievalMode, clause: str = "") -> str:
    sql, _stmts, _params = _build(mode, clause)
    return sql


# --------------------------------------------------------------- scope ---


def test_version_filter_alone_is_not_a_product_scope():
    """retrieve() adds a version clause to EVERY query, so a non-empty filter
    cannot mean 'scoped'. This is the distinction bool(clause) could not make."""
    scope = RetrievalScope.from_filters({"version_id": [1, 2, 3]})
    assert scope.product_pinned is False
    assert default_mode_for_scope(scope) is RetrievalMode.EXACT_CORPUS


@pytest.mark.parametrize("key", ["normalized_name", "doc_id", "appl_no"])
def test_product_keys_are_a_scope(key):
    scope = RetrievalScope.from_filters({key: "x", "version_id": [1]})
    assert scope.product_pinned is True
    assert default_mode_for_scope(scope) is RetrievalMode.EXACT_SCOPED


def test_empty_and_blank_filters_are_corpus_scope():
    for filters in (None, {}, {"normalized_name": ""}, {"normalized_name": None}):
        scope = RetrievalScope.from_filters(filters)
        assert scope.product_pinned is False, filters


def test_where_filter_keys_flattens_and_clause():
    flat = _where_filter_keys(
        {"$and": [{"normalized_name": {"$eq": "albuterol"}}, {"version_id": {"$in": [1]}}]}
    )
    assert set(flat) == {"normalized_name", "version_id"}


# ---------------------------------------------------------- preconditions ---


def test_exact_scoped_without_a_product_fails_closed():
    with pytest.raises(ScopeNotResolved):
        assert_mode_permitted(
            RetrievalMode.EXACT_SCOPED, RetrievalScope.from_filters({"version_id": [1]})
        )


def test_ann_is_refused_for_every_scope():
    """RegWatch retrieval is exact; ANN is not an executable runtime mode."""
    with pytest.raises(ApproximateSearchNotPermitted):
        assert_mode_permitted(
            RetrievalMode.ANN_RERANKED,
            RetrievalScope.from_filters(None),
        )
    with pytest.raises(ApproximateSearchNotPermitted):
        assert_mode_permitted(
            RetrievalMode.ANN_RERANKED,
            RetrievalScope.from_filters({"normalized_name": "albuterol"}),
        )


def test_default_is_never_approximate():
    for filters in (None, {"normalized_name": "x"}, {"version_id": [1]}):
        assert default_mode_for_scope(RetrievalScope.from_filters(filters)).is_exact


# ------------------------------------------------------------- SQL shape ---


def test_both_exact_modes_emit_identical_sql():
    """The two exact modes differ only as a policy label on the audit row."""
    assert _sql(RetrievalMode.EXACT_SCOPED, " WHERE c.normalized_name = :p0") == _sql(
        RetrievalMode.EXACT_CORPUS, " WHERE c.normalized_name = :p0"
    )


def test_exact_sql_carries_the_tiebreak_and_disables_index_scans():
    sql, stmts, params = _build(RetrievalMode.EXACT_CORPUS)
    assert f"{_TIEBREAK} LIMIT :k" in sql
    assert stmts == ["SET LOCAL enable_indexscan = off"]
    assert params == {}


def test_ann_inner_ordering_stays_a_bare_distance_operator():
    """A second sort key on the candidate ORDER BY makes the HNSW index
    unusable, silently turning ANN into a sequential scan."""
    sql = _sql(RetrievalMode.ANN_RERANKED)
    inner = sql.split("LIMIT :candidate_k")[0]
    assert _TIEBREAK not in inner
    # ...while the outer exact rerank does apply it.
    outer = sql.split("LIMIT :candidate_k")[1]
    assert f"{_TIEBREAK} LIMIT :k" in outer


def test_ann_ef_search_is_never_below_the_candidate_pool():
    """The old fixed ef_search=100 against candidate_k=200 meant the requested
    pool could never be filled."""
    _s, stmts, params = _build(RetrievalMode.ANN_RERANKED, k=50)
    candidate_k = params["candidate_k"]
    ef_search = int(stmts[0].rsplit("=", 1)[1])
    assert candidate_k == 200
    assert ef_search >= candidate_k


def test_ann_candidate_distance_follows_the_index_dtype():
    """Hardcoding `vector` would break the 2560-dim halfvec profile's index."""
    sql = _sql(RetrievalMode.ANN_RERANKED)
    assert "halfvec" not in sql
    half, _s, _p = _build(RetrievalMode.ANN_RERANKED, index_dtype="halfvec", dimension=2560)
    inner = half.split("LIMIT :candidate_k")[0]
    assert "halfvec(2560)" in inner
    # The rerank stays full precision regardless of what the index stores.
    assert "vector(2560)" in half.split("LIMIT :candidate_k")[1]


def test_exact_mode_never_sets_ef_search():
    for mode in (RetrievalMode.EXACT_SCOPED, RetrievalMode.EXACT_CORPUS):
        _s, stmts, _p = _build(mode)
        assert not any("ef_search" in stmt for stmt in stmts)


# ------------------------------------------------------------ audit row ---


def test_plan_records_what_ran():
    plan = RetrievalPlan(
        mode=RetrievalMode.EXACT_SCOPED,
        scope=RetrievalScope.from_filters({"normalized_name": "albuterol"}),
        profile_id="ep_" + "a" * 32,
        dimension=1024,
        k=50,
    ).with_result(8)
    block = plan.as_route_json()
    assert block["mode"] == "exact_scoped"
    assert block["exact"] is True
    assert block["product_pinned"] is True
    assert block["returned"] == 8
    assert block["enable_indexscan"] is False


def test_ann_plan_is_marked_inexact():
    block = RetrievalPlan(
        mode=RetrievalMode.ANN_RERANKED,
        scope=RetrievalScope.from_filters(None),
        profile_id="ep_" + "a" * 32,
        dimension=1024,
        k=8,
        enable_indexscan=True,
    ).as_route_json()
    assert block["exact"] is False
    assert block["enable_indexscan"] is True
