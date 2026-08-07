"""The shared sentence split, and the claim-shape rules that depend on it.

The gate admits a claim only when it is ONE sentence, so every false split here
destroys a correct, fully-cited answer. Every merge, conversely, would let one
text slot carry two assertions behind one set of cites -- the hole
DROP_MULTI_SENTENCE exists to close. Both directions are asserted.
"""

from __future__ import annotations

import pytest

from regwatch.common.sentences import sentence_count, split_sentences
from regwatch.generate import turn_gate as tg


@pytest.mark.parametrize(
    "text",
    [
        "The U.S. FDA recommends a fasting study.",
        "Applicants should consult the U.S. Pharmacopeia monograph.",
        "Use approx. 12 dosage units for the comparison.",
        "See Fig. 3 for the dissolution profile.",
        "Cite No. 4 of the referenced guidance.",
        "A waiver may apply, e.g. for a lower strength.",
        "The reference product, i.e. the RLD, must be used.",
        "Compare the test product vs. the reference listed drug.",
        "Dissolution testing follows the Ph. Eur.",
        "Dr. Smith signed the protocol.",
        # Decimals never split: there is no whitespace after the period.
        "Dissolution is measured at 0.5 mg per unit.",
        "Apparatus 2 runs at 50 rpm for 30 min.",
    ],
)
def test_single_sentence_stays_single(text: str) -> None:
    assert sentence_count(text) == 1


@pytest.mark.parametrize(
    "text",
    [
        "FDA recommends a fasting study. A fed study is also required.",
        "Use Apparatus 2. Report the profile.",
        # "etc." is deliberately NOT treated as non-terminal: it can end a
        # sentence, and merging is the dangerous direction.
        "Include dissolution, content uniformity, etc. A fed study is required.",
        # "pH." ends dissolution sentences for real; only "Eur." continues a
        # "Ph." abbreviation. Merging here would hide two assertions behind
        # one set of cites.
        "The buffer is adjusted to the target pH. A second stage follows.",
        # "Ph. Eur." can itself end a sentence; the next one stays separate.
        "Testing follows the Ph. Eur. The method is described below.",
    ],
)
def test_two_sentences_are_still_two(text: str) -> None:
    assert sentence_count(text) == 2


def test_split_returns_the_segments_not_just_a_count() -> None:
    assert split_sentences("FDA requires X. FDA requires Y.") == [
        "FDA requires X.",
        "FDA requires Y.",
    ]
    assert split_sentences("") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("- FDA recommends a fasting study.", "FDA recommends a fasting study."),
        ("* FDA recommends a fasting study.", "FDA recommends a fasting study."),
        ("1. FDA recommends a fasting study.", "FDA recommends a fasting study."),
        ("2) FDA recommends a fasting study.", "FDA recommends a fasting study."),
    ],
)
def test_leading_list_markers_are_stripped(raw: str, expected: str) -> None:
    """A surviving "1." reads as a sentence terminator and drops a valid claim."""
    assert tg._sanitize_claim_text(raw) == expected
    assert tg._sentence_count(tg._sanitize_claim_text(raw)) == 1


def test_numbered_claim_would_have_been_dropped_before_the_fix() -> None:
    """Guards the specific regression: numbered claim -> two sentences -> drop."""
    sanitized = tg._sanitize_claim_text("1. A fasting study is recommended.")
    assert not sanitized.startswith("1.")
    assert tg._sentence_count(sanitized) == 1
