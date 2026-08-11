"""End-to-end: the five Ask outcomes stay five distinct things.

Audit #1715 collapsed a greeting, a topic-less request and an absent drug into
one refusal. These tests run the real pipeline and pin them apart.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db
from regwatch.store.vector_store import add_chunks


def _seed(names: list[str]) -> None:
    init_db()
    provider = get_embedding_provider()
    texts = [f"Guidance text for {name}." for name in names]
    add_chunks(
        ids=[f"c{i}" for i, _ in enumerate(names)],
        documents=texts,
        embeddings=provider.embed(texts),
        metadatas=[
            {
                "doc_id": i + 1,
                "version_id": i + 1,
                "ordinal": 0,
                "page": 1,
                "normalized_name": name,
                "dosage_form": "TABLET",
                "route": "ORAL",
                "source_url": "https://example.test/psg.pdf",
                "psg_type": "final",
                "appl_no": f"02000{i}",
            }
            for i, name in enumerate(names)
        ],
    )


_TWO = ["propranolol hydrochloride", "metformin hydrochloride"]


class _ForbiddenLLM:
    """Any construction is a failure: a greeting must reach no AI path."""

    name = "forbidden"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a greeting must not reach a model")


@pytest.mark.parametrize("greeting", ["Hello", "hi", "Hey there", "thanks"])
def test_greeting_converses_without_touching_a_model(
    monkeypatch: pytest.MonkeyPatch, greeting: str
) -> None:
    """The defect, inverted: a greeting is a reply, not an Evidence gap.

    Also pins the cost fix -- every "Hello" used to pay a router round trip
    whose text render_guidance_message discarded on the no_product branch.
    """
    _seed(_TWO)
    calls = {"n": 0}

    def _forbidden(*a: object, **k: object) -> Any:
        calls["n"] += 1
        return _ForbiddenLLM()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _forbidden)

    result = qa_mod.ask(greeting)

    assert result.status == "meta"
    assert result.reason == "greeting"
    assert result.refused is False
    assert result.citations == []
    assert result.answer == qa_mod.CONVERSE_GREETING_TEXT
    assert calls["n"] == 0


def test_greeting_is_still_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-6 does not bend for a cheap turn: no audit row, no reply."""
    _seed(_TWO)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ForbiddenLLM())
    result = qa_mod.ask("Hello")
    assert result.audit_id is not None


def test_topicless_request_clarifies_conversationally() -> None:
    """A task-shaped turn with no product asks which product, and is not red."""
    _seed(_TWO)
    result = qa_mod.ask("Can you tell me something about a drug?")
    assert result.status == "clarify"
    assert result.reason == "need_product"
    assert result.refused is False
    assert result.answer == qa_mod.NEED_PRODUCT_GUIDANCE_TEXT


def test_greeting_and_absent_drug_are_not_the_same_turn() -> None:
    """ "Tell me about romidepsin" must never be equivalent to "Hello"."""
    _seed(_TWO)
    greeting = qa_mod.ask("Hello")
    absent = qa_mod.ask("What study design is recommended for romidepsin?")
    assert (greeting.status, greeting.reason) != (absent.status, absent.reason)
    assert greeting.status == "meta"
    assert absent.status == "clarify"


def test_greeting_with_a_pinned_product_still_offers_that_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The social gate must not swallow a turn that HAS something to talk about.

    "Hello" with an active-ingredient filter already has a product, so it keeps
    reaching the vague-input clarify and its option menu -- the estradiol
    gel-versus-tablet guard depends on that path staying live.
    """
    _seed(_TWO)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ForbiddenLLM())
    result = qa_mod.ask("Hello", filters={"normalized_name": "propranolol hydrochloride"})
    assert result.status == "clarify"
    assert result.reason == "vague_input"


def test_a_polite_lookup_is_not_swallowed_as_social() -> None:
    """A question that merely opens politely is still a question."""
    _seed(_TWO)
    result = qa_mod.ask("hi, what does the propranolol hydrochloride guidance recommend?")
    assert result.reason != "greeting"
    assert result.status != "meta"
