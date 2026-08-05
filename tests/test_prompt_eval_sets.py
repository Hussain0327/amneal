from __future__ import annotations

import json

import pytest

from regwatch.eval import prompt_eval
from regwatch.eval.prompt_eval import validate_prompt_sets
from regwatch.generate import turn_gate as tg
from regwatch.generate.guidance import GUIDANCE_SCHEMA_MESSAGE, QUERY_GUIDANCE_PROMPT
from regwatch.generate.llm import LLMResponse
from regwatch.generate.prompt_identity import identify_prompt
from regwatch.generate.prompts import (
    GROUNDED_QA_PROMPT,
    GROUNDED_QA_SYSTEM,
    GROUNDED_QA_USER,
    QUERY_GUIDANCE_SYSTEM,
    QUERY_GUIDANCE_USER,
    generation_prompt_manifest,
)
from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE
from regwatch.retrieve.retriever import RetrievedPassage
from tests.conftest import synth_turn_json


def test_prompt_eval_sets_are_nonempty_unique_and_schema_valid() -> None:
    manifest = validate_prompt_sets()

    assert set(manifest) == {"qa", "guidance", "extraction", "changes"}
    assert all(item["count"] >= 3 for item in manifest.values())
    assert manifest["guidance"]["count"] >= 7
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


def test_guidance_prompt_set_covers_non_answer_routes() -> None:
    rows = prompt_eval._load_jsonl(prompt_eval._SET_FILES["guidance"]).rows

    assert {
        "no_product",
        "low_top_score",
        "vague_input",
        "multi_form",
        "ambiguous_product",
        "scope_warning",
        "meta",
    }.issubset({row["reason"] for row in rows})


def test_generation_prompt_manifest_is_versioned_and_hashed() -> None:
    manifest = generation_prompt_manifest()

    # Per-prompt versions: the grounded QA prompt moved to the structured turn
    # contract while the extraction/change prompts did not, so one shared
    # literal would hide the next divergence.
    assert {prompt_id: item["version"] for prompt_id, item in manifest.items()} == {
        "regwatch.grounded_qa": "4",
        "regwatch.query_guidance": "1",
        "regwatch.be_extraction": "2",
        "regwatch.change_summary": "2",
    }
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


def test_guidance_prompt_fingerprint_includes_the_output_schema() -> None:
    with_schema = identify_prompt(
        "regwatch.query_guidance",
        "1",
        QUERY_GUIDANCE_SYSTEM,
        QUERY_GUIDANCE_USER,
        GUIDANCE_SCHEMA_MESSAGE.content,
    )
    without_schema = identify_prompt(
        "regwatch.query_guidance", "1", QUERY_GUIDANCE_SYSTEM, QUERY_GUIDANCE_USER
    )

    assert with_schema == QUERY_GUIDANCE_PROMPT
    assert QUERY_GUIDANCE_PROMPT.sha256 != without_schema.sha256


def test_grounded_qa_prompt_fingerprint_includes_the_turn_schema() -> None:
    """The schema pins the answer SHAPE, so it must move the fingerprint.

    Claim.text's length cap and the claims-per-turn cap live in
    TURN_SCHEMA_MESSAGE, not in the prose templates. While it sat outside the
    hash, editing either one changed what the model was told and left
    route_json["prompt"] byte-identical -- so a before/after cohort could not be
    separated in the audit trail at all.
    """
    with_schema = identify_prompt(
        "regwatch.grounded_qa",
        "4",
        GROUNDED_QA_SYSTEM,
        GROUNDED_QA_USER,
        TURN_SCHEMA_MESSAGE.content,
    )
    without_schema = identify_prompt(
        "regwatch.grounded_qa", "4", GROUNDED_QA_SYSTEM, GROUNDED_QA_USER
    )

    assert with_schema == GROUNDED_QA_PROMPT
    assert GROUNDED_QA_PROMPT.sha256 != without_schema.sha256


class _GuidanceProvider:
    name = "guidance-stub"

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[tuple[list[object], dict[str, object]]] = []

    def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
        self.calls.append((messages, kwargs))
        return LLMResponse(text=self.raw, model=self.name)


def _guidance_eval_row() -> dict[str, object]:
    return {
        "id": "unit_guidance",
        "question": "Which metformin product did you mean?",
        "status": "clarify",
        "reason": "ambiguous_product",
        "product": None,
        "clarify": [
            {
                "label": "Metformin hydrochloride",
                "query": "What study is recommended for metformin hydrochloride?",
                "filters": {"normalized_name": "metformin hydrochloride"},
            },
            {
                "label": "Sitagliptin and metformin",
                "query": "What study is recommended for sitagliptin and metformin?",
                "filters": {"normalized_name": "sitagliptin and metformin"},
            },
        ],
        "related": [],
        "expected_next_steps": ["choose_product"],
        "expected_first_option_ids": ["clarify:0"],
    }


