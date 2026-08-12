"""The Checkpoint 3 arithmetic, provable without a production window.

Checkpoint 3 is a promotion decision, so the numbers behind it have to be
trustworthy before anyone reads them off a live shadow window. These tests pin
the two ways a summary could mislead an owner: reporting "no failures" when it
means "no data", and reporting a clean corpus record when a corpus scope was
authorized under a reason the #163 contract does not allow.
"""

from __future__ import annotations

from typing import Any

from regwatch.eval.route_shadow_report import PARSE_FAILURE_CEILING, summarize
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from regwatch.store.queries import load_route_shadow_rows


def _row(**overrides: Any) -> dict[str, Any]:
    """A successful, agreeing, product-scoped shadow row."""
    row: dict[str, Any] = {
        "attempted": True,
        "configured_mode": "shadow",
        "effective_mode": "shadow",
        "outcome": "success",
        "latency_ms": 900,
        "mode": "lookup",
        "scope_hint": "product",
        "current_mode": "lookup",
        "current_scope": "product",
        "compile_status": "success",
        "compiled_scope": {"kind": "product", "reason": "resolved_product"},
        "agrees_with_mode": True,
        "agrees_with_scope": True,
        "cost_usd": 0.0001,
    }
    row.update(overrides)
    return row


def test_no_data_does_not_read_as_no_failures() -> None:
    """The single most dangerous way this report could lie.

    A promotion decision made on an empty window must not see failure_rate 0.0
    and read it as "the route call is reliable". Empty means None, everywhere.
    """
    report = summarize([])

    assert report.attempted == 0
    assert report.failure_rate is None
    assert report.parse_failure_rate is None
    assert report.mode_agreement is None
    assert report.scope_agreement is None
    assert report.meets_parse_ceiling is False, "an empty window cannot clear an acceptance floor"


def test_route_off_turns_do_not_dilute_the_rates() -> None:
    """A window spanning the flag being switched on stays honest.

    Turns recorded before REGWATCH_ROUTE_CALL was set carry no attempt. Counting
    them in the denominator would make a bad failure rate look acceptable.
    """
    report = summarize([_row(), _row(outcome="invalid"), {"attempted": False}, {}])

    assert report.total_rows == 4
    assert report.attempted == 2
    assert report.failure_rate == 0.5
    assert report.parse_failure_rate == 0.5


def test_parse_failures_are_measured_against_the_two_percent_ceiling() -> None:
    """Checkpoint 3 accepts parse failure below 2% of attempts."""
    clean = summarize([_row() for _ in range(100)])
    assert clean.parse_failure_rate == 0.0
    assert clean.meets_parse_ceiling is True

    one_bad = summarize([_row() for _ in range(99)] + [_row(outcome="invalid")])
    assert one_bad.parse_failure_rate == 0.01
    assert one_bad.meets_parse_ceiling is True

    three_bad = summarize([_row() for _ in range(97)] + [_row(outcome="invalid")] * 3)
    assert three_bad.parse_failure_rate == 0.03
    assert three_bad.meets_parse_ceiling is False
    assert PARSE_FAILURE_CEILING == 0.02


def test_the_confusion_matrices_are_joint_not_marginal() -> None:
    """Checkpoint 3 asks for a JOINT mode/scope matrix.

    Marginal counts would hide the case that matters: the route agreeing on mode
    while disagreeing on scope is exactly the disagreement that would misroute a
    turn once the route has authority.
    """
    report = summarize(
        [
            _row(),
            _row(mode="converse", current_mode="lookup", agrees_with_mode=False),
            _row(
                compiled_scope={"kind": "clarify", "reason": "product_not_resolved"},
                current_scope="product",
                agrees_with_scope=False,
            ),
        ]
    )

    assert report.mode_matrix[("lookup", "lookup")] == 2
    assert report.mode_matrix[("converse", "lookup")] == 1
    assert report.scope_matrix[("product", "product")] == 2
    assert report.scope_matrix[("clarify", "product")] == 1
    assert report.mode_agreement == 2 / 3
    assert report.scope_agreement == 2 / 3


def test_latency_percentiles_name_a_real_observed_call() -> None:
    """Nearest-rank, so p95 is a latency some request actually experienced."""
    report = summarize([_row(latency_ms=ms) for ms in range(1, 101)])

    assert report.latency.count == 100
    assert report.latency.p50 == 50
    assert report.latency.p95 == 95
    assert report.latency.maximum == 100


def test_a_failed_call_still_contributes_its_latency() -> None:
    """Added latency is what the USER paid, whether or not the call succeeded."""
    report = summarize([_row(), _row(outcome="provider_error", latency_ms=5000)])

    assert report.latency.count == 2
    assert report.latency.maximum == 5000


