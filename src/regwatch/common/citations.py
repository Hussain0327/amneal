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
from collections.abc import Iterator

# A single source token inside a bracket: short_name + page, e.g. "PSG_020503, p.4".
_PAIR = re.compile(r"([A-Za-z0-9_./-]+)\s*,\s*p\.\s*(\d+)", re.IGNORECASE)

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
