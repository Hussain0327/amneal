"""Centralized citation grammar.

One module defines how inline citations are written and parsed, so the
generator (which *validates* citations against retrieved passages) and the
eval metrics (which *score* them) can never drift apart.

A PSG citation looks like ``[PSG_020503, p.3]``. The grammar also accepts
**compound** citations — several sources in one bracket separated by ``;`` —
which the LLM commonly emits, e.g. ``[PSG_020503, p.4; PSG_021730, p.4]``.
The previous single-pair regex dropped these entirely, which zeroed out
citation precision on the eval. Every consumer must go through this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

# A single source token inside a bracket: short_name + page, e.g. "PSG_020503, p.4".
# Keep the token source-shaped so prose like "[Table 1, p.3]" is not treated as
# a citation and stripped from the answer.
_PAIR = re.compile(r"((?:PSG_|OB_)?\d{3,})\s*,\s*p\.\s*(\d+)", re.IGNORECASE)

# Any bracketed run with no nested brackets. We treat a bracket as a citation
# only if it contains at least one _PAIR, so prose like "[see appendix]" is
# left untouched.
_BRACKET = re.compile(r"\[([^\[\]]+)\]")


def _pairs_in(body: str) -> list[tuple[str, int]]:
    """Every (short_name, page) pair inside one bracket body (compound-aware)."""
    return [(m.group(1), int(m.group(2))) for m in _PAIR.finditer(body)]


def iter_psg_citations(text: str) -> Iterator[tuple[str, int]]:
    """Yield (short_name, page) for every citation in ``text``, in order.

    Splits compound brackets like ``[A, p.1; B, p.2]`` into separate pairs.
    """
    for bracket in _BRACKET.finditer(text):
        yield from _pairs_in(bracket.group(1))


def has_citation(text: str) -> bool:
    """True if ``text`` contains at least one citation marker."""
    return any(_pairs_in(b.group(1)) for b in _BRACKET.finditer(text))


def strip_all_citations(text: str) -> str:
    """Remove every citation marker (keeps non-citation brackets intact)."""

    def repl(m: re.Match[str]) -> str:
        return "" if _pairs_in(m.group(1)) else m.group(0)

    return _BRACKET.sub(repl, text)


def filter_citations(text: str, allowed: set[tuple[str, int]]) -> str:
    """Rewrite every citation marker to keep only pairs in ``allowed``.

    A bracket retaining >=1 allowed pair is rewritten with just those pairs
    (canonical ``[name, p.N; ...]`` form); a bracket whose pairs are all
    disallowed is removed entirely. Non-citation brackets are left untouched.
    Used to strip fabricated citations from a rendered answer (INV-1).
    """

    def repl(m: re.Match[str]) -> str:
        pairs = _pairs_in(m.group(1))
        if not pairs:
            return m.group(0)
        kept = [(s, p) for (s, p) in pairs if (s, p) in allowed]
        if not kept:
            return ""
        return "[" + "; ".join(f"{s}, p.{p}" for (s, p) in kept) + "]"

    return _BRACKET.sub(repl, text)


# ---------------------------------------------------------------------------
# Structured source-token grammar (INV-8).
#
# A White-Paper cell that cites a structured FDA row uses one of four token
# shapes instead of the ``[short_name, p.N]`` PSG/SPL-page form:
#
#   SPL_{setid}#{loinc}      a DailyMed SPL LOINC-coded section
#   OB_{applno}/{productno}  an Orange Book Products.txt product row
#   OBPAT_{patentno}         an Orange Book patent.txt row
#   OBEXCL_{code}            an Orange Book exclusivity.txt row
#
# ``validate_structured_citations`` is the INV-8 guard: a token is honored ONLY
# when it both parses AND is backed by a row the populator actually fetched. Any
# token outside that set is dropped, and its cell collapses to
# ``analyst_input_required`` — the structured analogue of the PSG-citation
# stripping above. The grammar is deliberately distinct from ``_PAIR`` so the
# two citation worlds never collide.
# ---------------------------------------------------------------------------

_SPL_TOKEN = re.compile(r"^SPL_(?P<setid>[A-Za-z0-9._-]+)#(?P<loinc>[0-9]+-[0-9])$")
_OB_TOKEN = re.compile(r"^OB_(?P<applno>\d{6})/(?P<productno>\d{1,4})$")
_OBPAT_TOKEN = re.compile(r"^OBPAT_(?P<patentno>[A-Za-z0-9,*-]+)$")
_OBEXCL_TOKEN = re.compile(r"^OBEXCL_(?P<code>[A-Za-z0-9*-]+)$")


@dataclass(frozen=True)
class StructuredCitation:
    """A parsed structured source token: ``kind`` plus its identifying parts."""

    kind: str  # "spl" | "ob" | "obpat" | "obexcl"
    parts: tuple[str, ...]
    token: str


def spl_token(setid: str, loinc: str) -> str:
    return f"SPL_{setid}#{loinc}"


def ob_token(application_number: str, product_number: str) -> str:
    return f"OB_{application_number}/{product_number}"


def obpat_token(patent_no: str) -> str:
    return f"OBPAT_{patent_no}"


def obexcl_token(exclusivity_code: str) -> str:
    return f"OBEXCL_{exclusivity_code}"


def parse_structured_token(token: str) -> StructuredCitation | None:
    """Parse one structured token, or return ``None`` if it is not one.

    Plain prose and ``[short_name, p.N]`` locators return ``None`` — they are
    handled by the PSG-page grammar above, not by the structured validator.
    """
    raw = token.strip()
    if (m := _SPL_TOKEN.match(raw)) is not None:
        return StructuredCitation("spl", (m.group("setid"), m.group("loinc")), raw)
    if (m := _OB_TOKEN.match(raw)) is not None:
        return StructuredCitation("ob", (m.group("applno"), m.group("productno")), raw)
    if (m := _OBPAT_TOKEN.match(raw)) is not None:
        return StructuredCitation("obpat", (m.group("patentno"),), raw)
    if (m := _OBEXCL_TOKEN.match(raw)) is not None:
        return StructuredCitation("obexcl", (m.group("code"),), raw)
    return None


def is_structured_token(token: str) -> bool:
    """True if ``token`` is a structured source token (not a PSG-page locator)."""
    return parse_structured_token(token) is not None


def validate_structured_citations(
    tokens: Sequence[str],
    known: set[str],
) -> tuple[list[str], list[str]]:
    """Split ``tokens`` into (valid, invalid) against the fetched-row set.

    A token is valid iff it parses as a structured token AND is present in
    ``known`` (the set of tokens for rows actually fetched this run). Anything
    else — malformed, or fabricated/unfetched — is invalid. Order preserved;
    duplicates de-duped within each bucket so a caller can collapse cleanly.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()
    for token in tokens:
        raw = token.strip()
        if parse_structured_token(raw) is not None and raw in known:
            if raw not in seen_valid:
                seen_valid.add(raw)
                valid.append(raw)
        elif raw not in seen_invalid:
            seen_invalid.add(raw)
            invalid.append(raw)
    return valid, invalid