def test_a_sound_corpus_authorization_is_not_flagged() -> None:
    report = summarize(
        [
            _row(
                compiled_scope={
                    "kind": "corpus",
                    "reason": "explicit_corpus",
                    "corpus_policy": "inhalation_psg",
                    "corpus_documents": [{"doc_id": 1}, {"doc_id": 2}],
                },
                current_scope="corpus",
            )
        ]
    )

    assert report.corpus_authorizations == 1
    assert report.unsafe_corpus == ()
    assert report.has_unsafe_corpus is False


def test_every_way_a_corpus_authorization_violates_the_contract() -> None:
    """ "Zero unsafe corpus authorizations" is an acceptance line, so it is checked.

    Each row below is one clause of issue #163's safety contract: corpus intent
    positively identified, an allowlisted policy, and a bounded NON-EMPTY set of
    current versions.
    """
    report = summarize(
        [
            _row(
                compiled_scope={
                    "kind": "corpus",
                    "reason": "scope_unknown",
                    "corpus_policy": "inhalation_psg",
                    "corpus_documents": [{"doc_id": 1}],
                }
            ),
            _row(
                compiled_scope={
                    "kind": "corpus",
                    "reason": "explicit_corpus",
                    "corpus_policy": None,
                    "corpus_documents": [{"doc_id": 1}],
                }
            ),
            _row(
                compiled_scope={
                    "kind": "corpus",
                    "reason": "explicit_corpus",
                    "corpus_policy": "inhalation_psg",
                    "corpus_documents": [],
                }
            ),
            _row(
                compiled_scope={
                    "kind": "corpus",
                    "reason": "explicit_corpus",
                    "corpus_policy": "inhalation_psg",
                    "corpus_documents": [{"doc_id": n} for n in range(513)],
                }
            ),
        ]
    )

    assert report.corpus_authorizations == 4
    assert report.has_unsafe_corpus is True
    violations = [u.violation for u in report.unsafe_corpus]
    assert "reason='scope_unknown'" in violations[0]
    assert "no allowlisted policy" in violations[1]
    assert "empty document set" in violations[2]
    assert "above the 512 cap" in violations[3]


def test_scope_reasons_are_counted_for_rule_tuning() -> None:
    """The corpus-intent rule gets tuned from these counts, not from intuition."""
    report = summarize(
        [
            _row(compiled_scope={"kind": "clarify", "reason": "corpus_intent_not_explicit"}),
            _row(compiled_scope={"kind": "clarify", "reason": "corpus_intent_not_explicit"}),
            _row(compiled_scope={"kind": "product", "reason": "resolved_product"}),
        ]
    )

    assert report.scope_reasons == {"corpus_intent_not_explicit": 2, "resolved_product": 1}


def test_an_accidentally_early_live_secret_is_visible() -> None:
    """`live` executes as shadow today, so the only harm is a silent misread.

    Surfacing the count means an operator who set the reserved value sees it in
    the evidence bundle instead of believing the route had authority.
    """
    report = summarize([_row(), _row(configured_mode="live")])

    assert report.configured_live == 1


def test_the_loader_lifts_route_call_out_of_route_json_and_skips_turns_without_one() -> None:
    """The read half, against a real audit table.

    The arithmetic above is fixture-driven, so this is the only test that would
    catch the loader reading the wrong JSON path -- which would make every
    Checkpoint 3 number silently zero.
    """
    with session_scope() as session:
        session.add_all(
            [
                QueryLog(
                    mode="qa",
                    query_text="What studies does the albuterol PSG recommend?",
                    answer_text="...",
                    model_name="route-model",
                    route_json={"route_call": _row(latency_ms=812), "reason": "retrieval"},
                ),
                QueryLog(
                    mode="qa",
                    query_text="A turn recorded before the flag was set",
                    answer_text="...",
                    model_name="synth-model",
                    route_json={"reason": "retrieval"},
                ),
            ]
        )

    rows = load_route_shadow_rows()
    report = summarize(rows)

    assert len(rows) == 1, "turns with no route_call block must not reach the report"
    assert report.attempted == 1
    assert report.latency.p95 == 812
    assert report.mode_matrix == {("lookup", "lookup"): 1}


def test_an_uncompiled_row_contributes_no_matrix_entry() -> None:
    """A provider error has no proposal to compare, and must not invent one."""
    report = summarize(
        [_row(outcome="provider_error", compile_status="not_attempted", compiled_scope=None)]
    )

    assert report.mode_matrix == {}
    assert report.scope_matrix == {}
    assert report.compile_statuses == {"not_attempted": 1}
    assert report.failure_rate == 1.0