def test_guidance_eval_uses_router_and_records_only_bounded_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _GuidanceProvider(
        json.dumps({"next_step": "choose_product", "option_ids": ["clarify:0"]})
    )
    roles: list[str] = []

    def _provider_for_role(*, role: str) -> _GuidanceProvider:
        roles.append(role)
        return provider

    monkeypatch.setattr(prompt_eval, "get_llm_provider", _provider_for_role)

    details = prompt_eval._run_guidance([_guidance_eval_row()])

    assert roles == ["router"]
    assert len(provider.calls) == 1
    _messages, kwargs = provider.calls[0]
    assert kwargs == {"temperature": 0.0, "max_tokens": 600, "response_format": "json"}
    assert details == [
        {
            "id": "unit_guidance",
            "passed": True,
            "model": "guidance-stub",
            "next_step": "choose_product",
            "option_ids": ["clarify:0"],
            "option_match": True,
        }
    ]
    assert not ({"question", "response", "text", "prose"} & details[0].keys())


def test_guidance_eval_scores_an_allowlisted_but_unexpected_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _guidance_eval_row()
    row.update(
        reason="low_top_score",
        status="refused",
        expected_next_steps=["narrow_source_topic"],
    )
    provider = _GuidanceProvider('{"next_step":"choose_dosage_form","option_ids":[]}')
    monkeypatch.setattr(prompt_eval, "get_llm_provider", lambda *, role: provider)

    details = prompt_eval._run_guidance([row])

    assert details[0]["passed"] is False
    assert details[0]["next_step"] == "choose_dosage_form"
    assert "failure" not in details[0]


def test_guidance_eval_scores_option_priority_not_only_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _GuidanceProvider('{"next_step":"choose_product","option_ids":["clarify:1"]}')
    monkeypatch.setattr(prompt_eval, "get_llm_provider", lambda *, role: provider)

    details = prompt_eval._run_guidance([_guidance_eval_row()])

    assert details[0]["next_step"] == "choose_product"
    assert details[0]["option_match"] is False
    assert details[0]["passed"] is False


@pytest.mark.parametrize(
    ("raw", "failure"),
    [
        (
            '{"next_step":"view_capabilities","option_ids":[]}',
            "disallowed_guidance_step",
        ),
        (
            '{"next_step":"choose_product","option_ids":["clarify:99"]}',
            "unknown_guidance_option",
        ),
    ],
)
def test_guidance_eval_rejects_non_allowlisted_model_selections(
    monkeypatch: pytest.MonkeyPatch, raw: str, failure: str
) -> None:
    provider = _GuidanceProvider(raw)
    monkeypatch.setattr(prompt_eval, "get_llm_provider", lambda *, role: provider)

    details = prompt_eval._run_guidance([_guidance_eval_row()])

    assert details == [
        {
            "id": "unit_guidance",
            "passed": False,
            "model": "guidance-stub",
            "failure": failure,
        }
    ]


# ---------- the INV-1 properties the deleted segment splitter used to cover ----------

_QUESTION = "What study is recommended, and what are the waiver conditions?"
_PARTIAL_LINE = "Evidence not found in the supplied passages for: waiver conditions."


def _passage(short_name: str, page: int) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=f"{short_name}-{page}",
        text="Fasting single-dose two-way crossover bioequivalence study.",
        score=1.0,
        doc_id=1,
        version_id=10,
        page=page,
        section_path=None,
        normalized_name="synthetic",
        source_url="",
        short_name=short_name,
        metadata={},
    )


_PASSAGES = [_passage("PSG_100001", 2), _passage("PSG_100002", 3)]


def _admit(raw: str) -> tg.AdmittedTurn:
    out = tg.admit_turn(raw, passages=_PASSAGES, question=_QUESTION)
    assert isinstance(out, tg.AdmittedTurn)
    return out


def test_partial_evidence_disclosure_is_narrow_and_renderer_owned() -> None:
    """The evidence-not-found line is written by the renderer from validated
    labels, so it can no longer be a laundering channel for a regulatory claim
    ("waiver is approved") dressed as retrieval state."""
    valid = _admit(
        synth_turn_json(
            [("A fasting study is recommended", [("PSG_100002", 3)])],
            unsupported=("waiver conditions",),
        )
    )
    assert valid.unsupported == ("waiver conditions",)
    body = tg.render_answer(valid).split("\n\nSources:")[0]
    assert body.strip().endswith(_PARTIAL_LINE)

    laundering = _admit(
        synth_turn_json(
            [("A fasting study is recommended", [("PSG_100002", 3)])],
            unsupported=("waiver is approved. A fed study is required",),
        )
    )
    assert laundering.unsupported == ()
    assert "Evidence not found" not in tg.render_answer(laundering)


def test_uncited_sentence_cannot_be_laundered_by_another_cited_sentence() -> None:
    """Two sentences in ONE claim slot behind one valid cite is the exact shape
    that would make this design weaker than the gate it replaces."""
    turn = _admit(
        synth_turn_json(
            [
                (
                    "A fasting study is recommended. A fed study is also required.",
                    [("PSG_100001", 2)],
                )
            ]
        )
    )

    assert [c.reason for c in turn.dropped] == [tg.DROP_MULTI_SENTENCE]
    assert "fed study" not in tg.render_answer(turn)


def test_fabricated_cite_drops_its_claim_whole() -> None:
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting study is recommended", [("PSG_100001", 2)]),
                ("A fed study is also advised", [("PSG_999999", 9)]),
            ]
        )
    )

    assert [c.reason for c in turn.dropped] == [tg.DROP_UNKNOWN_CITATION]
    assert "PSG_999999" not in tg.render_answer(turn)
