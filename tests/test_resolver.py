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
    # Also the negative control for the comparison-marker rule in
    # test_resolution_hardening.py: "and" is NOT a marker, so a plain combo
    # question still resolves to the combo rather than clarifying.
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


# A corpus that mixes real drugs with pure salt/mineral products. The latter
# strip to an empty primary token; before the _product_tokens empty-token guard
# they whole-word-matched EVERY question and forced spurious ambiguity. These
# mirror the live prod catalog (sodium chloride / potassium chloride / the
# bowel-prep electrolyte combo all sit alongside atorvastatin/amlodipine).
JUNK_CORPUS = {
    "atorvastatin calcium",
    "atorvastatin calcium; ezetimibe",
    "amlodipine besylate",
    "amlodipine benzoate",
    "sodium chloride",
    "potassium chloride",
    "magnesium sulfate; potassium chloride; sodium sulfate",
}


def test_base_ingredient_resolves_past_salt_only_phantom_products() -> None:
    # "atorvastatin" must resolve to atorvastatin calcium, NOT clarify among the
    # salt-only electrolyte products (the live atorvastatin junk-clarify bug).
    r = resolve_product(
        "What BE study design does FDA recommend for atorvastatin?", products=JUNK_CORPUS
    )
    assert r.status == "resolved"
    assert r.normalized_name == "atorvastatin calcium"


def test_real_salt_forms_clarify_without_salt_only_junk() -> None:
    # Two genuine amlodipine salt forms -> clarify between THOSE two only; the
    # electrolyte products must never appear as candidates.
    r = resolve_product("amlodipine dissolution method", products=JUNK_CORPUS)
    assert r.status == "ambiguous"
    assert set(r.candidates) == {"amlodipine besylate", "amlodipine benzoate"}


def test_salt_only_products_never_phantom_match() -> None:
    # A query naming no real product must reach `none` (so the brand / did-you-mean
    # path can run) instead of being polluted into a junk `ambiguous`.
    r = resolve_product("Eliquis study design", products=JUNK_CORPUS)
    assert r.status == "none"


def test_catalog_tokenization_cached_per_catalog_content() -> None:
    # Tokenizing the full catalog (plus one regex per product) is per-query CPU;
    # it must run once per distinct catalog content, not once per resolve call.
    from regwatch.retrieve import resolver

    resolver._catalog_tokens.cache_clear()
    resolve_product("levalbuterol tartrate study design?", products=CORPUS)
    resolve_product("beclomethasone dipropionate study design?", products=CORPUS)
    info = resolver._catalog_tokens.cache_info()
    assert info.misses == 1
    assert info.hits >= 1

    # A changed catalog (e.g. new ingest) is a new cache entry -- never stale
    # tokens: the newly added product resolves immediately.
    r = resolve_product("romidepsin study design?", products=CORPUS | {"romidepsin"})
    assert r.status == "resolved"
    assert r.normalized_name == "romidepsin"
    assert resolver._catalog_tokens.cache_info().misses == 2


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
