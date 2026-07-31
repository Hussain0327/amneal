"""New coverage for regwatch's structured-output adapter (src/regwatch/deficiency/structured.py).

This module did not exist upstream: DefPredict's llm/structured.py called the OpenAI SDK
directly against a Databricks strict json_schema response_format. The regwatch adapter
instead routes every call through the injectable regwatch.generate.llm.LLMProvider seam
(the D1 residency guard lives there), so these tests build a tiny fake provider and drive
the L1->L5 pipeline entirely offline: no Postgres, no real LLM call.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from regwatch.deficiency.structured import chat_completion, parse_structured, structured_call
from regwatch.generate.llm import D1ResidencyError, LLMMessage, LLMResponse, LLMStreamChunk


class Widget(BaseModel):
    """A small local schema -- no dependency on the deficiency domain schemas."""

    name: str
    count: int


class FakeProvider:
    """Mirrors regwatch.generate.llm.LLMProvider: complete(messages, *, temperature,
    max_tokens, response_format) -> LLMResponse. Scripted with a queue of responses;
    each is either the completion text (str) or an Exception instance to raise.
    """

    name = "fake"

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if not self._responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, model=self.name)

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        # Protocol completeness only: the structured adapter never streams.
        raise NotImplementedError("FakeProvider does not stream")


def test_structured_call_happy_path_returns_validated_instance():
    provider = FakeProvider(['{"name": "bolt", "count": 3}'])
    instance, failure = structured_call(
        messages=[{"role": "user", "content": "extract"}],
        model_cls=Widget,
        provider=provider,
    )
    assert failure is None
    assert instance == Widget(name="bolt", count=3)
    assert len(provider.calls) == 1


def test_parse_structured_handles_markdown_fence_and_trailing_prose():
    raw = (
        "Here is the result:\n"
        "```json\n"
        '{"name": "bolt", "count": 2}\n'
        "```\n"
        "Let me know if you need more."
    )
    instance, err = parse_structured(raw, Widget)
    assert err is None
    assert instance == Widget(name="bolt", count=2)


def test_parse_structured_salvages_almost_json_via_json_repair():
    # Unquoted keys, single-quoted strings, trailing comma -- not valid JSON on its own.
    raw = "{name: 'bolt', count: 4,}"
    instance, err = parse_structured(raw, Widget)
    assert err is None
    assert instance == Widget(name="bolt", count=4)


def test_structured_call_retries_once_on_truncation_with_doubled_max_tokens():
    provider = FakeProvider([RuntimeError("finish_reason=length"), '{"name": "bolt", "count": 5}'])
    instance, failure = structured_call(
        messages=[{"role": "user", "content": "x"}],
        model_cls=Widget,
        max_tokens=100,
        provider=provider,
    )
    assert failure is None
    assert instance == Widget(name="bolt", count=5)
    assert len(provider.calls) == 2
    assert provider.calls[0]["max_tokens"] == 100
    assert provider.calls[1]["max_tokens"] == 200


def test_structured_call_rescue_recovers_from_invalid_first_response():
    # First response is missing the required "count" field -> fails validation;
    # the L5 rescue call gets it right.
    provider = FakeProvider(['{"name": "bolt"}', '{"name": "bolt", "count": 7}'])
    instance, failure = structured_call(
        messages=[{"role": "user", "content": "x"}],
        model_cls=Widget,
        provider=provider,
    )
    assert failure is None
    assert instance == Widget(name="bolt", count=7)
    assert len(provider.calls) == 2


def test_structured_call_both_invalid_returns_parse_failed_l5():
    provider = FakeProvider(['{"name": "bolt"}', '{"name": "bolt"}'])
    instance, failure = structured_call(
        messages=[{"role": "user", "content": "x"}],
        model_cls=Widget,
        provider=provider,
    )
    assert instance is None
    assert failure is not None
    assert failure.layer == "L5"
    assert failure.requires_human_review is True


def test_structured_call_propagates_d1_residency_error():
    provider = FakeProvider([D1ResidencyError("model outside the D1 perimeter")])
    with pytest.raises(D1ResidencyError):
        structured_call(
            messages=[{"role": "user", "content": "x"}],
            model_cls=Widget,
            provider=provider,
        )


def test_chat_completion_returns_text_and_passes_temperature_and_max_tokens():
    provider = FakeProvider(["pong"])
    text = chat_completion(
        [{"role": "user", "content": "ping"}],
        temperature=0.25,
        max_tokens=222,
        provider=provider,
    )
    assert text == "pong"
    assert len(provider.calls) == 1
    assert provider.calls[0]["temperature"] == 0.25
    assert provider.calls[0]["max_tokens"] == 222
