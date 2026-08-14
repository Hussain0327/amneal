"""_StreamScrubber: the incremental twin of _visible_answer_text.

Property pinned here: for ANY split of the wire text into chunks, the
concatenated visible output (pushes + flush) equals _visible_answer_text of
the full text, and no push ever emits private-block content or a partial
delimiter.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from regwatch.generate.llm import LLMMessage, _StreamScrubber, _visible_answer_text

if TYPE_CHECKING:
    from regwatch.generate.llm import DatabricksProvider

CASES = [
    "plain answer with no markup at all.",
    "<|channel>thought\nsecret reasoning<channel|>The visible answer.",
    "<think>secret</think>Visible after think.",
    "answer ends with partial <|chan",
    "<|channel>thought\nunterminated secret",
    "pre <think>a</think>mid<think>b</think> post",
]


def _every_split(text: str, parts: int = 2) -> Iterator[list[str]]:
    if parts == 2:
        for i in range(len(text) + 1):
            yield [text[:i], text[i:]]
    else:
        for i in range(len(text) + 1):
            for rest in _every_split(text[i:], parts - 1):
                yield [text[:i], *rest]


@pytest.mark.parametrize("text", CASES)
def test_incremental_equals_buffered_scrub_for_every_two_way_split(text: str) -> None:
    expected = _visible_answer_text(text)
    for chunks in _every_split(text):
        scrub = _StreamScrubber()
        out: list[str] = []
        reset_seen = False
        for chunk in chunks:
            visible, reset = scrub.push(chunk)
            if reset:
                reset_seen = True
                out.clear()
            out.append(visible)
        out.append(scrub.flush())
        emitted = "".join(out)
        # Final consistency: what survived equals the buffered scrub (both
        # sides whitespace-normalized; the buffered scrub strips ends).
        assert emitted.strip() == expected, (chunks, reset_seen)


@pytest.mark.parametrize("text", CASES[1:3])
def test_no_push_ever_emits_private_content(text: str) -> None:
    for chunks in _every_split(text, parts=3):
        scrub = _StreamScrubber()
        for chunk in chunks:
            visible, _ = scrub.push(chunk)
            assert "secret" not in visible, chunks


def test_stray_close_delimiter_signals_reset() -> None:
    scrub = _StreamScrubber()
    v1, r1 = scrub.push("looked like an answer ")
    assert v1 and not r1
    v2, r2 = scrub.push("<channel|>the real answer")
    assert r2 is True
    assert "looked like" not in v2
    assert ("".join([v2, scrub.flush()])).strip() == _visible_answer_text(
        "looked like an answer <channel|>the real answer"
    )


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeEvent:
    def __init__(
        self,
        content: str | None = None,
        model: str | None = None,
        finish_reason: str | None = None,
        choices: bool = True,
    ) -> None:
        self.model = model
        self.choices = [_FakeChoice(content, finish_reason)] if choices else []


class _FakeStreamClient:
    """Duck-typed openai client whose chat.completions.create yields events."""

    class _Completions:
        def __init__(self, events: list[Any]) -> None:
            self._events = events

        def create(self, **kwargs: object) -> object:
            if not kwargs.get("stream"):
                raise AssertionError("buffered fallback must not be reached")
            return iter(self._events)

    def __init__(self, events: list[Any]) -> None:
        self.chat = type("C", (), {"completions": self._Completions(events)})()


def _provider(events: list[Any], **kw: Any) -> DatabricksProvider:
    from regwatch.generate.llm import DatabricksProvider

    return DatabricksProvider(
        "alias-model", "http://unit.test", "tok", client=_FakeStreamClient(events), **kw
    )


def test_databricks_stream_yields_true_incremental_deltas() -> None:
    events = [
        _FakeEvent(content="Hello ", model="served-1"),
        _FakeEvent(content="world."),
        _FakeEvent(content=None, finish_reason="stop"),
    ]
    chunks = list(_provider(events).stream([LLMMessage(role="user", content="q")]))
    deltas = [c.delta for c in chunks if not c.done]
    assert deltas == ["Hello ", "world."], "one visible delta per wire event, not one blob"
    assert chunks[-1].done and chunks[-1].response is not None
    assert chunks[-1].response.text == "Hello world."
    assert chunks[-1].response.model == "served-1"


def test_d1_rejects_on_first_event_before_any_yield() -> None:
    from regwatch.generate.llm import D1ResidencyError

    events = [_FakeEvent(content="leak", model="evil-model")]
    prov = _provider(events, d1_enforced=True, d1_allowed_models=("good-model",))
    it = prov.stream([LLMMessage(role="user", content="q")])
    with pytest.raises(D1ResidencyError):
        next(it)


def test_d1_late_model_does_not_hold_deltas_and_binds_at_arrival() -> None:
    from regwatch.generate.llm import D1ResidencyError

    events = [
        _FakeEvent(content="early ", model=None),
        _FakeEvent(content="text", model="evil-model"),
    ]
    prov = _provider(events, d1_enforced=True, d1_allowed_models=("good-model",))
    it = prov.stream([LLMMessage(role="user", content="q")])
    assert next(it).delta == "early "  # owner decision: no hold on late metadata
    with pytest.raises(D1ResidencyError):
        while True:
            next(it)


def test_stream_never_reports_model_raises_under_enforcement_at_end() -> None:
    from regwatch.generate.llm import D1ResidencyError

    events = [
        _FakeEvent(content="text", model=None),
        _FakeEvent(content=None, finish_reason="stop"),
    ]
    prov = _provider(events, d1_enforced=True, d1_allowed_models=("good-model",))
    with pytest.raises(D1ResidencyError):
        list(prov.stream([LLMMessage(role="user", content="q")]))


def test_no_buffered_resend_after_first_yield() -> None:
    class _Boom:
        model = None
        choices = property(lambda self: (_ for _ in ()).throw(RuntimeError("wire died")))

    events: list[object] = [_FakeEvent(content="painted ", model="served-1"), _Boom()]
    it = _provider(events).stream([LLMMessage(role="user", content="q")])
    assert next(it).delta == "painted "
    with pytest.raises(RuntimeError):
        # The _FakeStreamClient asserts if the buffered fallback re-calls
        # create(stream=False), so a silent re-send fails loudly here too.
        list(it)


def test_reasoning_block_never_reaches_a_delta() -> None:
    events = [
        _FakeEvent(content="<|channel>thought\nsecret ", model="served-1"),
        _FakeEvent(content="stuff<channel|>The answer.", finish_reason="stop"),
    ]
    chunks = list(_provider(events).stream([LLMMessage(role="user", content="q")]))
    assert "secret" not in "".join(c.delta for c in chunks)
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "The answer."
