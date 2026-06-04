"""Product resolver: resolve / clarify / refuse before retrieval."""

from __future__ import annotations

from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.resolver import resolve_product
from regwatch.store.vector_store import add_chunks, distinct_metadata_values

CORPUS = {
    "albuterol sulfate",
    "albuterol sulfate; budesonide",
    "beclomethasone dipropionate",
    "levalbuterol tartrate",
}


def test_resolves_distinct_drug() -> None:
    r = resolve_product(
        "What study design does the levalbuterol tartrate PSG recommend?", products=CORPUS
    )
    assert r.status == "resolved"
    assert r.normalized_name == "levalbuterol tartrate"


def test_albuterol_does_not_resolve_to_levalbuterol() -> None:
    # "albuterol" is a substring of "levalbuterol" but not a whole word.
    r = resolve_product(
        "What study design is recommended for albuterol sulfate inhalation aerosol?",
        products=CORPUS,
    )
    assert r.status == "resolved"
    assert r.normalized_name == "albuterol sulfate"


def test_combo_wins_over_single_ingredient() -> None:
    r = resolve_product("What does the albuterol sulfate and budesonide PSG say?", products=CORPUS)
    assert r.status == "resolved"
    assert r.normalized_name == "albuterol sulfate; budesonide"


def test_beclomethasone_resolves_one_product_key() -> None:
    # Two beclomethasone PSGs share one normalized_name → one product key.
    r = resolve_product(
        "What type of study does the beclomethasone dipropionate PSG recommend?",
        products=CORPUS,
    )
    assert r.status == "resolved"
    assert r.normalized_name == "beclomethasone dipropionate"


def test_two_unrelated_drugs_are_ambiguous() -> None:
    r = resolve_product("Compare albuterol and beclomethasone studies", products=CORPUS)
    assert r.status == "ambiguous"
    assert "albuterol sulfate" in r.candidates
    assert "beclomethasone dipropionate" in r.candidates


def test_no_product_named_is_unresolved() -> None:
    r = resolve_product("What bioequivalence study should we run?", products=CORPUS)
    assert r.status == "none"


def test_single_product_corpus_resolves_without_name() -> None:
    r = resolve_product("What study design is recommended?", products={"albuterol sulfate"})
    assert r.status == "resolved"
    assert r.normalized_name == "albuterol sulfate"


def test_empty_corpus_is_none() -> None:
    assert resolve_product("anything", products=set()).status == "none"


def test_distinct_metadata_cache_invalidates_on_add_chunks() -> None:
    embedder = get_embedding_provider()
    add_chunks(
        ids=["a"],
        embeddings=embedder.embed(["alpha"]),
        documents=["alpha"],
        metadatas=[{"normalized_name": "albuterol sulfate"}],
    )
    assert distinct_metadata_values("normalized_name") == {"albuterol sulfate"}

    add_chunks(
        ids=["b"],
        embeddings=embedder.embed(["beta"]),
        documents=["beta"],
        metadatas=[{"normalized_name": "beclomethasone dipropionate"}],
    )
    assert distinct_metadata_values("normalized_name") == {
        "albuterol sulfate",
        "beclomethasone dipropionate",
    }
