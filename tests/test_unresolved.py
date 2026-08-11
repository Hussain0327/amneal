"""The fork that separates a greeting from a drug we do not carry.

Audit #1715: a bare "Hello" came back as status=refused / reason=no_product --
the identical outcome a genuinely absent drug produces -- and rendered as a red
"Evidence gap" card. These tests pin the three outcomes apart.
"""

from __future__ import annotations

import pytest

from regwatch.generate.grounded_qa import _FILLER
from regwatch.generate.unresolved import _SOCIAL, classify_unresolved, is_social
from regwatch.retrieve.resolver import _NON_DRUG_WORDS


@pytest.mark.parametrize(
    "question",
    [
        "Hello",
        "hi",
        "Hey",
        "hi there",
        "Hello there!",
        "thanks",
        "Thank you",
        "good morning",
        "  hey  ",
        "hello?",
    ],
)
def test_social_turns_are_social(question: str) -> None:
    assert is_social(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # A lookup that merely opens politely is NOT social: the greeting gate
        # must not swallow the question riding behind it.
        "hi, what does the estradiol PSG recommend?",
        "hello, tell me about beclomethasone",
        "thanks, what about dissolution?",
        # Topic-less but task-shaped -- this is a clarify, not a conversation.
        "Can you tell me something about a drug?",
        "tell me more",
        "romidepsin",
        "",
        "   ",
    ],
)
def test_non_social_turns_are_not_social(question: str) -> None:
    assert is_social(question) is False


def test_no_social_word_is_a_drug_candidate() -> None:
    """The two modules must never disagree about what could be a drug name.

    That disagreement IS the original defect: grounded_qa._FILLER called
    "hello" filler while resolver._NON_DRUG_WORDS omitted it, so the resolver
    fuzzy-scored a greeting against the whole FDA catalog. Only tokens of four
    characters or more ever become candidates (resolver._drug_like_tokens), so
    those are what must be excluded.
    """
    candidates = {word for word in _SOCIAL if len(word) >= 4}
    assert candidates <= _NON_DRUG_WORDS


def test_social_words_are_filler_where_filler_knows_them() -> None:
    """Where both lists name a word, they must agree it is not content.

    Containment does not hold in the other direction -- _FILLER carries
    follow-up words like "more" and "something" that are emphatically NOT
    social -- so this pins the overlap, not equality.
    """
    assert _SOCIAL & _FILLER


def test_greeting_converses() -> None:
    assert classify_unresolved("Hello", external_drug_known=False) == "converse"


def test_greeting_converses_even_when_openfda_matched() -> None:
    """Social wins outright: a pleasantry is never a product lookup."""
    assert classify_unresolved("Hello", external_drug_known=True) == "converse"


def test_topicless_request_asks_which_product() -> None:
    assert (
        classify_unresolved("Can you tell me something about a drug?", external_drug_known=False)
        == "need_product"
    )


def test_known_absent_drug_is_not_covered() -> None:
    assert (
        classify_unresolved("Tell me about romidepsin", external_drug_known=True)
        == "product_not_covered"
    )


def test_unverified_drug_like_token_falls_back_to_need_product() -> None:
    """Weak and ambiguous tokens must never claim the corpus lacks a product.

    Without positive external evidence (no key, lookup failed, no match) the
    honest answer is "which product?", not "we do not cover asdfgh".
    """
    assert classify_unresolved("Tell me about asdfgh", external_drug_known=False) == "need_product"


def test_greeting_and_absent_drug_are_different_outcomes() -> None:
    """The invariant audit #1715 violated: romidepsin is not Hello."""
    greeting = classify_unresolved("Hello", external_drug_known=False)
    absent = classify_unresolved("Tell me about romidepsin", external_drug_known=True)
    assert greeting != absent
