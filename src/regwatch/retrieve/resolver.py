"""Product resolution — entity resolution BEFORE semantic retrieval.

FDA PSG templates reuse the same language across drugs ("Single actuation
content (SAC)", "Fasting, single-dose, two-way crossover" appear verbatim in
many guidances). Embeddings are good at finding the relevant passage *inside*
the right product corpus, but they are NOT a safe product-disambiguation
mechanism: a beclomethasone question will happily retrieve identical albuterol
boilerplate. So we resolve the product first, then constrain retrieval with a
mandatory ``normalized_name`` filter.

Resolution order (highest-confidence first):
  1. an explicit ``normalized_name`` filter from the caller (API / dossier);
  2. active-ingredient names mentioned in the question, matched against the
     products the corpus actually holds (distinct ``normalized_name`` in the
     vector store) by whole-word ingredient match;
  3. single-product-corpus fallback — if the corpus holds exactly one product,
     a product-specific question unambiguously refers to it.

Outcomes: ``resolved`` (one product → filter), ``ambiguous`` (several → the
caller should clarify), ``none`` (nothing resolvable → clarify or refuse).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from regwatch.common.text_normalize import split_ingredients, stripped_name
from regwatch.store.vector_store import distinct_metadata_values


@dataclass
class Resolution:
    status: str  # "resolved" | "ambiguous" | "none"
    normalized_name: str | None = None
    candidates: list[str] = field(default_factory=list)


def _primary_token(ingredient: str) -> str:
    """The leading word of an ingredient's salt-free name (e.g. "albuterol").

    Matching on the primary token tolerates user phrasing — "beclomethasone"
    resolves "beclomethasone dipropionate" — while staying drug-specific:
    "albuterol" never matches "levalbuterol" (a different leading word).
    """
    stripped = stripped_name(ingredient)
    return stripped.split()[0] if stripped.split() else stripped


def _product_tokens(normalized_name: str) -> frozenset[str]:
    """The set of primary ingredient tokens for one product."""
    return frozenset(_primary_token(i) for i in split_ingredients(normalized_name) if i.strip())


def _mentions(token: str, question: str) -> bool:
    return re.search(rf"\b{re.escape(token)}\b", question) is not None


def resolve_product(
    question: str,
    *,
    products: set[str] | None = None,
) -> Resolution:
    """Resolve the single product a question is about, or flag ambiguity.

    ``products`` defaults to the distinct ``normalized_name`` values in the
    vector store (injectable for tests). See module docstring for the contract.
    """
    known = products if products is not None else distinct_metadata_values("normalized_name")
    if not known:
        return Resolution(status="none")

    q = question.lower()
    matched: list[tuple[str, frozenset[str]]] = []
    for name in known:
        tokens = _product_tokens(name)
        if tokens and all(_mentions(tok, q) for tok in tokens):
            matched.append((name, tokens))

    # Prefer the most specific product: drop any whose ingredient set is a strict
    # subset of another match (so "albuterol sulfate and budesonide" resolves to
    # the combo, not the single-ingredient albuterol product).
    maximal = sorted(
        {
            name
            for name, tokens in matched
            if not any(other != name and tokens < other_tokens for other, other_tokens in matched)
        }
    )

    if len(maximal) == 1:
        return Resolution(status="resolved", normalized_name=maximal[0])
    if len(maximal) >= 2:
        return Resolution(status="ambiguous", candidates=maximal)
    # Nothing matched by name.
    if len(known) == 1:
        return Resolution(status="resolved", normalized_name=next(iter(known)))
    return Resolution(status="none", candidates=sorted(known))
