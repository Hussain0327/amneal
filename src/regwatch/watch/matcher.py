"""Match PSG listings against the company watchlist.

Strategy (in priority order):
  1. Exact canonical-name match.
  2. Exact stripped-name match (drops salt/hydrate tokens).
  3. Fuzzy match (rapidfuzz) on stripped names, with a minimum score threshold.
  4. For multi-ingredient combos: every component on the watchlist counts.

The matcher returns *all* matches, with a confidence ∈ [0, 1] and the
rationale (which key matched), so downstream alert logic can decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import (
    canonical_name,
    is_combo,
    split_ingredients,
    stripped_name,
)
from regwatch.ingest.psg_crawler import PsgListing

log = get_logger(__name__)


# rapidfuzz token_sort_ratio floor. Raised 88 -> 92 because 88 admitted
# distinct near-name generics as false matches (prednisone<->prednisolone scores
# 90.9). No string metric cleanly separates such pairs, so this is incremental
# hardening, not a complete guard; legitimate spelling/spacing variants match via
# the canonical/stripped exact keys above, and real typos still clear 92.
FUZZY_THRESHOLD = 92


@dataclass
class WatchMatch:
    listing: PsgListing
    product: dict[str, Any]
    confidence: float
    rationale: str  # "canonical" | "stripped" | "fuzzy" | "combo_component"


def product_id(m: WatchMatch) -> int | None:
    """The match's watchlist product id, or None when it carries no int id.

    Single home for the "non-int product id => not alertable/pairable" rule:
    run.py's missed-pair recovery and alerts.py's build_alerts must apply the
    SAME coercion or the INV-4 re-surfacing check drifts from alert emission.
    """
    pid = m.product.get("id")
    return pid if isinstance(pid, int) else None


def _norm_attr(v: Any) -> str:
    """Lowercased, whitespace-collapsed attribute for lenient route/form compare."""
    if not isinstance(v, str):
        return ""
    return " ".join(v.strip().lower().split())


def _attr_compatible(listing_val: str | None, product_val: Any) -> bool:
    """True if a listing's route/dosage_form is compatible with a product's.

    Compatible when EITHER side is unknown (null/empty) or one normalized value
    is a prefix of the other ("Tablet" vs "Tablet, Extended Release"). The
    null-fallback is deliberate: a missing form/route must never DROP a real
    match (INV-4 spirit -- prefer an over-alert to a silent miss); it only
    narrows the fan-out when both sides positively disagree.
    """
    a = _norm_attr(listing_val)
    b = _norm_attr(product_val)
    if not a or not b:
        return True
    return a == b or a.startswith(b) or b.startswith(a)


def _form_route_compatible(listing: PsgListing, product: dict[str, Any]) -> bool:
    """Gate a name match by dosage-form/route so one PSG does not fan out to every
    same-ingredient product the company holds across unrelated forms."""
    return _attr_compatible(listing.route, product.get("route")) and _attr_compatible(
        listing.dosage_form, product.get("dosage_form")
    )


def _index_watchlist(
    products: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Build (canonical key -> products, stripped key -> products) indexes.

    Two maps, not one merged dict: the emitted rationale/confidence must record
    WHICH product-side key was hit. A merged index let a listing whose canonical
    string equaled only a product's STRIPPED key claim 'canonical'/1.0 -- a
    salt-preserving exact match that never happened -- in the durable alert row.
    """
    canonical_index: dict[str, list[dict[str, Any]]] = {}
    stripped_index: dict[str, list[dict[str, Any]]] = {}
    for p in products:
        canon = canonical_name(p.get("active_ingredient", ""))
        strip = stripped_name(p.get("active_ingredient", ""))
        if canon:
            canonical_index.setdefault(canon, []).append(p)
        # An all-salt-token name ("Potassium Chloride") strips to "": never
        # index the empty key, or distinct electrolytes would cross-match.
        if strip:
            stripped_index.setdefault(strip, []).append(p)
    return canonical_index, stripped_index


