# Ask Live-Draft SSE Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream live, un-audited model prose to the Ask page as an explicitly
provisional `draft` SSE channel while the audited pipeline runs, per the
approved spec docs/superpowers/specs/2026-08-10-ask-sse-live-draft-design.md.

**Architecture:** A streaming reasoning-scrubber inside DatabricksProvider
turns the buffered stream path into true incremental delivery; a
`_stream_structured` sibling in grounded_qa forwards clean deltas through a
new `on_draft` callback threaded exactly like `on_progress`; main.py adds
`draft`/`draft_reset` SSE frames and a `draft_withdrawn` result field; the
frontend feeds a client-side typewriter buffer and a withdrawal note. All
dark behind `REGWATCH_LIVE_DRAFT` AND `REGWATCH_PROSE_SYNTHESIS` AND a
per-request `live_draft` opt-in.

**Tech Stack:** Python 3.12 / FastAPI / pydantic 2, openai SDK streaming
against Databricks Model Serving, Go edge proxy (zero changes), Next.js 16 +
React 18 frontend, pytest + vitest + tests_contract harness.

## Global Constraints

- ASCII only in code and committed output; no em-dashes or smart quotes.
- Run `black .` after the LAST Python edit before any push; CI runs
  `black --check` (ruff alone is NOT enough).
- mypy scope is `mypy src tests tests_contract` (CI scope; `mypy src` alone
  is insufficient).
- No Co-Authored-By or Claude attribution in commits or PR bodies.
- Branch: `feat/ask-sse-live-draft` (this worktree). Commit per task; push
  only at the milestones marked below.
- Local test env: disposable Postgres on :5499
  (`DATABASE_URL=postgresql+psycopg://regwatch@localhost:5499/regwatch`);
  run pytest with `env -u VIRTUAL_ENV uv run pytest`; tests_contract needs
  the Go proxy built (`go build ./cmd/proxy` in go/).
- All new draft paths must be byte-inert when any gate flag is off: with
  `REGWATCH_LIVE_DRAFT` unset, every existing test passes UNCHANGED.
- The terminal LLMResponse.text stays byte-identical to today on every path:
  drafts are best-effort; the buffered scrub over the full text remains the
  canonical answer input to the gate.
- File anchors below were verified against main @ f3e4aa4; re-verify with
  grep before editing if a rebase happened.

---

### Task 1: Docs truth-up + INV-1 amendment of record

**Files:**
- Modify: `docs/PROJECT_SPEC.md` (section 4, near line 75)
- Modify: `src/regwatch/generate/turn_gate.py:1-37` (module docstring only)
- Modify: `src/regwatch/generate/grounded_qa.py:2237-2241` (ask_core docstring only)
- Modify: `regwatch/frontend/README.md:98-103`
- Modify: `docs/POLYGLOT_TARGET_2026-07-10.md:97-101` (R3)

**Interfaces:** none (docs/docstrings only; zero behavior change).

- [ ] **Step 1: Amend PROJECT_SPEC.** Locate the line reading
  `No ungrounded claims, ever` (near line 75). Append after its paragraph:

```markdown
Amendment (owner, 2026-08-07, implemented 2026-08-10): live un-gated prose
MAY stream to the client as an explicitly provisional draft on the dedicated
`draft` SSE channel, dual-gated by REGWATCH_LIVE_DRAFT and a per-request
opt-in and available only in prose-synthesis mode. Nothing un-audited may be
PRESENTED AS VALIDATED: draft frames carry no citations, no audit id, and no
validated affordances, and the terminal `result` frame remains the only
validated artifact. See docs/superpowers/specs/2026-08-10-ask-sse-live-draft-design.md.
```

- [ ] **Step 2: Amend the turn_gate docstring.** In its module docstring the
  claim that it is the ONLY place model-authored bytes become user-visible
  gains one sentence:

```
Amended 2026-08-10: the flag-gated live-draft SSE channel (REGWATCH_LIVE_DRAFT,
see grounded_qa._stream_structured) may emit un-gated PROVISIONAL bytes; the
gate remains the only source of VALIDATED user-visible text.
```

- [ ] **Step 3: Amend the ask_core docstring.** Replace the sentence
  `There is deliberately NO token sink here -- answer text is replayed by the
  shell after the audit write, so the core emits no user-visible bytes at all.`
  with:

```
There is deliberately NO token sink here -- answer text is replayed by the
shell after the audit write. The ONLY un-gated bytes the core may emit ride
the dual-gated provisional draft channel (``on_draft``/``on_draft_reset``,
owner-amended INV-1, 2026-08-10); they are never presented as validated.
```

- [ ] **Step 4: Fix the frontend README.** Replace the paragraph at
  README.md:98-103 claiming there is no /query/stream endpoint with:

```markdown
`POST /query/stream` (SSE) streams `status` progress frames, post-audit
`token` replay frames, optional flag-gated provisional `draft` /
`draft_reset` frames, and exactly one terminal `result` frame; the client
falls back to plain `POST /query` if the stream fails (lib/api.ts).
```

- [ ] **Step 5: Rewrite POLYGLOT R3** (lines 97-101) to state that
  pre-validation provisional streaming was deliberately reversed at commit
  0a96f7e and returns only as the flag-gated draft channel under amended
  INV-1, citing the spec path.

- [ ] **Step 6: Verify zero behavior change and commit**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_streaming_synthesis.py -q && uv run ruff check src && uv run black --check src`
Expected: all pass (docstrings only).

```bash
git add docs/PROJECT_SPEC.md docs/POLYGLOT_TARGET_2026-07-10.md src/regwatch/generate/turn_gate.py src/regwatch/generate/grounded_qa.py regwatch/frontend/README.md
git commit -m "docs: record the INV-1 live-draft amendment; truth-up streaming docs"
```

---

### Task 2: G1 live probe (HARD GATE - no Task 3 until answered in writing)

**Files:**
- Create: `scripts/probe_stream_format.py`
- Create: `docs/superpowers/specs/2026-08-10-g1-probe-results.md`

**Interfaces:**
- Produces: the four written facts L1 consumes: (a) delta incrementality,
  (b) reasoning surface (typed field vs Harmony-in-content vs pre-stripped),
  (c) `model` presence on the first event, (d) the served model id.

- [ ] **Step 1: STOP - owner input required.** Ask the owner which
  Databricks CLI profile to use (never auto-select; profiles: DEFAULT,
  amneal, regwatch) and confirm the endpoint name (repo docs say alias
  `workspace.default.regwatch`). Load the `databricks-core` +
  `databricks-model-serving` skills before running CLI commands.

- [ ] **Step 2: Write the probe script**

```python
"""One-shot G1 probe: stream a v6-style prose prompt and record wire facts.

Usage: python scripts/probe_stream_format.py --base-url URL --token TOK --endpoint NAME
Prints one JSON report; makes exactly 3 streamed calls. Never commits secrets.
"""

