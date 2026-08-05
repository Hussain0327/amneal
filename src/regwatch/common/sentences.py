"""One sentence splitter, shared by the turn gate and the faithfulness metric.

These two callers MUST agree. The gate admits a claim only when it is exactly one
sentence; the metric then asserts that every sentence of the rendered answer
carries a citation. "One claim = one rendered sentence" and "faithfulness == 1.0
for an all-admitted turn" are the same statement only while both use the same
split -- and until this module existed they were two byte-identical regexes in
two files, kept in sync by a comment rather than by code.

The naive split (any ``.``/``!``/``?`` followed by whitespace) is wrong on
abbreviations: "The U.S. FDA requires X" is ONE sentence, but the naive rule
reads it as two, so the gate drops a correct claim for DROP_MULTI_SENTENCE.
Decimals are already safe without special handling -- "0.5 mg" has no whitespace
after the period, so the split never fires there.

The abbreviation list is deliberately conservative. It contains only forms that
essentially never END a sentence, because the two errors are not symmetric:

* Splitting too eagerly destroys a correct claim (a real answer is lost).
* Merging too eagerly lets one text slot hold two assertions behind one set of
  cites -- exactly the hole DROP_MULTI_SENTENCE exists to close.

The second error is the dangerous one, so a form that can plausibly end a
sentence ("etc.", "Inc.", "Ltd.") is NOT listed: leaving it out keeps the strict
behaviour there.
"""

from __future__ import annotations

import re

_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Lowercased, without the trailing period. Titles precede a name; the reference
# and measurement forms are always followed by a value.
_NON_TERMINAL_ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "cf",
        "vs",
        "approx",
        "no",
        "fig",
        "figs",
        "sec",
        "secs",
        "tbl",
        "ref",
        "refs",
        "eq",
        "ca",
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
    }
)

_TRAILING_TOKEN = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")


def _is_non_terminal(segment: str) -> bool:
    """True when `segment` ends in an abbreviation, so the next chunk continues it."""
    match = _TRAILING_TOKEN.search(segment.strip())
    if match is None:
        return False
    token = match.group(1).lower()
    # An INTERNAL period makes the token a dotted initialism -- "U.S", the
    # "F.D.A" of "F.D.A.". Those never end a sentence. Note this deliberately
    # does NOT extend to a bare single letter: "...is required for product X."
    # is a real sentence ending, and merging it would swallow the sentence that
    # follows into one claim slot.
    return "." in token or token in _NON_TERMINAL_ABBREVIATIONS


def split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping abbreviations attached to what follows."""
    raw = [s for s in _SPLIT.split(text or "") if s.strip()]
    if not raw:
        return []
    merged: list[str] = [raw[0]]
    for segment in raw[1:]:
        if _is_non_terminal(merged[-1]):
            merged[-1] = f"{merged[-1]} {segment}"
        else:
            merged.append(segment)
    return merged


def sentence_count(text: str) -> int:
    return len(split_sentences(text))
