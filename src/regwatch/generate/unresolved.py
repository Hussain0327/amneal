"""What a turn that resolved no product actually is.

Ask used to have exactly one terminal exit for an unresolved turn -- reason
``no_product`` -- so a greeting, a topic-less request and a genuinely absent
drug were indistinguishable, and all three rendered as a red "Evidence gap"
card. This module is the fork that separates them.

Pure by construction: no DB, no network, no settings. The one signal that needs
I/O (does this token name a real FDA drug we do not cover?) is resolved by the
caller and passed in, so every rule here is table-testable offline.

The outcomes are terminal states, not a routing layer. When the conversational
route call is promoted to ``live`` (plan PR12), ``mode=converse`` becomes a
second trigger for the same outcomes and this classifier stays as the
deterministic fallback that PR12 already requires on route failure.
"""

from __future__ import annotations

import re
from typing import Literal

UnresolvedOutcome = Literal["converse", "need_product", "product_not_covered"]

# Social vocabulary. Deliberately NOT grounded_qa._FILLER, which also holds
# "more", "something", "else", "question" and "regarding" -- under _FILLER a
# follow-up like "something else" would classify as a greeting. Every word here
# is also in _FILLER or is a pure pleasantry; test_unresolved pins the subset
# relationship so the two lists cannot drift into disagreement.
_SOCIAL = frozenset(
    {
        "afternoon",
        "evening",
        "good",
        "greetings",
        "hey",
        "hi",
        "hiya",
        "hello",
        "howdy",
        "morning",
        "ok",
        "okay",
        "please",
        "thank",
        "thanks",
        "there",
        "yo",
        "you",
    }
)

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(question: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(question.lower()) if t]


def is_social(question: str) -> bool:
    """True when the turn is a pleasantry and nothing else.

    Requires EVERY token to be social, not merely the presence of a greeting:
    "hi, what does the estradiol PSG recommend?" is a lookup that happens to
    open politely, and must not be swallowed here.
    """
    tokens = _tokens(question)
    return bool(tokens) and all(token in _SOCIAL for token in tokens)


def classify_unresolved(question: str, *, external_drug_known: bool) -> UnresolvedOutcome:
    """Decide what an unresolved turn is, in precedence order.

    Args:
      question: The user's turn, verbatim and untrusted.
      external_drug_known: True only when an external authority confirmed the
        turn names a real FDA drug whose ingredients are absent from this
        corpus. False whenever that lookup was unavailable, unconfigured or
        inconclusive -- weak and ambiguous drug-like tokens must fall to
        ``need_product``, never claim ``product_not_covered``.

    Returns:
      ``converse`` for a pure social turn, ``product_not_covered`` for a
      credible drug this corpus does not carry, else ``need_product``.
    """
    if is_social(question):
        return "converse"
    if external_drug_known:
        return "product_not_covered"
    return "need_product"