from __future__ import annotations

import argparse
import json

from openai import OpenAI

PROBE_SYSTEM = "You answer in 2-3 short cited sentences using [1] style markers."
PROBE_USER = (
    "Passages:\n[1] A fasting bioequivalence study with 36 subjects is "
    "recommended.\n\nQuestion: What study design is recommended?"
)


def probe(client: OpenAI, endpoint: str) -> dict[str, object]:
    events = client.chat.completions.create(
        model=endpoint,
        messages=[
            {"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": PROBE_USER},
        ],
        temperature=0.0,
        max_tokens=400,
        stream=True,
        stream_options={"include_usage": True},
    )
    deltas: list[str] = []
    first_model: str | None = None
    models: list[str | None] = []
    reasoning_fields: set[str] = set()
    n = 0
    for event in events:
        n += 1
        models.append(getattr(event, "model", None))
        if n == 1:
            first_model = getattr(event, "model", None)
        for choice in getattr(event, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                deltas.append(content)
            for field in ("reasoning_content", "reasoning", "thinking"):
                if getattr(delta, field, None):
                    reasoning_fields.add(field)
    text = "".join(deltas)
    return {
        "event_count": n,
        "content_delta_count": len(deltas),
        "incremental": len(deltas) > 3,
        "first_event_model": first_model,
        "served_models_seen": sorted({m for m in models if m}),
        "typed_reasoning_fields": sorted(reasoning_fields),
        "harmony_markup_in_content": "<|channel|>" in text or "<|message|>" in text,
        "think_tags_in_content": "<think>" in text.lower(),
        "content_preview": text[:400],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--endpoint", required=True)
    args = ap.parse_args()
    client = OpenAI(base_url=args.base_url, api_key=args.token)
    reports = [probe(client, args.endpoint) for _ in range(3)]
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run it** with the owner-chosen profile:

```bash
TOKEN=$(databricks auth token -p <PROFILE> | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
HOST=$(databricks auth env -p <PROFILE> | grep DATABRICKS_HOST | cut -d= -f2)
python scripts/probe_stream_format.py --base-url "$HOST/serving-endpoints" --token "$TOKEN" --endpoint workspace.default.regwatch
```

- [ ] **Step 4: Record results** in
  `docs/superpowers/specs/2026-08-10-g1-probe-results.md`: the four facts
  (a)-(d) verbatim from the JSON, plus the raw content_preview. DECISIONS:
  if `incremental` is false -> STOP, consult owner (L1 collapses to atomic
  reveal). If `harmony_markup_in_content` is true -> Task 3's scrubber MUST
  handle the Harmony delimiters AND escalate to owner: the buffered prose
  path leaks reasoning today (P0 for the prose flip, separate from this
  feature). If typed_reasoning_fields only -> the content channel is already
  clean and the scrubber is belt-and-braces.

- [ ] **Step 5: Commit** (probe script + results note; never the token)

```bash
git add scripts/probe_stream_format.py docs/superpowers/specs/2026-08-10-g1-probe-results.md
git commit -m "docs(ask): G1 stream-format probe + recorded wire facts"
```

---

### Task 3: L1 - incremental DatabricksProvider.stream() + _StreamScrubber

**Files:**
- Modify: `src/regwatch/generate/llm.py` (LLMStreamChunk ~line 53; new
  _StreamScrubber after _visible_answer_text ~line 557; rewrite
  DatabricksProvider.stream ~line 955)
- Test: `tests/test_stream_scrubber.py` (new)

**Interfaces:**
- Produces: `LLMStreamChunk.reset: bool = False` (new field; a True chunk
  tells the consumer to discard all deltas received so far).
- Produces: `DatabricksProvider.stream()` now yields many true incremental
  deltas (scrubbed), then the terminal done=True chunk whose `response` is
  byte-identical to today's buffered result.
- Produces: `_StreamScrubber` with `push(chunk: str) -> tuple[str, bool]`
  (visible delta, retroactive_reset) and `flush() -> str`.
- Consumes: G1 facts from Task 2 (delimiter set; D1 first-event answer).

- [ ] **Step 1: Add the `reset` field** to LLMStreamChunk (llm.py:53-64):

```python
    delta: str = ""
    done: bool = False
    response: LLMResponse | None = None
    # True = retroactive invalidation: a late close-delimiter revealed that
    # earlier deltas may have been private reasoning. Consumers discard all
    # deltas received so far and start over; the terminal response is
    # unaffected (it is built from the full buffered scrub).
    reset: bool = False
```

- [ ] **Step 2: Write the failing scrubber tests** (`tests/test_stream_scrubber.py`):

```python
"""_StreamScrubber: the incremental twin of _visible_answer_text.

Property pinned here: for ANY split of the wire text into chunks, the
concatenated visible output (pushes + flush) equals _visible_answer_text of
the full text, and no push ever emits private-block content or a partial
delimiter.
"""

from __future__ import annotations

import pytest
from regwatch.generate.llm import _StreamScrubber, _visible_answer_text

CASES = [
    "plain answer with no markup at all.",
    "<|channel>thought\nsecret reasoning<channel|>The visible answer.",
    "<think>secret</think>Visible after think.",
    "answer ends with partial <|chan",
    "<|channel>thought\nunterminated secret",
    "pre <think>a</think>mid<think>b</think> post",
]


def _every_split(text: str, parts: int = 2):
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_stream_scrubber.py -q`
Expected: FAIL / collection error - `_StreamScrubber` not defined.

- [ ] **Step 4: Implement _StreamScrubber** in llm.py, directly after
  `_visible_answer_text`. Extend `_OPENERS`/`_CLOSER` pairs with the
  G1-confirmed Harmony forms if (and only if) Task 2 recorded
  harmony_markup_in_content=true (add `("<|channel|>analysis<|message|>",
  "<|end|>")` and treat `<|channel|>final<|message|>` as a visible-channel
  opener to strip):

```python
class _StreamScrubber:
    """Incremental twin of ``_visible_answer_text`` for live draft streaming.

    push() returns (visible_delta, retroactive_reset). It never emits text
    inside a private block, never emits a tail that could still grow into a
    delimiter, and signals reset=True on a stray close-delimiter (everything
    already emitted may have been reasoning; the consumer discards it).
    flush() drops an unterminated private block, mirroring the buffered
    scrubber's conservative choice.
    """

    # (compiled opener, literal closer) pairs, buffered-scrubber-aligned.
    _PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
        (_THOUGHT_CHANNEL_START, "<channel|>"),
        (_THINK_TAG_START, "</think>"),
    )
    # Longest text that could still be an incomplete opener/closer; holding
    # this much tail is what makes split-across-chunks delimiters safe.
    _MAX_HOLD = max(
        len("<|channel>thought") + 2,  # opener + optional \r\n
        len("<channel|>"),
        len("<think>"),
        len("</think>"),
    )

    def __init__(self) -> None:
        self._buf = ""
        self._closer: str | None = None  # inside a private block when set

    def _could_be_delimiter_prefix(self, tail: str) -> bool:
        probes = ["<|channel>thought", "<think>", "</think>", "<channel|>", "<|think|>"]
        return any(p.startswith(tail) for p in probes if tail)

    def push(self, chunk: str) -> tuple[str, bool]:
        self._buf += chunk
        visible: list[str] = []
        reset = False
        while True:
            if self._closer is not None:
                close = self._buf.find(self._closer)
                if close < 0:
                    # Keep only enough tail to complete the closer.
                    self._buf = self._buf[-(len(self._closer) - 1) :]
                    return ("".join(visible), reset)
                self._buf = self._buf[close + len(self._closer) :]
                self._closer = None
                continue
            # Stray closers first: everything before one is suspect.
            stray_at = self._buf.find("<channel|>")
            think_at = self._buf.lower().find("</think>")
            if think_at != -1 and (stray_at == -1 or think_at < stray_at):
                stray_at, stray_len = think_at, len("</think>")
            elif stray_at != -1:
                stray_len = len("<channel|>")
            else:
                stray_len = 0
            opener = None
            opener_match = None
            for start, closer in self._PAIRS:
                m = start.search(self._buf)
                if m and (opener_match is None or m.start() < opener_match.start()):
                    opener_match, opener = m, closer
            if opener_match is not None and (stray_len == 0 or opener_match.start() < stray_at):
                visible.append(self._buf[: opener_match.start()])
                self._buf = self._buf[opener_match.end() :]
                self._closer = opener
                continue
            if stray_len:
                visible.clear()
                reset = True
                self._buf = self._buf[stray_at + stray_len :]
                continue
            break
        # Emit all but a tail that could still become a delimiter.
        hold = 0
        for i in range(min(self._MAX_HOLD, len(self._buf)), 0, -1):
            if self._could_be_delimiter_prefix(self._buf[-i:]):
                hold = i
                break
        out = self._buf[: len(self._buf) - hold] if hold else self._buf
        self._buf = self._buf[len(self._buf) - hold :] if hold else ""
        visible.append(out.replace("<|think|>", ""))
        return ("".join(visible), reset)

    def flush(self) -> str:
        if self._closer is not None:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out.replace("<|think|>", "")
```

- [ ] **Step 5: Run scrubber tests until green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_stream_scrubber.py -q`
Expected: PASS. Iterate on push() edge cases (the every-split property test
is the arbiter; the buffered scrubber is the oracle).

- [ ] **Step 6: Write the failing provider-stream tests** (append to
  `tests/test_stream_scrubber.py`):

```python
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
        def __init__(self, events: list[object]) -> None:
            self._events = events

        def create(self, **kwargs: object) -> object:
            if not kwargs.get("stream"):
                raise AssertionError("buffered fallback must not be reached")
            return iter(self._events)

    def __init__(self, events: list[object]) -> None:
        self.chat = type("C", (), {"completions": self._Completions(events)})()


def _provider(events: list[object], **kw: object) -> "DatabricksProvider":
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

    events = [_FakeEvent(content="text", model=None), _FakeEvent(content=None, finish_reason="stop")]
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
```

- [ ] **Step 7: Run to verify the new tests fail**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_stream_scrubber.py -q`
Expected: the six provider tests FAIL (stream() is still atomic).

- [ ] **Step 8: Rewrite DatabricksProvider.stream()** (llm.py:955-989),
  keeping `_complete_stream` for nothing - fold its accumulation in and
  DELETE it (its docstring's buffering rationale moves onto the scrubber):

```python
    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        """True incremental streaming with an in-adapter reasoning scrubber.

        Deltas are scrubbed by _StreamScrubber, so control/reasoning markup is
        parsed out at this boundary and never reaches a consumer. The D1 check
        binds on the FIRST event that reports ``model`` (owner decision
        2026-08-10: deltas are NOT held waiting for late metadata; a stream
        that never reports raises at the end exactly like complete()). After
        the first yielded delta the buffered fallback is DISABLED - a re-send
        would paint the whole answer twice.

        The terminal chunk's response is built from the full buffered scrub of
        every raw part, so it stays byte-identical to the pre-streaming
        implementation on every input.
        """
        client = self._client_or_create()
        try:
            events = client.chat.completions.create(
                **self._request_kwargs(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
            )
        except Exception:
            # Endpoint without SSE/stream_options support; nothing yielded yet.
            yield from _buffered_stream(
                self, messages, temperature=temperature, max_tokens=max_tokens
            )
            return
        scrub = _StreamScrubber()
        parts: list[str] = []
        usage = LLMUsage()
        finish_reason: Any = None
        last_event: Any = None
        served: str | None = None
        d1_checked = False
        yielded = False
        saw_choice = False
        iterator = iter(events)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except D1ResidencyError:
                raise
            except Exception:
                if yielded:
                    # No re-send after first yield: a fallback would repaint
                    # the full answer after a partial one.
                    raise
                yield from _buffered_stream(
                    self, messages, temperature=temperature, max_tokens=max_tokens
                )
                return
            last_event = event
            reported = getattr(event, "model", None)
            if reported:
                served = reported
                if not d1_checked:
                    # Raises D1ResidencyError pre-yield when the wire reports
                    # early (the G1-recorded common case); a late report binds
                    # here mid-stream instead of holding deltas.
                    self._check_served_model(reported)
                    d1_checked = True
            event_usage = _usage_from(event, "prompt_tokens", "completion_tokens")
            if event_usage.input_tokens is not None:
                usage.input_tokens = event_usage.input_tokens
            if event_usage.output_tokens is not None:
                usage.output_tokens = event_usage.output_tokens
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            saw_choice = True
            choice = choices[0]
            candidate_finish = getattr(choice, "finish_reason", None)
            if candidate_finish is not None:
                finish_reason = candidate_finish
            delta = getattr(choice, "delta", None)
            # Deliberately ignore delta.reasoning_content / reasoning / thinking.
            raw = _chat_content_text(getattr(delta, "content", None))
            parts.append(raw)
            visible, reset = scrub.push(raw)
            if reset:
                yielded = True
                yield LLMStreamChunk(reset=True)
            if visible:
                yielded = True
                yield LLMStreamChunk(delta=visible)
        tail = scrub.flush()
        if tail:
            yielded = True
            yield LLMStreamChunk(delta=tail)
        if not d1_checked:
            self._check_served_model(served)  # raises under enforcement; logs otherwise
        if not saw_choice:
            raise RuntimeError("databricks chat stream returned no choices")
        self._raise_for_finish_reason(finish_reason)
        yield LLMStreamChunk(
            done=True,
            response=LLMResponse(
                text=_visible_answer_text("".join(parts)),
                model=served or self.model,
                raw=_safe_chat_raw(last_event, finish_reason=finish_reason),
                usage=usage,
            ),
        )
```

Delete `_complete_stream` (llm.py:886-953) in the same edit; nothing else
calls it (verify: `grep -rn "_complete_stream" src tests tests_contract`).

- [ ] **Step 9: Run the full L1 gate**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_stream_scrubber.py tests/test_streaming_synthesis.py tests/test_llm*.py -q`
Expected: PASS (existing Databricks stream tests may assert the old atomic
behavior - if any do, re-scope them to the new contract in this task: the
property "terminal response byte-identical to complete()" is unchanged).

- [ ] **Step 10: mypy + commit**

Run: `env -u VIRTUAL_ENV uv run mypy src tests tests_contract && uv run black . && uv run ruff check .`

```bash
git add src/regwatch/generate/llm.py tests/test_stream_scrubber.py
git commit -m "feat(llm): true incremental Databricks streaming behind an adapter-boundary scrubber"
```

---

### Task 4: L2 - flags, request field, _stream_structured, threading, SSE frames

**Files:**
- Modify: `config/settings.py` (beside prose_synthesis_enabled, line ~146)
- Modify: `src/regwatch/generate/grounded_qa.py` (_stream_structured beside
  _complete_structured ~line 874; _synthesize_and_admit signature ~1836 and
  call site ~1927; ask_core ~2212-2250; ask ~2459-2568)
- Modify: `src/regwatch/api/main.py` (QueryRequest ~635; _sse_event ~904;
  _query_event_stream ~918-1012; query_stream ~1015)
- Test: `tests/test_live_draft.py` (new)

**Interfaces:**
- Produces: `Settings.live_draft_enabled: bool` (alias REGWATCH_LIVE_DRAFT).
- Produces: `QueryRequest.live_draft: bool = False`.
- Produces: `ask(..., on_draft: Callable[[str], None] | None = None,
  on_draft_reset: Callable[[], None] | None = None)` and the same two
  params on `ask_core` and `_synthesize_and_admit` (as `_emit_draft` /
  `_emit_draft_reset` best-effort closures).
- Produces: SSE events `draft` (`{"delta": str}`) and `draft_reset` (`{}`).
- Consumes: `provider.stream()` incremental contract + `LLMStreamChunk.reset`
  from Task 3; `PROSE_NO_EVIDENCE_SENTINEL` from prose_turn.

- [ ] **Step 1: Write the failing pipeline tests** (`tests/test_live_draft.py`):

```python
"""The dual-gated live-draft channel: pipeline half (L2).

Pins: drafts stream only in prose mode with a sink attached; the sentinel
refusal is NEVER painted; the truncation retry emits a reset; the gate still
operates on the COMPLETE text; flag-off turns are byte-identical to today.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMMessage, LLMResponse, LLMStreamChunk
from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL
from tests.test_invariants import _meta, _seed_corpus

_QUESTION = "What study design is recommended?"
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]
_PROSE = "A fasting bioequivalence study is recommended [1]."


class _StreamingStub:
    """Prose synthesizer stub with a REAL incremental stream()."""

    name = "stub-streaming"

    def __init__(self, *stream_texts: str, chunk: int = 8) -> None:
        self._texts = list(stream_texts)
        self._chunk = chunk
        self.stream_calls = 0
        self.complete_calls = 0

    def complete(self, *a: object, **kw: object) -> LLMResponse:
        self.complete_calls += 1
        return LLMResponse(text=self._texts[0], model="stub-streaming")

    def stream(self, *a: object, **kw: object) -> Iterator[LLMStreamChunk]:
        text = self._texts[min(self.stream_calls, len(self._texts) - 1)]
        self.stream_calls += 1
        if text == "<raise-truncated>":
            raise RuntimeError("finish_reason=length")
        if text.startswith("<truncate-then:"):
            # First call: emit a partial delta, then raise truncation.
            if self.stream_calls == 1:
                yield LLMStreamChunk(delta="partial that will be reset ")
                raise RuntimeError("finish_reason=length")
            text = text[len("<truncate-then:") : -1]
        for i in range(0, len(text), self._chunk):
            yield LLMStreamChunk(delta=text[i : i + self._chunk])
        yield LLMStreamChunk(done=True, response=LLMResponse(text=text, model="stub-streaming"))


@pytest.fixture()
def prose_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _use(monkeypatch: pytest.MonkeyPatch, provider: _StreamingStub) -> _StreamingStub:
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    return provider


def test_drafts_stream_live_and_gate_runs_on_complete_text(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(_PROSE))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert prov.stream_calls == 1 and prov.complete_calls == 0
    assert "".join(drafts) == _PROSE  # raw model prose, [1] marker included
    assert result.refused is False
    assert "[PSG_020503, p.3]" in result.answer  # gate rendered from FULL text


def test_no_draft_sink_means_buffered_call_and_no_stream(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(_PROSE))
    result = qa_mod.ask(_QUESTION)
    assert prov.stream_calls == 0 and prov.complete_calls == 1
    assert result.refused is False


def test_json_mode_never_streams_even_with_a_draft_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # prose flag OFF: the v5 JSON arm must be structurally unable to stream.
    from tests.conftest import synth_turn_json

    _seed_corpus(_CORPUS)
    turn = synth_turn_json(
        [("A fasting bioequivalence study is recommended", [("PSG_020503", 3)])]
    )
    prov = _use(monkeypatch, _StreamingStub(turn))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert prov.stream_calls == 0 and prov.complete_calls == 1
    assert drafts == []
    assert result.refused is False


def test_sentinel_refusal_is_never_painted_as_a_draft(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub(PROSE_NO_EVIDENCE_SENTINEL, chunk=3))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert drafts == []  # held: every prefix of the sentinel is withheld
    assert result.refused is True


def test_truncation_retry_emits_reset_then_restreams(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(f"<truncate-then:{_PROSE}>"))
    drafts: list[str] = []
    resets: list[bool] = []
    result = qa_mod.ask(
        _QUESTION, on_draft=drafts.append, on_draft_reset=lambda: resets.append(True)
    )
    assert prov.stream_calls == 2
    assert resets == [True]
    assert result.refused is False
    # Post-reset deltas reassemble to attempt 2's text exactly.
    reset_marker = drafts.index("partial that will be reset ") + 1
    assert "".join(drafts[reset_marker:]) == _PROSE
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_live_draft.py -q`
Expected: FAIL - `ask() got an unexpected keyword argument 'on_draft'`.

- [ ] **Step 3: Settings + request field.** In `config/settings.py`, directly
  below `prose_synthesis_enabled` (line ~148):

```python
    # Live provisional draft streaming over SSE (owner-amended INV-1,
    # 2026-08-10). Dark by default; effective only when prose synthesis is
    # also on AND the request opts in. Alias so the prod flip is a REGWATCH_*
    # Fly secret like the prose flag.
    live_draft_enabled: bool = Field(default=False, validation_alias="REGWATCH_LIVE_DRAFT")
```

In `src/regwatch/api/main.py` QueryRequest (after `session_id`, line ~644):

```python
    # Per-request opt-in for the provisional draft SSE channel. Ignored by the
    # blocking /query route and whenever the server-side dual gate is off.
    live_draft: bool = False
```

- [ ] **Step 4: _stream_structured** in grounded_qa.py, directly after
  `_complete_structured` (~line 874):

```python
def _stream_structured(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    telemetry: dict[str, Any] | None = None,
    on_delta: Callable[[str], None],
    on_reset: Callable[[], None],
) -> LLMResponse:
    """Streaming twin of ``_complete_structured`` - prose mode only.

    Forwards scrubbed deltas to ``on_delta`` while the model writes, then
    returns the terminal LLMResponse; the parse/admit gate downstream always
    operates on that COMPLETE text, exactly as on the buffered path. The
    sentinel hold lives HERE, provider-agnostically (Echo streams too): no
    delta is forwarded while the accumulated text is still a prefix of
    PROSE_NO_EVIDENCE_SENTINEL, so a refusal never paints. Truncation keeps
    the same one-2x-retry policy; because attempt 1's deltas may already be
    on the wire, the retry emits ``on_reset`` first (the client discards the
    partial draft) and re-streams. D1ResidencyError re-raises first, exactly
    like the buffered twin.
    """
    from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL

    capped = min(max_tokens, _SYNTH_MAX_TOKENS_CEILING)
    if telemetry is not None:
        telemetry["first_budget"] = capped

    def _attempt(budget: int) -> tuple[LLMResponse | None, bool]:
        """(terminal response | None, any_delta_forwarded)."""
        held = ""
        holding = True
        forwarded = False

        def _forward(text: str) -> None:
            nonlocal forwarded
            if not text:
                return
            forwarded = True
            try:
                on_delta(text)
            except Exception:  # broad: a draft sink is cosmetic, never fatal
                log.debug("on_draft_failed", exc_info=True)

        response: LLMResponse | None = None
        for chunk in provider.stream(
            messages, temperature=_SYNTH_TEMPERATURE, max_tokens=budget
        ):
            if chunk.reset:
                held, holding = "", True
                if forwarded:
                    try:
                        on_reset()
                    except Exception:
                        log.debug("on_draft_reset_failed", exc_info=True)
                continue
            if chunk.done:
                response = chunk.response
                break
            if holding:
                held += chunk.delta
                if PROSE_NO_EVIDENCE_SENTINEL.startswith(held):
                    continue  # still a possible refusal prefix - keep holding
                holding = False
                _forward(held)
                held = ""
                continue
            _forward(chunk.delta)
        return response, forwarded

    try:
        response, _ = _attempt(capped)
    except D1ResidencyError:
        raise
    except RuntimeError as exc:
        retry_budget = min(capped * 2, _SYNTH_MAX_TOKENS_CEILING)
        if retry_budget <= capped:
            raise
        if telemetry is not None:
            telemetry["synthesis_retried"] = True
            telemetry["retry_budget"] = retry_budget
        log.warning(
            "qa_synthesis_truncation_retry", old=capped, new=retry_budget, error=str(exc)[:200]
        )
        try:
            on_reset()
        except Exception:
            log.debug("on_draft_reset_failed", exc_info=True)
        response, _ = _attempt(retry_budget)
    if response is None:
        raise RuntimeError("provider stream ended without a terminal response chunk")
    return response
```

- [ ] **Step 5: Thread on_draft.** Three signature edits + one call-site edit:

(a) `_synthesize_and_admit` (line ~1836) gains two params after `_recent_turns`:

```python
    _emit_draft: Callable[[str], None] | None = None,
    _emit_draft_reset: Callable[[], None] | None = None,
```

and its synthesis call (line ~1927) becomes:

```python
    try:
        if prose_mode and _emit_draft is not None and _emit_draft_reset is not None:
            response = _stream_structured(
                provider,
                synth_messages,
                max_tokens=s.synthesizer_max_tokens,
                telemetry=synth_telemetry,
                on_delta=_emit_draft,
                on_reset=_emit_draft_reset,
            )
        else:
            response = _complete_structured(
                provider,
                synth_messages,
                max_tokens=s.synthesizer_max_tokens,
                response_format=None if prose_mode else "json",
                telemetry=synth_telemetry,
            )
    except Exception as exc:  # provider transport error (timeout / 429 / 5xx)
```

(b) `ask_core` (line ~2212) gains, after `on_progress`:

```python
    on_draft: Callable[[str], None] | None = None,
    on_draft_reset: Callable[[], None] | None = None,
```

and forwards them into `_synthesize_and_admit` at its existing call site
(search `_recent_turns=_recent_turns,` ~line 2455) as
`_emit_draft=on_draft, _emit_draft_reset=on_draft_reset,`. Amend the
docstring per Task 1 Step 3 (already done there).

(c) `ask()` (line ~2459) gains the same two params after `on_token` and
forwards them in the `ask_core(...)` call (line ~2541). Docstring addition:

```
    ``on_draft`` / ``on_draft_reset`` (optional) receive LIVE, un-gated,
    provisional prose deltas (and retroactive discard signals) during
    synthesis, prose mode only -- the dual-gated draft channel under the
    owner-amended INV-1 (2026-08-10). Never validated, never replayed on
    declines the way on_token is; the terminal result stays authoritative.
```

- [ ] **Step 6: Run the pipeline tests**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_live_draft.py tests/test_streaming_synthesis.py -q`
Expected: test_live_draft.py PASSES; test_streaming_synthesis.py PASSES
UNCHANGED (its stubs never pass on_draft, so nothing streams).

- [ ] **Step 7: SSE wiring in main.py.** _sse_event docstring (line ~904)
  now names five events. In `_query_event_stream`:

after the `on_token` closure (line ~951):

```python
    def on_draft(delta: str) -> None:
        # LIVE un-gated prose from the worker thread - provisional by
        # contract; the client renders it only as a draft.
        loop.call_soon_threadsafe(queue.put_nowait, ("draft", delta))

    def on_draft_reset() -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("draft_reset", None))
```

`_run()` computes the single conjunction and passes the callbacks:

```python
    s = get_settings()
    draft_on = bool(s.live_draft_enabled and s.prose_synthesis_enabled and req.live_draft)
    ...
            result = await _dispatch_ask(
                question=req.question,
                filters=req.filters,
                k=req.k,
                session_id=req.session_id,
                user_id=user_id,
                on_progress=on_progress,
                on_token=on_token,
                on_draft=on_draft if draft_on else None,
                on_draft_reset=on_draft_reset if draft_on else None,
            )
```

drain loop, after the `token` branch (line ~998):

```python
            if kind == "draft":
                yield _sse_event("draft", {"delta": payload})
                continue
            if kind == "draft_reset":
                yield _sse_event("draft_reset", {})
                continue
```

- [ ] **Step 8: Wire-level unit test.** Append to tests/test_live_draft.py
  (client/session helpers exactly as tests/test_streaming_synthesis.py:396-449
  uses them):

```python
def test_query_stream_emits_draft_frames_only_when_dual_gated(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    from tests.conftest import create_user, session_client
    from tests.test_query_stream import _parse_sse, _stream

    monkeypatch.setenv("REGWATCH_LIVE_DRAFT", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub(_PROSE))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, _QUESTION, live_draft=True).text)
        events = [e for e, _ in frames]
        assert "draft" in events
        assert events[-1] == "result"
        # Opt-out request on the same flag-on server: zero draft frames.
        frames2 = _parse_sse(_stream(client, _QUESTION).text)
        assert "draft" not in [e for e, _ in frames2]
    finally:
        client.__exit__(None, None, None)
```

Check `tests/test_query_stream.py::_stream`'s signature first; if it does not
accept extra body fields, extend it with `live_draft: bool = False` merged
into the posted JSON (default keeps every existing caller byte-identical).

- [ ] **Step 9: Full gate + commit**

Run: `env -u VIRTUAL_ENV uv run pytest tests/ -q && uv run mypy src tests tests_contract && uv run black . && uv run ruff check .`

```bash
git add config/settings.py src/regwatch/generate/grounded_qa.py src/regwatch/api/main.py tests/test_live_draft.py tests/test_query_stream.py
git commit -m "feat(ask): dual-gated live-draft SSE channel through the prose pipeline"
```

Push milestone: `git push` (Tasks 1-4 reviewed together on the PR).

---

### Task 5: Withdrawal signal (draft_withdrawn on the result)

**Files:**
- Modify: `src/regwatch/api/main.py` (QueryResponse model + _query_event_stream)
- Test: extend `tests/test_live_draft.py`

**Interfaces:**
- Produces: `QueryResponse.draft_withdrawn: str | None = None` with values
  "refused" | "clarify" | "error" | "meta" | "scope_warning" | "partial";
  set ONLY on streams that emitted at least one draft frame.

- [ ] **Step 1: Failing test** (append to tests/test_live_draft.py):

```python
def test_result_carries_draft_withdrawn_when_a_painted_draft_dies(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    """A stub that streams fluent prose which the gate then REFUSES (its one
    citation is fabricated) must stamp draft_withdrawn='refused' on the result
    frame -- and a clean answer turn must stamp nothing."""
    from tests.conftest import create_user, session_client
    from tests.test_query_stream import _parse_sse, _stream

    monkeypatch.setenv("REGWATCH_LIVE_DRAFT", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub("A fabricated dose claim [7]."))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, _QUESTION, live_draft=True).text)
        events = [e for e, _ in frames]
        assert "draft" in events  # the fluent draft painted
        result = json.loads([d for e, d in frames if e == "result"][0])
        assert result["refused"] is True
        assert result["draft_withdrawn"] == "refused"
    finally:
        client.__exit__(None, None, None)
```

Also assert the clean-path inverse inside
`test_query_stream_emits_draft_frames_only_when_dual_gated`:
`assert json.loads([d for e, d in frames if e == "result"][0])["draft_withdrawn"] is None`.

- [ ] **Step 2: Verify it fails** (KeyError/AssertionError on draft_withdrawn).

- [ ] **Step 3: Implement.** In main.py: find the QueryResponse pydantic
  model (`grep -n "class QueryResponse" src/regwatch/api/main.py`) and add:

```python
    # Set only on /query/stream turns that painted at least one provisional
    # draft frame which the gate then withdrew (refuse/clarify/error/meta/
    # scope_warning) or partially dropped. The client keys its withdrawal
    # note on this server-declared value - never on text diffing.
    draft_withdrawn: str | None = None
```

In `_query_event_stream`: track emission and stamp the built response:

```python
    draft_frames_sent = False   # beside the closures; set in the drain loop
    ...
            if kind == "draft":
                draft_frames_sent = True
                yield _sse_event("draft", {"delta": payload})
                continue
    ...
            if kind == "result":
                ...
                response = await run_in_threadpool(_build_query_response, payload)
                if draft_frames_sent:
                    from regwatch.generate.turn_gate import PARTIAL_DROP_DISCLOSURE

                    if response.status not in ("answer", "summary"):
                        response.draft_withdrawn = response.status
                    elif PARTIAL_DROP_DISCLOSURE in response.answer:
                        response.draft_withdrawn = "partial"
```

(the `import json` for the test lives at the top of tests/test_live_draft.py:
`import json`.)

- [ ] **Step 4: Run, gate, commit**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_live_draft.py tests/test_query_stream.py -q && uv run mypy src tests tests_contract`

```bash
git add src/regwatch/api/main.py tests/test_live_draft.py
git commit -m "feat(ask): server-declared draft_withdrawn signal on the result frame"
```

---

### Task 6: Re-scope the legacy streaming pins honestly

**Files:**
- Modify: `tests/test_streaming_synthesis.py` (module docstring + one test)

**Interfaces:** none new; keeps Task 4's contract pinned from the flag-off side.

- [ ] **Step 1: Amend the module docstring** (lines 1-21): after the numbered
  guarantees add:

```
Amended 2026-08-10: these pins now describe the DEFAULT (flag-off) contract.
The dual-gated live-draft channel (REGWATCH_LIVE_DRAFT + prose + request
opt-in) may stream provisional model prose BEFORE the gate; its own pins
live in tests/test_live_draft.py. Every guarantee above still holds verbatim
whenever the draft gate is closed - which is every turn in prod today.
```

- [ ] **Step 2: Re-scope the buffered-call pin.** In
  `test_synthesis_is_one_buffered_json_call_never_provider_stream` the name
  and docstring stay accurate (json mode NEVER streams, flag or no flag) -
  no change needed. Verify `_StructuredLLM.stream`'s AssertionError message
  still holds for the json arm by running the file.

- [ ] **Step 3: Run + commit**

Run: `env -u VIRTUAL_ENV uv run pytest tests/test_streaming_synthesis.py -q`

```bash
git add tests/test_streaming_synthesis.py
git commit -m "test: re-scope streaming-synthesis pins to the flag-off contract"
```

---

### Task 7: S31 contract scenario (real Go edge + uvicorn + Postgres)

**Files:**
- Modify: `tests_contract/conftest.py` (_FLAVOR_OVERRIDES ~line 559; scenario
  matrix comment ~line 11)
- Modify: `tests_contract/test_query_stream.py` (event-set widening + test_s31)

**Interfaces:**
- Consumes: the echo provider's prose branch (llm.py:243-267) and
  EchoLLMProvider.stream (llm.py:272-288); the `live_draft` flavor env.

- [ ] **Step 1: Add the flavor** to _FLAVOR_OVERRIDES:

```python
    # S31: both server halves of the live-draft dual gate on; echo streams its
    # deterministic two-chunk prose so draft frames are wire-reachable with no
    # external model. Fenced from prod by REGWATCH_ALLOW_TEST_PROVIDERS.
    "live_draft": {"REGWATCH_PROSE_SYNTHESIS": "1", "REGWATCH_LIVE_DRAFT": "1"},
    # S31b: refusal under the live-draft flavor (echo emits the NO_EVIDENCE
    # sentinel, which the sentinel hold must swallow entirely).
    "live_draft_refusal": {
        "REGWATCH_PROSE_SYNTHESIS": "1",
        "REGWATCH_LIVE_DRAFT": "1",
        "REGWATCH_ECHO_FORCE_REFUSAL": "1",
    },
```

Update the scenario-matrix comment (line ~19) to add
`S31 test_query_stream.py live-draft frame grammar`.

- [ ] **Step 2: Write test_s31** in tests_contract/test_query_stream.py,
  following test_s19's structure (same helpers: seed_answerable_corpus,
  edge_login, _stream_to_eof, query_log_count):

```python
def test_s31_live_draft_frame_grammar(
    live_draft_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    seed_answerable_corpus()
    client = edge_login(live_draft_stack)
    rows_before = query_log_count()

    # Opted-in request: draft frames appear, then the normal token replay and
    # exactly one terminal result; one audit row; Go relayed the new event
    # name byte-transparently (this test runs through the real edge).
    status, content_type, sse = _stream_to_eof(
        client, {"question": ANSWERABLE_QUESTION, "live_draft": True}
    )
    assert status == 200 and "text/event-stream" in content_type
    events = sse.events()
    assert set(events) <= {"status", "draft", "draft_reset", "token", "result"}
    assert events.count("draft") >= 1
    result = sse.single_result()
    assert result["status"] == "answer"
    assert result["draft_withdrawn"] is None
    for data in sse.data_for("draft"):
        assert set(json.loads(data).keys()) == {"delta"}
    # Drafts are the RAW prose ([n] marker form), never the rendered answer.
    drafted = "".join(json.loads(d)["delta"] for d in sse.data_for("draft"))
    assert drafted  # non-empty
    assert query_log_count() == rows_before + 1

    # Same server, no opt-in: zero draft frames, grammar unchanged from S19.
    _, _, sse2 = _stream_to_eof(client, {"question": ANSWERABLE_QUESTION})
    assert "draft" not in sse2.events()


def test_s31b_live_draft_refusal_paints_nothing(
    live_draft_refusal_stack: Stack, edge_login: Callable[..., EdgeClient]
) -> None:
    seed_answerable_corpus()
    client = edge_login(live_draft_refusal_stack)
    status, _, sse = _stream_to_eof(
        client, {"question": ANSWERABLE_QUESTION, "live_draft": True}
    )
    assert status == 200
    events = sse.events()
    # The sentinel hold swallows the whole refusal: no draft ever paints, no
    # token replays (S23's guarantee), and no withdrawal is needed.
    assert "draft" not in events
    assert "token" not in events
    result = sse.single_result()
    assert result["refused"] is True
    assert result["draft_withdrawn"] is None
```

Stack fixtures: add `live_draft_stack` / `live_draft_refusal_stack` exactly
the way the existing per-flavor fixtures are defined (grep
`def forced_refusal_stack` in conftest.py and mirror it).

- [ ] **Step 3: Run the contract lane** (needs :5499 Postgres + built proxy):

```bash
(cd go && go build ./cmd/proxy)
env -u VIRTUAL_ENV uv run pytest tests_contract/test_query_stream.py -q
```

Expected: S19/S20/S21a/S23 UNCHANGED and green; S31/S31b green. Then run the
full suite once with `GO_NATIVE_QUERY` both values if the harness
parameterizes it (follow tests_contract/README or conftest behavior).

- [ ] **Step 4: Commit**

```bash
git add tests_contract/conftest.py tests_contract/test_query_stream.py
git commit -m "test(contract): S31 live-draft frame grammar over the real edge"
```

---

### Task 8: L3 frontend - client, typewriter pacing, withdrawal note

**Files:**
- Modify: `regwatch/frontend/lib/api.ts` (StreamCallbacks ~440; consumeSse
  dispatch ~499; askQueryStream ~570; QueryResponse type + normalizeQuery)
- Modify: `regwatch/frontend/lib/turns.ts` (Turn + StreamTrace + assistantTurn)
- Modify: `regwatch/frontend/components/Turns.tsx` (WithdrawnNote beside
  FallbackNote ~76; render slots; FallbackNote copy)
- Modify: `regwatch/frontend/app/(shell)/page.tsx` (pacing buffer, onDraft
  wiring ~440-486, SR copy ~459-470)
- Test: `regwatch/frontend/test/sse.test.ts`, `regwatch/frontend/test/askPage.test.tsx`

**Interfaces:**
- Consumes: `draft`/`draft_reset` SSE events, `QueryResponse.draft_withdrawn`.
- Produces: `StreamCallbacks.onDraft/onDraftReset`; `askQueryStream(...,
  liveDraft?: boolean)`; `Turn.draftWithdrawn: string | null`.

- [ ] **Step 1: Failing client tests.** In test/sse.test.ts (mirror the
  existing token-frame tests' fixture style) add: a `draft` frame invokes
  onDraft with the delta; a `draft_reset` frame invokes onDraftReset; an
  unknown event name is still ignored; the posted body carries
  `live_draft: true` when askQueryStream is called with liveDraft=true and
  omits it otherwise (assert on the fetch mock's body).

- [ ] **Step 2: api.ts.** StreamCallbacks (line ~440):

```ts
export interface StreamCallbacks {
  onStatus?: (text: string) => void;
  onToken?: (delta: string) => void;
  // LIVE un-gated provisional prose (flag-gated server-side). Never the
  // authoritative answer; rendered only as a draft.
  onDraft?: (delta: string) => void;
  // Retroactive discard: everything received via onDraft so far is invalid
  // (truncation retry or upstream reset). Clear the draft and start over.
  onDraftReset?: () => void;
}
```

consumeSse dispatch, after the `token` branch (line ~507):

```ts
    if (name === "draft") {
      try {
        const d = JSON.parse(payload) as { delta?: unknown };
        if (typeof d.delta === "string") callbacks?.onDraft?.(d.delta);
      } catch {
        // malformed draft frame is cosmetic - keep reading
      }
      return null;
    }
    if (name === "draft_reset") {
      callbacks?.onDraftReset?.();
      return null;
    }
```

askQueryStream signature gains `liveDraft: boolean = false` (after
`callbacks`, before `signal` - update the page call site in the same PR) and
the body becomes:

```ts
        body: JSON.stringify(
          liveDraft
            ? { question, filters, session_id, live_draft: true }
            : { question, filters, session_id },
        ),
```

QueryResponse type (find `interface QueryResponse` in api.ts or lib/types)
gains `draft_withdrawn?: string | null;` and normalizeQuery preserves it
(default `null` if absent - old servers).

- [ ] **Step 3: Pacing buffer in page.tsx.** Beside the `draft` state
  (line ~185):

```tsx
  // Client-side typewriter: incoming draft deltas land here and a paced
  // drain feeds `draft`, so render cadence is smooth regardless of wire
  // chunking (the server sends deltas as fast as the model writes).
  const draftBufRef = useRef("");
  const draftTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const DRAFT_TICK_MS = 33; // ~30fps
  const DRAFT_CHARS_PER_TICK = 6; // ~180 chars/s, tuned to the replay feel

  const stopDraftDrain = useCallback((discardBuffered: boolean) => {
    if (draftTimerRef.current !== null) {
      clearInterval(draftTimerRef.current);
      draftTimerRef.current = null;
    }
    if (discardBuffered) draftBufRef.current = "";
  }, []);

  const ensureDraftDrain = useCallback(() => {
    if (draftTimerRef.current !== null) return;
    draftTimerRef.current = setInterval(() => {
      const buf = draftBufRef.current;
      if (!buf) {
        stopDraftDrain(false);
        return;
      }
      const take = buf.slice(0, DRAFT_CHARS_PER_TICK);
      draftBufRef.current = buf.slice(DRAFT_CHARS_PER_TICK);
      setDraft((prev) => (prev ?? "") + take);
    }, DRAFT_TICK_MS);
  }, [stopDraftDrain]);
```

Wire into the askQueryStream callbacks object (after onToken, ~line 471):

```tsx
            onDraft: (delta) => {
              if (runSeqRef.current !== seq) return;
              draftBufRef.current += delta;
              ensureDraftDrain();
              // Reuse the existing milestone SR announcements (same cadence
              // logic as onToken); first delta announces the revised copy.
            },
            onDraftReset: () => {
              if (runSeqRef.current !== seq) return;
              stopDraftDrain(true);
              setDraft(null);
            },
```

and pass `true` for the new liveDraft parameter of askQueryStream. Every
existing draft teardown gains the buffer flush: the STREAM_FALLBACK_STATUS
branch (line ~452) and the finalize-swap (line ~482) call
`stopDraftDrain(true)` beside their `setDraft(null)`.

- [ ] **Step 4: SR copy.** Replace the milestone announcement (line ~463):
  `"Drafting the answer \u2014 citations will be verified before it is shown."`
  with
  `"Drafting a provisional answer \u2014 the verified answer will follow."`
  (keep the `\u2014` escape form the file already uses; ASCII source only)
  (fires from whichever of onToken/onDraft lands first; keep one milestone
  counter shared by both).

- [ ] **Step 5: Withdrawal note.** turns.ts: `Turn` gains
  `draftWithdrawn: string | null;`, `StreamTrace` gains
  `draftWithdrawn?: string | null;`, `assistantTurn` maps
  `draftWithdrawn: trace.draftWithdrawn ?? null,` and `turnFromMessage` sets
  `draftWithdrawn: null` (history never carries it). page.tsx passes
  `draftWithdrawn: next.draft_withdrawn ?? null` in the assistantTurn trace.
  Turns.tsx, beside FallbackNote (~line 82):

```tsx
// The gate withdrew a provisional draft the analyst had already started
// reading (refusal, clarify, error, or dropped claims). Keyed ONLY on the
// server-declared signal - never on diffing draft text against the answer.
function WithdrawnNote({ turn }: { turn: Turn }) {
  if (!turn.draftWithdrawn) return null;
  const why =
    turn.draftWithdrawn === "partial"
      ? "some draft statements could not be verified and were dropped"
      : "it could not be verified against the cited guidance";
  return (
    <p className="msg__fallback code">
      {`The provisional draft was withdrawn \u2014 ${why}. The response below is the verified outcome.`}
    </p>
  );
}
```

Render `<WithdrawnNote turn={turn} />` directly after `<FallbackNote ... />`
in all four fixed slots (Turns.tsx lines ~218, 243, 315, 419). Revise
FallbackNote's copy (line ~86) to:
`"Connection dropped mid-draft \u2014 the answer was re-run over a fresh request and may differ from the draft."`

- [ ] **Step 6: Page tests.** In test/askPage.test.tsx add (mirroring the
  existing draft-swap test fixtures): a turn whose response carries
  `draft_withdrawn: "refused"` renders the withdrawal note text; a response
  without it renders no note; a `draft_reset` mid-stream clears the visible
  draft (drive the mocked askQueryStream callbacks directly).

- [ ] **Step 7: Frontend gate**

Run (in regwatch/frontend): `npm run test -- --run && npx tsc --noEmit && npm run lint`
Expected: all green; existing sse/askPage tests pass unchanged.

- [ ] **Step 8: Commit**

```bash
git add regwatch/frontend
git commit -m "feat(frontend): live draft channel - typewriter pacing, reset, withdrawal note"
```

---

### Task 9: Full gates, diff review, STOP

- [ ] **Step 1: Full backend suite**: `env -u VIRTUAL_ENV uv run pytest tests -q`
- [ ] **Step 2: Contract suite** (Postgres :5499 up, proxy built):
  `env -u VIRTUAL_ENV uv run pytest tests_contract -q`
- [ ] **Step 3: Static gates**: `uv run mypy src tests tests_contract && uv run ruff check . && uv run black --check .` (run `black .` first if needed - LAST edit wins)
- [ ] **Step 4: Frontend + Go**: `(cd regwatch/frontend && npm run test -- --run && npx tsc --noEmit && npm run lint)` and `(cd go && go build ./... && go test ./...)` (no Go changes expected; this proves it)
- [ ] **Step 5: Push, show the owner the full diffstat and a summary, and
  STOP.** The PR is opened from `feat/ask-sse-live-draft` after explicit
  owner go-ahead. Rollout (flag flips) is out of scope per spec section 12.

## Self-Review Notes

- Spec coverage: L0 amendment = Task 1; G1 = Task 2; L1 = Task 3; L2 +
  dual gate + frames = Task 4; withdrawal = Task 5; legacy re-scope = Task 6;
  S31 = Task 7; L3 = Task 8; gates = Task 9. Rollout (spec 12) is
  deliberately unplanned (ops, not code).
- Deviation from spec recorded: the sentinel hold lives in
  `_stream_structured` (pipeline), NOT the provider parser - EchoLLMProvider
  also streams (contract flavor live_draft_refusal), so a provider-local
  hold would leak the sentinel through echo. Spec section 5.1's intent (a
  refusal never paints) is preserved and pinned by S31b.
- Deviation recorded: `_complete_stream` is deleted (folded into the new
  stream()); spec called for "a new incremental path feeding stream()" and
  this is that, minus a dead wrapper.
- Type consistency: on_draft/on_draft_reset (Python), onDraft/onDraftReset
  (TS), draft_withdrawn (wire), draftWithdrawn (Turn) - each defined once in
  its Interfaces block and used with that exact name everywhere here.
