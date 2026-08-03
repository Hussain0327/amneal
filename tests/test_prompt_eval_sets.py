from __future__ import annotations

from regwatch.eval.prompt_eval import validate_prompt_sets
from regwatch.generate.grounded_qa import _uncited_answer_segments
from regwatch.generate.prompts import generation_prompt_manifest


def test_prompt_eval_sets_are_nonempty_unique_and_schema_valid() -> None:
    manifest = validate_prompt_sets()

    assert set(manifest) == {"qa", "extraction", "changes"}
    assert all(item["count"] >= 3 for item in manifest.values())
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


def test_generation_prompt_manifest_is_versioned_and_hashed() -> None:
    manifest = generation_prompt_manifest()

    assert set(manifest) == {
        "regwatch.grounded_qa",
        "regwatch.be_extraction",
        "regwatch.change_summary",
    }
    assert all(item["version"] == "2" for item in manifest.values())
    assert all(len(item["sha256"]) == 64 for item in manifest.values())


def test_partial_evidence_disclosure_is_narrow_and_must_be_last() -> None:
    valid = (
        "A fasting study is recommended [PSG_100002, p.3].\n"
        "Evidence not found in the supplied passages for: waiver conditions."
    )
    assert _uncited_answer_segments(valid) == []

    laundering = (
        "Evidence not found in the supplied passages for: waiver is approved. "
        "A fed study is required."
    )
    assert _uncited_answer_segments(laundering)


def test_uncited_sentence_cannot_be_laundered_by_another_cited_sentence() -> None:
    answer = "A fasting study is recommended [PSG_100001, p.2]. " "A fed study is also required."

    assert _uncited_answer_segments(answer) == ["A fed study is also required."]
