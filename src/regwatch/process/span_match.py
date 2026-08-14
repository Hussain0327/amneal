"""Locating a cited quote inside its source page.

Both quote validators (BE-requirements extraction and Watch change
summaries) have to answer one question: does this span the model quoted
actually occur on the page it cites? Exact string equality answers "no"
far too often, because PDF text extraction moves characters around
without changing what a human reads:

    ligature        U+FB01 for "fi"
    soft hyphen     "dis" U+00AD "solution"
    line wrap       "dis-\\nsolution"
    smart quotes    U+201C for '"', U+2019 for "'"
    column joins    doubled spaces, dropped newlines

This module holds the two stages both call sites share (spec S34). Stage 1
normalizes both sides and tests exact containment. Stage 2 allows a small,
explicit edit budget so a residual extraction artifact does not silently
cost us a real citation. The budget is guarded so it can only absorb
typo-class noise, never a change of meaning: it is hard-capped at
`_FUZZY_BUDGET_CAP` total edits, no single word may change by more than
one edit (which rejects "fed"/"fasted", "milligrams"/"micrograms"), no
word may be inserted or dropped (which rejects an added "not"), negation
words may not change at all, and digits must survive verbatim. Anything
past those guards is not a match: an unlocatable quote fails closed,
exactly as before.

The call sites only ask a page-membership question and produce no
character offsets, so there is no normalized-to-original offset map here
and multiple candidate locations are not a rejection reason.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

# The characters NFKC leaves alone, keyed by code point to keep this source
# ASCII. Invisible characters map to None (deleted): they sit *inside* words
# after extraction and are invisible to the reader, so they must not decide a
# citation. Typographic punctuation folds to ASCII so a quote typed with
# straight punctuation matches a page set in a real font.
_CHAR_FOLD: dict[int, str | None] = {
    0x00AD: None,  # soft hyphen
    0x200B: None,  # zero-width space
    0x200C: None,  # zero-width non-joiner
    0x200D: None,  # zero-width joiner
    0xFEFF: None,  # byte-order mark
    0x2018: "'",  # left single quotation mark
    0x2019: "'",  # right single quotation mark / apostrophe
    0x201A: "'",  # single low-9 quotation mark
    0x201B: "'",  # single high-reversed-9 quotation mark
    0x2032: "'",  # prime
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x201E: '"',  # double low-9 quotation mark
    0x201F: '"',  # double high-reversed-9 quotation mark
    # U+2033 (double prime) and U+2011 (non-breaking hyphen) are absent on
    # purpose: NFKC runs first and decomposes them to U+2032 U+2032 and
    # U+2010, so entries for them would be dead code.
    0x2010: "-",  # hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2015: "-",  # horizontal bar
    0x2212: "-",  # minus sign
}

# A hyphen-minus or soft hyphen at the end of a line is a wrap artifact, so
# the word is rejoined. This runs BEFORE the punctuation fold so an en/em
# dash at a line end (a numeric range like "80-125" wrapping) is never
# mistaken for a wrap hyphen and fused into one token. It is still ambiguous
# with a real hyphen that happens to fall at a line end ("single-\ndose");
# that case loses its hyphen here and is recovered by the Stage 2 budget
# instead of being silently dropped.
_LINE_BREAK_HYPHEN_RE = re.compile(r"[-\u00ad][ \t]*\r?\n\s*")

_WHITESPACE_RE = re.compile(r"\s+")

_DIGIT_RE = re.compile(r"\d")

# Stage 2 budget: 5% of the quote length, at least one edit (spec S34), but
# never more than the cap. Without the cap a 300-char quote would get a
# 15-edit budget, enough to absorb several meaning-inverting word changes.
_FUZZY_BUDGET_RATIO = 0.05
_FUZZY_BUDGET_CAP = 3

# No single word may change by more than one edit. One-char typos and a
# hyphen lost to dehyphenation stay within it; a word swapped for a
# different word ("fed"/"fasted", "milligrams"/"micrograms") does not.
_PER_WORD_BUDGET = 1

# Words that flip the meaning of a requirement outright. A fuzzy match may
# never create, destroy, or alter one: "not"/"now" is a single edit.
_NEGATION_TOKENS = frozenset({"no", "not", "non", "never", "without", "cannot"})

# Below this length one edit is a large share of the quote, and a short
# span is cheap to confuse ("80 mg" vs "40 mg", "fasting" vs "fasted").
# Short quotes therefore get Stage 1 only. From 12 up, the per-word and
# negation guards are what keep the single-edit budget from buying a
# meaning change.
_MIN_FUZZY_CHARS = 12


@dataclass(frozen=True)
class SpanMatch:
    """Outcome of looking for one quote in one page of source text.

    Attributes:
      found: True when the quote was located within the edit budget.
      exact: True when it was found as a substring of the normalized page
        (Stage 1). False means it was only reached through Stage 2.
      distance: Edit distance of the accepted match, 0 when exact and -1
        when nothing was accepted.
    """

    found: bool
    exact: bool
    distance: int


NO_MATCH = SpanMatch(found=False, exact=False, distance=-1)


def normalize_for_match(text: str) -> str:
    """Fold text into the space where quotes and pages are compared.

    Applies, in order: NFKC (which folds ligatures, non-breaking spaces
    and ellipses), line-wrap dehyphenation (a hyphen-minus or soft hyphen
    at a line end, before dashes are folded so a wrapping en/em dash never
    fuses its neighbors), the code-point fold above (remaining invisible
    characters deleted, typographic punctuation mapped to ASCII),
    whitespace collapsing and case folding. The result is idempotent:
    normalizing it again is a no-op.

    Args:
      text: Raw quote or page text.

    Returns:
      The normalized form. May be empty if the input carried no content.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _LINE_BREAK_HYPHEN_RE.sub("", folded)
    folded = folded.translate(_CHAR_FOLD)
    return _WHITESPACE_RE.sub(" ", folded).strip().casefold()


