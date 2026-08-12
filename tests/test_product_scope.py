"""ProductScope is a lossless view of a turn's filter dict, not a new policy.

These tests exist because the refactor they cover is only safe if it is boring.
ProductScope sits on the retrieval boundary, and everything downstream of that
boundary -- the WHERE clause, the retrieval mode, and therefore INV-1's product
constraint and INV-2's cosine gate -- is a pure function of the filter mapping.
So the bar is not "the new type works"; it is "the new type is indistinguishable
from the dict it replaced". Each test below pins one way it could stop being
indistinguishable.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.common.conversation import SESSION_FILTER_KEYS, _safe_filters
from regwatch.retrieve.mode import RetrievalScope, default_mode_for_scope
from regwatch.retrieve.retriever import _build_where, _fold_filter_casing
from regwatch.retrieve.scope import ProductScope

# Every filter mapping shape the live paths can produce: the API whitelist keeps
# only SESSION_FILTER_KEYS (api/main.py::_whitelist_filter_keys), and the two
# internal ask() callers pass normalized_name plus an optional dosage_form/route
# pair (assemble/dossier.py:436-445, whitepaper/populator.py:1986-1989). The
# empty-value and casing cases are the ones that would silently change a query.
_LIVE_SHAPES: list[dict[str, Any]] = [
    {},
    {"normalized_name": "albuterol sulfate"},
    {"normalized_name": "albuterol sulfate", "dosage_form": "Aerosol, Metered"},
    {
        "normalized_name": "beclomethasone dipropionate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
    },
    {"normalized_name": "albuterol sulfate", "psg_type": "draft"},
    {"doc_id": "PSG_020503"},
    {"normalized_name": "albuterol sulfate", "doc_id": "PSG_020503"},
    # Empty values: dropped identically by _safe_filters and _build_where today.
    {"normalized_name": "albuterol sulfate", "dosage_form": "", "route": None},
    {"normalized_name": "albuterol sulfate", "psg_type": []},
    {"normalized_name": "", "dosage_form": None},
    # Casing variants a hand-typed UI filter can produce.
    {"normalized_name": "albuterol sulfate", "dosage_form": "aerosol, metered"},
    {"normalized_name": "albuterol sulfate", "route": "INHALATION"},
]


@pytest.mark.parametrize("raw", _LIVE_SHAPES)
def test_as_filters_matches_safe_filters_on_every_live_shape(raw: dict[str, Any]) -> None:
    """The round-trip identity that makes "zero behaviour change" a claim.

    ProductScope keys on nothing: it carries what it is given. _safe_filters
    projects onto SESSION_FILTER_KEYS. For every mapping the live paths can
    actually produce those are the same set, so the two must agree exactly --
    including which empty values disappear.
    """
    assert set(raw).issubset(SESSION_FILTER_KEYS), "fixture drifted from the live key set"

    assert ProductScope.from_filters(raw).as_filters() == _safe_filters(raw)


@pytest.mark.parametrize("raw", _LIVE_SHAPES)
def test_the_where_clause_is_byte_identical(raw: dict[str, Any]) -> None:
    """The retrieval boundary's real output.

    _build_where is what becomes SQL. If the scope and the dict disagree here,
    the candidate set changes, the max cosine changes, and a turn can flip
    between answer and refuse (INV-2) with no other test failing loudly.
    """
    assert _build_where(ProductScope.from_filters(raw).as_filters()) == _build_where(raw)


@pytest.mark.parametrize("raw", _LIVE_SHAPES)
def test_the_retrieval_mode_decision_is_unchanged(raw: dict[str, Any]) -> None:
    """filter_keys/product_pinned select EXACT_SCOPED vs EXACT_CORPUS."""
    from_scope = RetrievalScope.from_filters(ProductScope.from_filters(raw).as_filters())
    from_dict = RetrievalScope.from_filters(raw)

    assert from_scope == from_dict
    assert default_mode_for_scope(from_scope) is default_mode_for_scope(from_dict)


@pytest.mark.parametrize("raw", _LIVE_SHAPES)
def test_casing_is_left_alone_for_the_retriever_to_fold(raw: dict[str, Any]) -> None:
    """Casing stays owned by retriever._fold_filter_casing.

    Pre-folding here would defeat it: a value the scope had already re-cased
    could find no stored match, filter to nothing, and read as an INV-2 refusal
    on a perfectly answerable question. Asserted without a corpus by checking
    the scope hands the fold the same input it gets today -- the fold's early
    return means an unseeded corpus leaves values verbatim.
    """
    scoped = ProductScope.from_filters(raw).as_filters()

    assert _fold_filter_casing(scoped) == _fold_filter_casing(_safe_filters(raw))
    for key in ("dosage_form", "route", "psg_type", "normalized_name"):
        if raw.get(key):
            assert scoped[key] == raw[key], "ProductScope must not transform casing"


def test_it_is_lossless_beyond_the_session_key_set() -> None:
    """The one place ProductScope and _safe_filters deliberately differ.

    _safe_filters projects onto SESSION_FILTER_KEYS because a session row must
    not accumulate arbitrary keys. ProductScope must NOT project: it feeds
    retrieval, which today receives the whole dict. A key the scope silently
    dropped would widen the WHERE clause and could return a wrong-drug passage,
    which is INV-1's failure mode. appl_no is the live example of the divergence
    (in _PRODUCT_FILTER_KEYS, absent from SESSION_FILTER_KEYS); it is pre-existing
    and deliberately not "fixed" here.
    """
    raw = {"normalized_name": "albuterol sulfate", "appl_no": "020503"}

    assert ProductScope.from_filters(raw).as_filters() == raw
    assert "appl_no" not in _safe_filters(raw)
    assert _build_where(ProductScope.from_filters(raw).as_filters()) == _build_where(raw)


def test_non_string_scalars_survive() -> None:
    """The API whitelist admits int/float/bool, so the carrier must too.

    Dropping one on the way to the store would change the WHERE clause. Note 0
    and False are kept: only None, "" and [] are empty, matching _build_where.
    """
    raw: dict[str, Any] = {"normalized_name": "albuterol sulfate", "doc_id": 0, "psg_type": False}

    assert ProductScope.from_filters(raw).as_filters() == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, None),
        ({"normalized_name": "albuterol sulfate"}, "albuterol sulfate"),
        ({"normalized_name": ""}, None),
        ({"normalized_name": None}, None),
        ({"doc_id": "PSG_020503"}, None),
    ],
)
def test_normalized_name_mirrors_the_truthiness_test_it_replaces(
    raw: dict[str, Any], expected: str | None
) -> None:
    """`scope.normalized_name` must equal `filters.get("normalized_name")` truthiness.

    That expression gates product resolution, the social gate, the clarify
    branches and the retrieval scope. A scope that reported a product where the
    dict reported none would change which branch a turn takes.
    """
    scope = ProductScope.from_filters(raw)

    assert scope.normalized_name == expected
    assert scope.is_resolved is bool(raw.get("normalized_name"))


def test_from_filters_accepts_none() -> None:
    """ask(filters=None) is a live shape (every unscoped question)."""
    assert ProductScope.from_filters(None).as_filters() == {}
    assert ProductScope.from_filters(None).is_resolved is False


def test_as_filters_returns_a_fresh_dict_each_call() -> None:
    """The scope is frozen; a caller mutating its output must not corrupt it.

    retrieve() and _fold_filter_casing both copy before mutating today, but the
    guarantee belongs to the carrier, not to its callers' current politeness.
    """
    scope = ProductScope.from_filters({"normalized_name": "albuterol sulfate"})
    first = scope.as_filters()
    first["normalized_name"] = "mutated"

    assert scope.as_filters() == {"normalized_name": "albuterol sulfate"}