def match_listings(listings: list[PsgListing], products: list[dict[str, Any]]) -> list[WatchMatch]:
    """Return all matches between listings and products."""
    if not products:
        return []
    canonical_index, stripped_index = _index_watchlist(products)

    matches: list[WatchMatch] = []
    for li in listings:
        listing_canon = li.normalized_name
        listing_strip = li.stripped_name

        # Each name-match branch below short-circuits (`continue`) only when it
        # EMITTED at least one match. A name-key hit whose whole bucket was then
        # rejected by the form/route gate must fall through: a differently-keyed
        # but form-compatible product may still be reachable via a later branch,
        # and silencing it by control flow would be a silent miss (INV-4 spirit
        # -- the gate only filters WHICH same-named products alert; it must not
        # veto the remaining strategies for the listing).

        # 1. Canonical exact match. Provenance follows the PRODUCT-side key that
        # was hit: only canonical==canonical is 'canonical'/1.0; a hit on a
        # product's stripped key dropped salt tokens, so it is 'stripped'/0.92
        # even though the listing's canonical string found it.
        emitted = False
        seen: set[int] = set()  # id() of product dicts already handled for li
        for prod in canonical_index.get(listing_canon, []):
            seen.add(id(prod))
            if _form_route_compatible(li, prod):
                matches.append(
                    WatchMatch(listing=li, product=prod, confidence=1.0, rationale="canonical")
                )
                emitted = True
        for prod in stripped_index.get(listing_canon, []):
            if id(prod) in seen:
                continue
            seen.add(id(prod))
            if _form_route_compatible(li, prod):
                matches.append(
                    WatchMatch(listing=li, product=prod, confidence=0.92, rationale="stripped")
                )
                emitted = True
        if emitted:
            continue

        # 2. Stripped exact match: the listing side dropped salt tokens, so any
        # hit here is 'stripped'/0.92 regardless of which product key matched.
        emitted = False
        seen = set()
        for idx in (canonical_index, stripped_index):
            for prod in idx.get(listing_strip, []):
                if id(prod) in seen:
                    continue
                seen.add(id(prod))
                if _form_route_compatible(li, prod):
                    matches.append(
                        WatchMatch(listing=li, product=prod, confidence=0.92, rationale="stripped")
                    )
                    emitted = True
        if emitted:
            continue

        # 3. Combo component match: if the listing is a combination and ANY of
        # its component names matches a watchlist product, flag it.
        if is_combo(li.active_ingredient):
            emitted = False
            seen = set()
            for comp in split_ingredients(li.active_ingredient):
                # Check BOTH component keys: an all-salt-token component
                # ("Potassium Chloride") strips to "", so its canonical key is
                # the only exact handle on the matching watchlist product. The
                # canonical key is salt-preserving, so it cannot reintroduce the
                # cross-electrolyte collapse the empty-strip guard prevents.
                for comp_key in (canonical_name(comp), stripped_name(comp)):
                    if not comp_key:
                        continue
                    for idx in (canonical_index, stripped_index):
                        for prod in idx.get(comp_key, []):
                            if id(prod) in seen:
                                continue
                            seen.add(id(prod))
                            if _form_route_compatible(li, prod):
                                matches.append(
                                    WatchMatch(
                                        listing=li,
                                        product=prod,
                                        confidence=0.80,
                                        rationale="combo_component",
                                    )
                                )
                                emitted = True
            if emitted:
                continue

        # 4. Fuzzy match (rapidfuzz token_sort_ratio) — conservative threshold.
        fuzzy_hits: dict[int, tuple[dict[str, Any], float]] = {}
        fallback_hits: list[tuple[dict[str, Any], float]] = []
        for prod in products:
            prod_best = 0.0
            for key in (
                canonical_name(prod.get("active_ingredient", "")),
                stripped_name(prod.get("active_ingredient", "")),
            ):
                if not key:
                    continue
                score = fuzz.token_sort_ratio(listing_strip, key)
                prod_best = max(prod_best, float(score))
            if prod_best < FUZZY_THRESHOLD:
                continue
            prod_id = prod.get("id")
            if isinstance(prod_id, int):
                prior = fuzzy_hits.get(prod_id)
                if prior is None or prod_best > prior[1]:
                    fuzzy_hits[prod_id] = (prod, prod_best)
            else:
                fallback_hits.append((prod, prod_best))
        for prod, score in [*fuzzy_hits.values(), *fallback_hits]:
            if not _form_route_compatible(li, prod):
                continue
            matches.append(
                WatchMatch(
                    listing=li,
                    product=prod,
                    confidence=score / 100.0,
                    rationale="fuzzy",
                )
            )

    log.info("match_done", listings=len(listings), products=len(products), matches=len(matches))
    return matches
