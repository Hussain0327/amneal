"""Stage timings reach the audit row on both control planes.

`query_log.latency_ms` records what a turn cost but never where it went.
Measured 2026-08-19, the language-model call is only ~1.0s of a 3.4-5.25s turn,
so most of the turn was unattributable from production data. These pin that the
per-stage block actually lands in `route_json` -- on the relay path (`ask`) and
on the Go native path (`compute_turn`), which is where production traffic
flows.
"""

from __future__ import annotations

import json
import time
from typing import Any

import config.settings as cs
import pytest
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.retrieve.resolver import ExternalDrugMatch, Resolution
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests.conftest import synth_turn_json
from tests.test_invariants import _meta, _seed_corpus

pytestmark = pytest.mark.invariants

# A provider stub sleeps this long so a stage BOUNDARY (which span owns the
# cost) can be asserted, not just which keys exist. The assertions carry 20ms
# of slack in both directions -- ">= 40" for the span that must own it and
# "< 40" for the spans that must not -- so a loaded machine cannot flip them.
_STUB_SLEEP_MS = 60.0
_OWNS_THE_SLEEP_MS = 40
# A schema-valid router reply that needs no corpus-policy machinery to compile:
# unknown scope carries neither a product nor a corpus hint.
_ROUTE_PAYLOAD = json.dumps(
    {
        "standalone_question": "How is ISM defined?",
        "mode": "lookup_clarify",
        "scope_hint": "unknown",
        "product_hint": None,
        "corpus_policy_hint": None,
    }
)
_TOP_LEVEL_ANSWER_STAGES = frozenset(
    {
        "session_open_ms",
        "route_ms",
        "retrieve_ms",
        "synthesis_ms",
        "gate_ms",
        "provenance_ms",
    }
)


def _only_route_json() -> dict[str, Any]:
    with session_scope() as s:
        rows = list(s.exec(select(QueryLog)))
        assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
        return dict(rows[0].route_json)


def _only_row() -> tuple[dict[str, Any], int | None]:
    """The single audit row's route_json and latency, read inside the session.

    Returns:
        The row's route_json and its ``latency_ms``. Detached ORM rows expire,
        so both are copied out while the scope is still open.
    """
    with session_scope() as s:
        rows = list(s.exec(select(QueryLog)))
        assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
        return dict(rows[0].route_json), rows[0].latency_ms


def _top_level(timings: dict[str, Any]) -> dict[str, int]:
    """The stage keys that feed ``measured_total_ms``.

    Args:
        timings: One row's timing block.

    Returns:
        Only the summable keys: dotted children and the aggregate itself are
        excluded, which is exactly the rule ``as_route_json`` applies.
    """
    return {
        key: value
        for key, value in timings.items()
        if key.endswith("_ms") and key != "measured_total_ms" and "." not in key
    }


def _sleepy_provider(text: str, *, model: str = "stub") -> Any:
    """A provider stub that costs real wall time before answering.

    Args:
        text: The completion body to return.
        model: The model name to report on the response.

    Returns:
        An object with the provider's ``complete`` surface.
    """

    class _Slow:
        name = model

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            time.sleep(_STUB_SLEEP_MS / 1000.0)
            return LLMResponse(text=text, model=model)

    return _Slow()


def test_ask_records_stage_timings_in_the_audit_row() -> None:
    """The relay path stamps the stages it owns, including the session write."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    qa_mod.ask("What study design is recommended?")

    timings = _only_route_json().get("timings")
    assert timings is not None, "route_json carries no timings block"
    # session_open is the stage only the relay shell can see, and one of the
    # suspects for the unattributed time. It spans the whole opening
    # transaction: the session upsert, the user-message write, and both
    # context reads (carry-over filters and conversation memory).
    assert timings["session_open_ms"] >= 0
    assert timings["retrieve_ms"] >= 0
    assert timings["measured_total_ms"] >= 0
    # The retrieval breakdown must survive a REAL retrieval, not just a unit
    # test: `retrieve.embed_and_scope` is measured on the calling thread while
    # the scoping round trips run in a worker (PR #242). If the collector were
    # invisible across that boundary, or the span were recorded inside the
    # worker instead, these children would silently vanish and the version-
    # scoping question could never be answered from production data.
    assert "retrieve.embed_and_scope_ms" in timings
    assert "retrieve.vector_search_ms" in timings
    # Every top-level stage an ANSWER turn owns, and NOTHING else. Exact set
    # equality on purpose: a dropped wrap point loses a key, a top-level stage
    # accidentally given a dotted name loses a key, and a child promoted to top
    # level adds one -- all three are the same bug (measured_total_ms stops
    # being a true sum) and all three fail here.
    #
    # guidance_ms is absent because the router only runs on a decline, and
    # session_context_ms is absent because on THIS path open_turn already
    # loaded both reads inside session_open (a second key would double-count).
    assert set(_top_level(timings)) == set(_TOP_LEVEL_ANSWER_STAGES)
    # The sum identity, which is what makes the remainder trustworthy. Each
    # stage floors independently while measured_total_ms floors the whole sum,
    # so the two legitimately differ by up to (stages - 1) milliseconds of
    # discarded fractions -- never more, and never in the other direction.
    drift = timings["measured_total_ms"] - sum(_top_level(timings).values())
    assert 0 <= drift < len(_top_level(timings)), timings


def test_compute_turn_records_stage_timings_for_the_go_path() -> None:
    """The native path carries the block too; Go persists route_json verbatim.

    Without this, instrumentation would cover only the fallback path and miss
    the traffic we are actually trying to explain.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    _outcome, audit, _patch = qa_mod.compute_turn(
        "What study design is recommended?",
        session_id="s-timing",
        turn_id="t-timing",
    )

    timings = audit.route_json.get("timings")
    assert timings is not None, "compute_turn produced no timings block"
    assert timings["retrieve_ms"] >= 0
    # Go performed the session write before calling, so that stage is NOT ours
    # to claim; recording it here would attribute Go's time to Python.
    assert "session_open_ms" not in timings
    # The reads this path DOES own are stamped separately, as one top-level
    # span holding one transaction for both of them.
    assert timings["session_context_ms"] >= 0
    # The stages that were an undifferentiated remainder before ITEM 5 must be
    # on the NATIVE path too -- that is where production traffic flows, so
    # instrumenting only the relay path would leave the real turns unexplained.
    assert timings["route_ms"] >= 0
    assert timings["gate_ms"] >= 0
    assert timings["provenance_ms"] >= 0


