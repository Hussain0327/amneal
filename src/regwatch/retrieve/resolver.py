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

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from regwatch.common.text_normalize import canonical_name, split_ingredients, stripped_name
from regwatch.store.vector_store import distinct_metadata_values


# region agent log
_AGENT_DEBUG_LOG_PATH = Path("/Users/hussain/amneal/.cursor/debug-04b0f1.log")


def _agent_debug_log(
    *,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
) -> None:
    payload = {
        "sessionId": "04b0f1",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        _AGENT_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AGENT_DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        pass


# endregion


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
        return Resolution(status="resolved", normalized_name=maximal[0], by_name=True)
    if len(maximal) >= 2:
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
            # region agent log
            _agent_debug_log(
                run_id="initial",
                hypothesis_id="H6",
                location="src/regwatch/retrieve/resolver.py:278",
                message="brand_generic_product_token_match",
                data={
                    "generic_tokens": sorted(gtokens),
                    "product": name,
                    "product_tokens": sorted(ntokens),
                    "overlap_tokens": sorted(gtokens & ntokens),
                    "generic_covers_product": bool(ntokens <= gtokens),
                    "current_logic_matches": bool(gtokens & ntokens),
                },
            )
            # endregion
            if gtokens & ntokens:
                matches.add(name)
    return sorted(matches)[:limit]
