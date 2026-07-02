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
from functools import lru_cache

from rapidfuzz import fuzz

from regwatch.common.text_normalize import canonical_name, split_ingredients, stripped_name
from regwatch.store.vector_store import distinct_metadata_values


@dataclass
class Resolution:
    status: str  # "resolved" | "ambiguous" | "none"
    normalized_name: str | None = None
    candidates: list[str] = field(default_factory=list)
    by_name: bool = False  # True when resolved by an ingredient named in the question
    # (vs. the single-product-corpus fallback). Lets callers tell "user named this
    # drug" apart from "we guessed the only drug we have" — see grounded_qa.


# Topic words that may appear in a question but must never be fuzzy-matched to a
# drug name when offering a "did you mean" suggestion.
_NON_DRUG_WORDS = frozenset(
    {
        "study",
        "studies",
        "design",
        "designs",
        "dissolution",
        "guidance",
        "guidances",
        "bioequivalence",
        "equivalence",
        "recommend",
        "recommended",
        "recommends",
        "strength",
        "strengths",
        "tablet",
        "tablets",
        "capsule",
        "capsules",
        "dosage",
        "dose",
        "doses",
        "method",
        "methods",
        "acceptance",
        "interval",
        "fasting",
        "fed",
        "crossover",
        "waiver",
        "waivers",
        "generic",
        "product",
        "products",
        "what",
        "which",
        "does",
        "should",
        "approach",
        "help",
        "need",
        "about",
        "information",
        "please",
        "with",
        "this",
        "that",
        "drug",
        "fda",
        # Regulatory-process words — a question about filing/strategy names no
        # drug, so these must never be fuzzy-matched to one (else a question
        # like "what submission strategy to file the ANDA?" mis-resolves to a
        # real product and clarifies instead of refusing — INV-2).
        "submission",
        "submissions",
        "strategy",
        "strategies",
        "file",
        "filing",
        "anda",
        "supplement",
        "amendment",
        "approval",
        "internal",
        "benchmark",
        "benchmarks",
    }
)


def _primary_token(ingredient: str) -> str:
    """The leading word of an ingredient's salt-free name (e.g. "albuterol").

    Matching on the primary token tolerates user phrasing — "beclomethasone"
    resolves "beclomethasone dipropionate" — while staying drug-specific:
    "albuterol" never matches "levalbuterol" (a different leading word).
    """
    stripped = stripped_name(ingredient)
    return stripped.split()[0] if stripped.split() else stripped


def _product_tokens(normalized_name: str) -> frozenset[str]:
    """The set of primary ingredient tokens for one product.

    Empty primary tokens are dropped. A product whose ingredients are ENTIRELY
    salt / mineral words ("sodium chloride", "potassium chloride", "magnesium
    sulfate; potassium chloride; sodium sulfate") strips to "" (stripped_name
    removes every word), and an empty token whole-word-matches EVERY question
    (``re.search(r"\\b\\b", q)`` always succeeds). Left in, such a product is a
    phantom match for every query, forcing spurious ``ambiguous`` clarifies on
    ordinary drug questions ("atorvastatin" -> clarify among sodium chloride...).
    Mirrors the non-empty-stripped-key guard already in
    ``text_normalize.names_match`` so the two matchers cannot drift.
    """
    return frozenset(
        t for t in (_primary_token(i) for i in split_ingredients(normalized_name) if i.strip()) if t
    )


def _full_ingredient_words(normalized_name: str) -> frozenset[str]:
    """Every salt/hydrate-inclusive word of a product's ingredients, e.g.
    ``{"beclomethasone", "dipropionate", "monohydrate"}``.

    Used only to break a *primary-token* tie: when several products share a
    leading ingredient ("beclomethasone dipropionate" vs. "beclomethasone
    dipropionate monohydrate"), the candidate whose full name the question
    spells out is the unambiguous match. Conservative by construction — a
    salt-free or partial mention spells out no full name, so it narrows
    nothing and the caller still clarifies (never guesses).
    """
    return frozenset(t for t in re.split(r"[^a-z]+", normalized_name.lower()) if len(t) >= 3)


def _mentions(token: str, question: str) -> bool:
    return re.search(rf"\b{re.escape(token)}\b", question) is not None


@lru_cache(maxsize=8)
def _catalog_tokens(known: frozenset[str]) -> tuple[tuple[str, frozenset[str]], ...]:
    """Per-product primary tokens, computed once per distinct catalog content.

    resolve_product runs on every unpinned question (and more than once per
    request via the scope-warning and meta branches); retokenizing the full
    ~1.8k-product catalog each call is pure repeated CPU. The catalog only
    changes when ingest adds chunks, so keying on the frozen contents of the
    (already TTL-cached) distinct_metadata_values set stays correct across
    catalog updates while the tiny maxsize bounds memory.
    """
    return tuple((name, _product_tokens(name)) for name in known)


# Explicit comparison/contrast markers. A question that compares products names
# more than one ON PURPOSE; since we answer about a single product at a time
# (the cross-drug guard), an explicit comparison must clarify which — never
# silently collapse two products into one. "and"/"with" are deliberately NOT
# markers: "albuterol sulfate and budesonide" is one combination product.
_COMPARISON_TOKENS = frozenset({"compare", "comparison", "versus", "vs"})
_COMPARISON_PHRASES = (
    "difference between",
    "differences between",
    "compared to",
    "compared with",
)


