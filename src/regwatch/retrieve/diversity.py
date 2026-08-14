"""Evidence diversity: MMR selection over an already-retrieved pool.

Top-K by score alone returns near-duplicates. Eight chunks from one paragraph
of one guidance are one piece of evidence dressed as eight, and they crowd out
the distinct passage a multi-aspect question needs. Maximal Marginal Relevance
keeps the same COUNT but charges a candidate for overlapping what is already
selected (docs/DSA.md section 33)::

    next = argmax over remaining d of
        lambda * rel(d) - (1 - lambda) * max sim(d, s) for s in selected

Deliberate scoping: ``sim`` here is token-set Jaccard over the chunk text, not
cosine over embeddings. Diversifying must not cost a second embedding call or a
second query, so this module does no I/O, imports nothing from ``store``, and
is exercised with two floats and two strings per passage.

``rel`` is the passage's own score, which the pipeline produces on the cosine
scale. One interaction worth knowing before an operator turns both knobs on:
the optional cross-encoder reranker reorders the pool by a score on a different
scale than the one it leaves on ``.score``, so MMR after it re-ranks on cosine.
Both are off by default.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Protocol, TypeVar

# "Alphanumeric" the unicode way: ``\W`` is everything outside a word
# character, and adding ``_`` to the class drops the one non-alphanumeric
# character ``\w`` keeps. Splitting on runs of these leaves bare tokens.
_NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)


class ScoredPassage(Protocol):
    """The only two fields MMR reads off a retrieved passage."""

    text: str
    score: float


_P = TypeVar("_P", bound=ScoredPassage)


def _token_set(text: str) -> frozenset[str]:
    """Returns the casefolded alphanumeric tokens of `text`, deduplicated."""
    return frozenset(tok for tok in _NON_ALNUM_RE.split(text.casefold()) if tok)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Returns token-set overlap in [0, 1]; 0.0 if either side is empty."""
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if not shared:
        return 0.0
    return shared / len(a | b)


def mmr_select(passages: Sequence[_P], k: int, lambda_: float = 0.7) -> list[_P]:
    """Selects k passages that are relevant AND unlike one another.

    Pure and deterministic: the result depends only on the arguments, and ties
    are broken by a candidate's position in `passages`, so a rerun of the same
    pool selects the same passages in the same order.

    Args:
        passages: The candidate pool, in the pipeline's relevance order.
        k: How many passages to keep.
        lambda_: Relevance/diversity trade-off, expected in [0, 1]. 1.0 is
            pure relevance, 0.0 is pure diversity, 0.7 is the usual starting
            point. The range is enforced by the caller's settings field rather
            than re-checked here.

    Returns:
        At most k passages in selection order. Degenerate inputs return early:
        an empty pool or k <= 0 selects nothing, k >= len(passages) hands back
        the caller's order untouched (nothing to drop means nothing to
        select), and lambda_ >= 1.0 returns the plain top-k slice.
    """
    if k <= 0 or not passages:
        return []
    if k >= len(passages):
        # Every candidate survives either way, so reordering here would only
        # renumber the citations without changing the evidence.
        return list(passages)
    if lambda_ >= 1.0:
        # Pure relevance has to be the pre-existing slice EXACTLY -- including
        # when the pool was ordered by something other than ``.score`` -- so
        # that lambda_ = 1.0 is a true no-op control arm for an A/B.
        return list(passages[:k])

    tokens = [_token_set(p.text) for p in passages]
    # Running max similarity to anything already selected. Folding each new
    # pick into it once is what makes this O(k * n) instead of O(k^2 * n).
    penalty = [0.0] * len(passages)
    remaining = list(range(len(passages)))
    selected: list[int] = []
    while remaining and len(selected) < k:
        best_pos = -1
        best_value = 0.0
        for pos, cand in enumerate(remaining):
            rel = passages[cand].score
            value = lambda_ * rel - (1.0 - lambda_) * penalty[cand]
            # A NaN or infinite score (a provider returning junk) must not win
            # the argmax by poisoning every later comparison; skip it and let
            # the finite candidates decide.
            if not math.isfinite(value):
                continue
            if best_pos < 0 or value > best_value:
                best_pos, best_value = pos, value
        if best_pos < 0:
            # Every remaining candidate scored non-finite. Take them in the
            # caller's order rather than returning fewer passages than asked.
            best_pos = 0
        chosen = remaining.pop(best_pos)
        selected.append(chosen)
        for cand in remaining:
            penalty[cand] = max(penalty[cand], _jaccard(tokens[cand], tokens[chosen]))
    return [passages[i] for i in selected]
