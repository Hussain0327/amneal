"""The gate must catch unsupported regulatory claims, not ordinary analysis.

Product decision 2026-08-20: RegWatch is a regulatory colleague, not a passage
quoter. It may reason, hedge, observe gaps and suggest next steps without
citations. What it may NOT do is assert a regulatory requirement it cannot
support. These cases pin that boundary in both directions.
"""

from __future__ import annotations

import pytest

from regwatch.generate.prose_turn import _classify_uncited_selective as classify

# Uncited colleague speech: must be ADMITTED (not "source_fact").
ADMIT = [
    "I'd check the current PSG and referenced BE guidance before treating that as fasting-only.",
    "I don't see a fed requirement in the PSG we pulled.",
    "That's usually where a waiver question comes up.",
    "I wouldn't conclude that from this PSG alone.",
    "The next thing I'd check is the referenced BE guidance.",
    "My read is that these two passages describe different dosage forms.",
    "Worth confirming with the current revision before you rely on it.",
    "I'd suggest pulling the referenced guidance next.",
    "I have no strong view on the capsule yet.",
    "This looks like a standard immediate-release setup.",
    # Evidence observations: what THIS turn's passages do or do not contain.
    "The provided passage does not state any waiver conditions.",
    "These passages contain no mention of a fed study.",
    "The retrieved sections do not cover dissolution.",
    "To answer this accurately, I'd need the PSG's sections covering dissolution.",
    # Markdown is a feature now, and emphasis must not break word matching.
    "The provided passage does **not** state any waiver conditions.",
    "These passages contain *no mention* of a fed study.",
]

# Uncited regulatory assertions: must be DROPPED ("source_fact").
DROP = [
    "FDA requires a fed study for this product.",
    "The guidance recommends a two-way crossover design.",
    "A biowaiver is permitted for this strength.",
    "A fasting study is required for the tablet.",
    "This strength is exempt from in vivo testing.",
    "FDA prohibits the use of that comparator.",
    # A first-person opener must not launder an attributed assertion.
    "I'd say FDA requires a fed study here.",
    "My read is that the guidance requires a fasting study.",
    # A hedge softens a claim; it does not turn it into an observation.
    "I'd say a fed study is required here.",
    "My read is that a biowaiver is permitted for this strength.",
    "I'd say this strength is exempt from in vivo testing.",
    "I would say that comparator is prohibited.",
    "I'd say you must run a fasting study.",
    "I have no doubt a fed study is required.",
    # Evidence deixis is the ONLY thing that makes an absence report an
    # observation. Without it, a negative claim about FDA is still a claim.
    "The guidance does not require a fed study.",
    "FDA does not mention a fed study requirement.",
    # ...and emphasis must not smuggle an assertion through either.
    "**FDA requires** a fed study for this product.",
    "The guidance does **not** require a fed study.",
    "A biowaiver is **permitted** for this strength.",
]


@pytest.mark.parametrize("text", ADMIT)
def test_colleague_analysis_is_admitted(text: str) -> None:
    assert classify(text) != "source_fact", f"colleague speech wrongly dropped: {text!r}"


@pytest.mark.parametrize("text", DROP)
def test_unsupported_regulatory_assertion_is_dropped(text: str) -> None:
    assert classify(text) == "source_fact", f"unsupported assertion admitted: {text!r}"
