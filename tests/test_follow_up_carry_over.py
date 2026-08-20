"""A follow-up must keep the product the previous turn established.

Regression for prod audit #2501: one message after answering about
beclomethasone, "how come?" was met with "Sure -- which product are you asking
about?". Carry-over fires on `_looks_like_follow_up(question) or
route_carries_product`; the router supplies the second term and is off the Ask
path, so the text heuristic is now the only trigger and has to be right.
"""

from __future__ import annotations

import pytest

from regwatch.generate.grounded_qa import _looks_like_follow_up

FOLLOW_UPS = [
    "how come?",
    "how come",
    "why?",
    "and the fed study?",
    "ok and dissolution?",
    "so what about the fed study?",
    "then what about dissolution?",
    "what about the capsule?",
    "tell me more",
]

NEW_TOPICS = [
    "What study design is recommended for albuterol sulfate inhalation aerosol?",
    "What does the beclomethasone dipropionate PSG recommend?",
    "list every PSG revised in 2026",
]


@pytest.mark.parametrize("question", FOLLOW_UPS)
def test_follow_up_keeps_session_product(question: str) -> None:
    assert _looks_like_follow_up(question), f"follow-up not recognised: {question!r}"


@pytest.mark.parametrize("question", NEW_TOPICS)
def test_new_topic_does_not_inherit_session_product(question: str) -> None:
    assert not _looks_like_follow_up(question), f"new topic treated as follow-up: {question!r}"