def test_measured_total_never_exceeds_the_turn_wall_clock() -> None:
    """Two overlapping top-level stages would sum past the turn itself.

    The single assertion that catches a double count without knowing which
    stage caused it: every top-level span is inside [t0, persist], so their sum
    can never exceed the interval latency_ms measures. Safe against int
    truncation in both directions -- each stage floors independently (only ever
    shrinking the sum) while latency_ms floors the whole interval -- and
    _persist_turn stamps latency AFTER the timings are attached, so the row's
    two numbers describe the same turn.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    qa_mod.ask("What study design is recommended?")

    route_json, latency_ms = _only_row()
    timings = route_json["timings"]
    assert latency_ms is not None
    assert timings["measured_total_ms"] <= latency_ms, timings


def test_guidance_call_is_timed_on_the_clarify_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The router round trip inside _decline was the largest untimed span.

    On prod clarify rows it costs 1.4-3.4s and, before ITEM 5, landed entirely
    in the unattributed remainder. The `route_ms` bound is the load-bearing
    half: it pins that nobody wrapped `_decline` itself, which would nest the
    guidance span inside the route span and count the same milliseconds twice.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    plan = '{"next_step":"narrow_source_topic","option_ids":[]}'
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _sleepy_provider(plan, model="router-stub")
    )

    result = qa_mod.ask("albuterol sulfate")

    assert result.status == "clarify"
    route_json = _only_route_json()
    # The branch really ran; without this the timing assertions below could
    # pass on a turn that never called the router at all.
    assert route_json["guidance"]["attempted"] is True
    timings = route_json["timings"]
    assert timings["guidance_ms"] >= _OWNS_THE_SLEEP_MS, timings
    assert timings["route_ms"] < _OWNS_THE_SLEEP_MS, timings


def test_gate_time_is_measured_outside_the_synthesis_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse, admit and render are NOT part of synthesis_ms, and must not be.

    synthesis_ms answers "how long did the provider take"; folding the gate
    into it would hide a slow gate behind a slow model. The stub's sleep is
    provider time, so it must land in synthesis_ms and nowhere else.
    """
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    completion = synth_turn_json(
        [
            (
                "A fasting bioequivalence study with 36 subjects is recommended.",
                [("PSG_020503", 3)],
            )
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _sleepy_provider(completion))

    result = qa_mod.ask("What study design is recommended?")

    assert result.status == "answer"
    timings = _only_route_json()["timings"]
    assert timings["synthesis_ms"] >= _OWNS_THE_SLEEP_MS, timings
    # The gate ran (it admitted the claim above) and cost pure CPU only.
    assert timings["gate_ms"] < _OWNS_THE_SLEEP_MS, timings
    # And the provider's time was counted ONCE: if a future edit widened the
    # synthesis span over the gate, or nested the gate inside it, the sleep
    # would appear in two top-level stages and push the total past this bound.
    assert timings["measured_total_ms"] < 2 * timings["synthesis_ms"], timings


def test_timings_never_fail_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken collector degrades to a missing block, never a failed answer.

    On the INV-6 answer path a lost audit row costs the user their answer, so a
    diagnostic must never be able to raise into it.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

    class _Exploding:
        def as_route_json(self) -> dict[str, Any]:
            raise RuntimeError("instrumentation is broken")

    audit = qa_mod.compute_turn(
        "What study design is recommended?",
        session_id="s-timing",
        turn_id="t-timing",
    )[1]
    qa_mod._attach_stage_timings(audit, _Exploding())  # type: ignore[arg-type]

    result = qa_mod.ask("What study design is recommended?")
    assert result.status in {"answer", "clarify", "refused", "summary", "error"}


class _RouteOnlySleeper:
    """Provider that costs wall time on the ROUTER call and nowhere else.

    The router and the clarify-guidance call share one provider, so a stub that
    slept on every completion could not tell `route.model` apart from
    `guidance`. Keying on the route prompt's marker is what makes the span
    boundary assertable.
    """

    name = "route-sleeper"

    def __init__(self, route_payload: str) -> None:
        self.route_payload = route_payload
        self.route_calls = 0

    def complete(self, messages: list[Any], **_kwargs: Any) -> LLMResponse:
        is_route = any(
            "[REGWATCH_ROUTE_V2]" in message.content or "[REGWATCH_ROUTE_V1]" in message.content
            for message in messages
        )
        if not is_route:
            return LLMResponse(text="{}", model=self.name)
        self.route_calls += 1
        time.sleep(_STUB_SLEEP_MS / 1000.0)
        return LLMResponse(text=_ROUTE_PAYLOAD, model=self.name)


def _no_product_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forces the unresolved-product path, where both route children live.

    Args:
        monkeypatch: The test's monkeypatch fixture.
    """
    monkeypatch.setattr(qa_mod, "resolve_product", lambda _q: Resolution(status="none"))
    monkeypatch.setattr(qa_mod, "suggest_products", lambda _q: [])


def test_route_model_child_isolates_the_router_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGWATCH_ROUTE_CALL=shadow is live in prod, so this span runs every turn.

    It is the one number the shadow instrumentation exists to expose, and it is
    invisible to the rest of the suite because route_call_mode defaults to
    "off" everywhere else. Dotted on purpose: the router call already sits
    inside `route`, so promoting it to a top-level name would count the same
    milliseconds twice in measured_total_ms.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    provider = _RouteOnlySleeper(_ROUTE_PAYLOAD)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    _no_product_route(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "lookup_external_drug",
        lambda _q: ExternalDrugMatch(corpus_products=[], known_absent=False),
    )
    monkeypatch.setenv("REGWATCH_ROUTE_CALL", "shadow")
    cs.get_settings.cache_clear()

    _outcome, audit, _patch = qa_mod.compute_turn(
        "Across the inhalation guidances, how is ISM defined?",
        session_id="s-route-model",
        turn_id="t-route-model",
    )

    # The branch really ran; the timing assertions below would otherwise pass
    # vacuously on a turn that never called the router.
    assert provider.route_calls == 1
    timings = audit.route_json["timings"]
    assert "route.model_ms" in timings, timings
    assert timings["route.model_ms"] >= _OWNS_THE_SLEEP_MS, timings
    # The parent contains the child: the router call is route work, not a
    # sibling stage.
    assert timings["route_ms"] >= timings["route.model_ms"], timings
    # Dotted, therefore never summed. Both halves matter: a rename to a
    # non-dotted key would add a top-level stage AND inflate the total.
    assert "route.model_ms" not in _top_level(timings)
    drift = timings["measured_total_ms"] - sum(_top_level(timings).values())
    assert 0 <= drift < len(_top_level(timings)), timings


def test_route_external_drug_child_isolates_the_fda_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clarify path's local FdaDocument lookup gets its own dotted child.

    Same contract as route.model, on the branch that actually reaches it: no
    resolved product, no fuzzy suggestion. Route mode stays "off" here, so the
    span is measured without the router call sharing the parent.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _sleepy_provider("{}", model="guidance-stub")
    )
    _no_product_route(monkeypatch)
    lookups = 0

    def _slow_lookup(_question: str) -> ExternalDrugMatch:
        nonlocal lookups
        lookups += 1
        time.sleep(_STUB_SLEEP_MS / 1000.0)
        return ExternalDrugMatch(corpus_products=[], known_absent=False)

    monkeypatch.setattr(qa_mod, "lookup_external_drug", _slow_lookup)

    _outcome, audit, _patch = qa_mod.compute_turn(
        "Tell me about romidepsin",
        session_id="s-route-drug",
        turn_id="t-route-drug",
    )

    assert lookups == 1
    timings = audit.route_json["timings"]
    assert "route.external_drug_ms" in timings, timings
    assert timings["route.external_drug_ms"] >= _OWNS_THE_SLEEP_MS, timings
    assert timings["route_ms"] >= timings["route.external_drug_ms"], timings
    assert "route.external_drug_ms" not in _top_level(timings)
    drift = timings["measured_total_ms"] - sum(_top_level(timings).values())
    assert 0 <= drift < len(_top_level(timings)), timings