def _is_comparison(q: str) -> bool:
    """True if the (already-lowercased) question explicitly compares products."""
    if any(phrase in q for phrase in _COMPARISON_PHRASES):
        return True
    return any(re.search(rf"\b{re.escape(tok)}\b", q) for tok in _COMPARISON_TOKENS)


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
    matched: list[tuple[str, frozenset[str]]] = [
        (name, tokens)
        for name, tokens in _catalog_tokens(frozenset(known))
        if tokens and all(_mentions(tok, q) for tok in tokens)
    ]

    # AND/conjunction validation: an EXPLICIT comparison naming 2+ in-corpus
    # products must clarify — even when one product's ingredient set is a strict
    # subset of another's (where the subset-collapse below would otherwise pick
    # the superset). Checked before that collapse for exactly this reason.
    distinct_matched = sorted({name for name, _ in matched})
    if _is_comparison(q) and len(distinct_matched) >= 2:
        return Resolution(status="ambiguous", candidates=distinct_matched)

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
        return Resolution(status="resolved", normalized_name=maximal[0], by_name=True)
    if len(maximal) >= 2:
        # Tie-break: if the question spells out the full ingredient name of
        # exactly one candidate (salts/hydrates included), it unambiguously
        # means that product — resolve it instead of asking.
        specific = [n for n in maximal if all(_mentions(w, q) for w in _full_ingredient_words(n))]
        if len(specific) == 1:
            return Resolution(status="resolved", normalized_name=specific[0], by_name=True)
        return Resolution(status="ambiguous", candidates=maximal)
    # Nothing matched by name.
    if len(known) == 1:
        # Single-product corpus fallback: NOT a name match, so callers should not
        # treat a downstream non-answer as "vague question about a named drug".
        return Resolution(status="resolved", normalized_name=next(iter(known)), by_name=False)
    return Resolution(status="none", candidates=sorted(known))


def _drug_like_tokens(question: str) -> list[str]:
    """Question words that could be a drug name (≥4 chars, not topic words)."""
    return [
        t
        for t in re.split(r"[^a-z]+", question.lower())
        if len(t) >= 4 and t not in _NON_DRUG_WORDS
    ]


def suggest_products(
    question: str,
    *,
    products: set[str] | None = None,
    threshold: int = 82,
    limit: int = 3,
) -> list[str]:
    """Fuzzy "did you mean" for a question that named no in-corpus product.

    Compares the question's drug-like tokens against each product's primary
    (salt-free leading) token. ``threshold`` 82 catches genuine typos
    ("propranlol"→propranolol 95, "amphetmain"→amphetamine 85) while leaving a
    wide margin over genuinely-absent drugs (romidepsin's nearest match is ~60),
    which therefore yield no suggestion and still refuse. A suggestion always
    *asks*; it never silently substitutes a different drug.
    """
    known = products if products is not None else distinct_metadata_values("normalized_name")
    if not known:
        return []
    candidates = _drug_like_tokens(question)
    if not candidates:
        return []
    scored: dict[str, int] = {}
    for name in known:
        primary = _primary_token(name)
        best = max((int(fuzz.ratio(tok, primary)) for tok in candidates), default=0)
        if best >= threshold:
            scored[name] = max(scored.get(name, 0), best)
    return [name for name, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)][:limit]


def resolve_brand(question: str, *, products: set[str] | None = None, limit: int = 5) -> list[str]:
    """Map a brand name in the question to in-corpus generic ingredient(s).

    Brand names (e.g. "Adderall") are not in the PSG corpus, which is keyed by
    generic ingredient. We ask openFDA Drugs@FDA for the brand's ``generic_name``
    and return the corpus products that share an ingredient — to OFFER as "did you
    mean", never to auto-answer. Gated on an OpenFDA key (so tests stay offline)
    and graceful: any error / no match returns ``[]`` and the caller refuses.
    """
    from config.settings import get_settings

    if not get_settings().openfda_api_key:
        return []
    known = products if products is not None else distinct_metadata_values("normalized_name")
    if not known:
        return []
    candidates = _drug_like_tokens(question)
    if not candidates:
        return []
    try:
        from regwatch.sources._utils import fetch_openfda_results

        rows = fetch_openfda_results(
            "https://api.fda.gov/drug/drugsfda.json",
            [f'openfda.brand_name:"{tok}"' for tok in candidates],
            limit=5,
        )
    except Exception:  # offline / rate-limited / malformed — degrade to no match
        return []
    generics: set[str] = set()
    for row in rows:
        openfda = row.get("openfda") or {}
        for name in openfda.get("generic_name") or []:
            generics.add(str(name).lower())
    if not generics:
        return []
    known_tokens = {name: _product_tokens(name) for name in known}
    matches: set[str] = set()
    for generic in generics:
        gtokens = {
            t for ing in split_ingredients(canonical_name(generic)) for t in _product_tokens(ing)
        }
        if not gtokens:
            continue
        for name, ntokens in known_tokens.items():
            if gtokens & ntokens:
                matches.add(name)
    return sorted(matches)[:limit]
