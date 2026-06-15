"""Active-ingredient normalization.

Generic drug names show up with wildly different formats across sources:
  "Albuterol Sulfate"
  "ALBUTEROL SULFATE"
  "albuterol sulfate (anhydrous)"
  "Hydrocodone Bitartrate; Acetaminophen"
  "HYDROCODONE BITARTRATE AND ACETAMINOPHEN"

We normalize to: lowercased, salt-stripped, sorted parts joined by '; '. This
is one of the system's most failure-prone surfaces (see §14 risk: name
matching) so it has a dedicated module + tests.
"""

from __future__ import annotations

import re

# Common salt / hydrate suffixes to strip when matching by "base" name. We do
# this *softly* — the canonical name keeps the salt form; the stripped name is
# only used as a secondary lookup key.
SALT_TOKENS = {
    "hydrochloride",
    "hcl",
    "sulfate",
    "sulphate",
    "bitartrate",
    "tartrate",
    "citrate",
    "phosphate",
    "fumarate",
    "succinate",
    "mesylate",
    "dimesylate",
    "tosylate",
    "besylate",
    "sodium",
    "potassium",
    "calcium",
    "magnesium",
    "monohydrate",
    "dihydrate",
    "trihydrate",
    "anhydrous",
}


_SEP_RE = re.compile(r"[;/]|(?:\s+and\s+)|(?:\s*,\s*)", flags=re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_WS_RE = re.compile(r"\s+")


def split_ingredients(raw: str) -> list[str]:
    """Split a possibly multi-ingredient string into individual ingredient tokens."""
    parts = [p.strip() for p in _SEP_RE.split(raw) if p and p.strip()]
    return parts


def canonical_name(raw: str) -> str:
    """Canonical form: lowercased, whitespace-collapsed, salt-preserving, sorted parts.

    Multi-ingredient combos are sorted so that "A; B" and "B; A" canonicalize
    identically.
    """
    parts = []
    for part in split_ingredients(raw):
        cleaned = _PAREN_RE.sub(" ", part)
        cleaned = _WS_RE.sub(" ", cleaned).strip().lower()
        if cleaned:
            parts.append(cleaned)
    parts.sort()
    return "; ".join(parts)


def stripped_name(raw: str) -> str:
    """Salt-stripped lowercase form used as a secondary lookup key.

    "albuterol sulfate" -> "albuterol"
    "hydrocodone bitartrate; acetaminophen" -> "acetaminophen; hydrocodone"
    """
    parts = []
    for part in split_ingredients(raw):
        cleaned = _PAREN_RE.sub(" ", part)
        cleaned = _WS_RE.sub(" ", cleaned).strip().lower()
        if not cleaned:
            continue
        tokens = [t for t in cleaned.split(" ") if t and t not in SALT_TOKENS]
        if tokens:
            parts.append(" ".join(tokens))
    parts.sort()
    return "; ".join(parts)


def is_combo(raw: str) -> bool:
    """True if the raw name represents a multi-ingredient combination."""
    return len(split_ingredients(raw)) > 1


def names_match(
    query_canon: str,
    query_strip: str,
    doc_normalized_name: str | None,
    doc_active_ingredient: str | None,
) -> bool:
    """True iff a query's canonical/stripped ingredient identifies the document.

    Exact canonical match first; the salt-stripped branch only fires for a
    NON-EMPTY stripped key, so two products that are *entirely* salt/mineral
    tokens — ``stripped_name("Magnesium Sulfate") == stripped_name("Calcium
    Citrate") == ""`` — never collapse into one match. Shared by the dossier
    and white-paper PSG matchers so the cross-product guard (INV-7..9) cannot
    drift between them.
    """
    if not query_canon:
        return False
    if (doc_normalized_name or "") == query_canon:
        return True
    return bool(query_strip) and stripped_name(doc_active_ingredient or "") == query_strip