def quote_on_page(quote: str, page_text: str) -> SpanMatch:
    """Locate a quote inside one page of source text.

    Stage 1 is exact containment on normalized text. CPython's substring
    search is already linear, so it is used directly rather than a
    hand-rolled rolling hash. Stage 2 runs only when Stage 1 fails.

    Args:
      quote: The span the model claims it copied from the page.
      page_text: The raw text of the page the citation points at.

    Returns:
      A `SpanMatch`. `NO_MATCH` when either side normalizes to nothing or
      the quote cannot be placed within the edit budget.
    """
    needle = normalize_for_match(quote)
    haystack = normalize_for_match(page_text)
    if not needle or not haystack:
        return NO_MATCH
    if needle in haystack:
        return SpanMatch(found=True, exact=True, distance=0)
    return _bounded_fuzzy_match(needle, haystack)


def _bounded_fuzzy_match(needle: str, haystack: str) -> SpanMatch:
    """Accept a quote that is within a small edit budget of the page.

    rapidfuzz locates the best-aligned window, and the decision is then
    made by an exact, cutoff-bounded Levenshtein distance against that
    window. The window can be off by a character at either end, which
    spends budget the true best window would not; that direction of error
    rejects, which is the safe one.

    Args:
      needle: Normalized quote.
      haystack: Normalized page text.

    Returns:
      A fuzzy `SpanMatch`, or `NO_MATCH` when the quote is too short to
      risk it, past the budget, would move a digit, or would change any
      word by more than typo-class noise.
    """
    if len(needle) < _MIN_FUZZY_CHARS:
        return NO_MATCH
    budget = min(
        _FUZZY_BUDGET_CAP,
        max(1, math.ceil(_FUZZY_BUDGET_RATIO * len(needle))),
    )
    alignment = fuzz.partial_ratio_alignment(needle, haystack)
    if alignment is None:
        return NO_MATCH
    window = haystack[alignment.dest_start : alignment.dest_end]
    if not window:
        return NO_MATCH
    # Both forms are scored because the aligned window often carries one
    # leading or trailing space that would otherwise eat an edit.
    distance = min(
        Levenshtein.distance(needle, window, score_cutoff=budget),
        Levenshtein.distance(needle, window.strip(), score_cutoff=budget),
    )
    if distance > budget:
        return NO_MATCH
    if _DIGIT_RE.findall(needle) != _DIGIT_RE.findall(window):
        # One edit turns "900 mL" into "500 mL". Numbers carry the
        # regulatory content of a PSG quote, so a hit that changes them is
        # a different span, not an extraction artifact.
        return NO_MATCH
    if not _word_changes_are_typo_class(needle, window.strip()):
        return NO_MATCH
    return SpanMatch(found=True, exact=False, distance=distance)


def _word_changes_are_typo_class(needle: str, window: str) -> bool:
    """Decide whether the edits between quote and window are only noise.

    The character budget alone cannot tell a typo from a meaning change:
    "fed" to "fasted" is three edits, well inside a long quote's budget.
    So the two sides are also compared word by word. A run of differing
    words is acceptable only when joining each side (whitespace removed,
    so a word split or merged across a fold still compares as itself)
    stays within `_PER_WORD_BUDGET` and no negation word is involved. A
    whole word inserted or dropped fails the joined comparison by costing
    its full length.

    Args:
      needle: Normalized quote.
      window: The aligned, stripped page window the quote fuzzily matched.

    Returns:
      True when every difference is typo-class, False otherwise.
    """
    needle_words = needle.split()
    window_words = window.split()
    matcher = difflib.SequenceMatcher(a=needle_words, b=window_words, autojunk=False)
    for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        quote_side = needle_words[a_start:a_end]
        window_side = window_words[b_start:b_end]
        if _NEGATION_TOKENS & set(quote_side) or _NEGATION_TOKENS & set(window_side):
            return False
        joined_gap = Levenshtein.distance(
            "".join(quote_side),
            "".join(window_side),
            score_cutoff=_PER_WORD_BUDGET,
        )
        if joined_gap > _PER_WORD_BUDGET:
            return False
    return True
