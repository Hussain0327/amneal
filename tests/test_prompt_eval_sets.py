from __future__ import annotations

from regwatch.eval.prompt_eval import validate_prompt_sets
from regwatch.generate import turn_gate as tg
from regwatch.generate.prompts import generation_prompt_manifest
from regwatch.retrieve.retriever import RetrievedPassage
from tests.conftest import synth_turn_json


def test_prompt_eval_sets_are_nonempty_unique_and_schema_valid() -> None:
    manifest = validate_prompt_sets()

    assert set(manifest) == {"qa", "extraction", "changes"}
    assert all(item["count"] >= 3 for item in manifest.values())
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


def test_generation_prompt_manifest_is_versioned_and_hashed() -> None:
    manifest = generation_prompt_manifest()

    # Per-prompt versions: the grounded QA prompt moved to the structured turn
    # contract while the extraction/change prompts did not, so one shared
    # literal would hide the next divergence.
    assert {prompt_id: item["version"] for prompt_id, item in manifest.items()} == {
        "regwatch.grounded_qa": "3",
        "regwatch.be_extraction": "2",
        "regwatch.change_summary": "2",
    }
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


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
