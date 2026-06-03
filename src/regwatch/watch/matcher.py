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


FUZZY_THRESHOLD = 88  # rapidfuzz token_sort_ratio; tuned conservatively


@dataclass
class WatchMatch:
    listing: PsgListing
    product: dict[str, Any]
    confidence: float
    rationale: str  # "canonical" | "stripped" | "fuzzy" | "combo_component"


def _index_watchlist(products: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build a (key -> products) index for fast canonical/stripped lookup."""
    index: dict[str, list[dict[str, Any]]] = {}
    for p in products:
        canon = canonical_name(p.get("active_ingredient", ""))
        strip = stripped_name(p.get("active_ingredient", ""))
        for key in {canon, strip}:
            if not key:
                continue
            index.setdefault(key, []).append(p)
    return index


def match_listings(listings: list[PsgListing], products: list[dict[str, Any]]) -> list[WatchMatch]:
    """Return all matches between listings and products."""
    if not products:
        return []
    index = _index_watchlist(products)

    matches: list[WatchMatch] = []
    for li in listings:
        listing_canon = li.normalized_name
        listing_strip = li.stripped_name

        # 1. Canonical exact match
        if listing_canon in index:
            for prod in index[listing_canon]:
                matches.append(
                    WatchMatch(listing=li, product=prod, confidence=1.0, rationale="canonical")
                )
            continue

        # 2. Stripped exact match
        if listing_strip in index:
            for prod in index[listing_strip]:
                matches.append(
                    WatchMatch(listing=li, product=prod, confidence=0.92, rationale="stripped")
                )
            continue

        # 3. Combo component match: if the listing is a combination and ANY of
        # its component names matches a watchlist product, flag it.
        if is_combo(li.active_ingredient):
            comps = split_ingredients(li.active_ingredient)
            hit = False
            for comp in comps:
                comp_strip = stripped_name(comp)
                if comp_strip in index:
                    for prod in index[comp_strip]:
                        matches.append(
                            WatchMatch(
                                listing=li,
                                product=prod,
                                confidence=0.80,
                                rationale="combo_component",
                            )
                        )
                        hit = True
            if hit:
                continue

        # 4. Fuzzy match (rapidfuzz token_sort_ratio) — conservative threshold.
        best_score: float = 0.0
        best_prod: dict[str, Any] | None = None
        for prod in products:
            for key in (
                canonical_name(prod.get("active_ingredient", "")),
                stripped_name(prod.get("active_ingredient", "")),
            ):
                if not key:
                    continue
                score = fuzz.token_sort_ratio(listing_strip, key)
                if score > best_score:
                    best_score = score
                    best_prod = prod
        if best_prod is not None and best_score >= FUZZY_THRESHOLD:
            matches.append(
                WatchMatch(
                    listing=li,
                    product=best_prod,
                    confidence=best_score / 100.0,
                    rationale="fuzzy",
                )
            )

    log.info("match_done", listings=len(listings), products=len(products), matches=len(matches))
    return matches
