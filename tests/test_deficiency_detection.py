"""Unit tests for the deterministic parts of the fault-detection layer.

Ported from upstream tests/unit/test_detection.py (DefPredict). Import rewritten onto the
vendored modules; logic and assertions otherwise unchanged. None of these tests exercise
an LLM call: TestChallenge/TestCrossReferenceAET/TestElChecklists all hit pure functions
(_apply_verdict, _sections_for, the oracle/checklist functions) directly, never
challenge.py's structured_call use site.
"""

from __future__ import annotations

from typing import Any

from regwatch.deficiency.detection.catalog import normalize_type
from regwatch.deficiency.detection.checklists import run_checklists
from regwatch.deficiency.detection.oracles import (
    parse_limit,
    parse_number,
    result_vs_limit,
    satisfies,
)
from regwatch.deficiency.detection.render import render_section
from regwatch.deficiency.detection.verify import verify_and_tier
from regwatch.deficiency.schemas.documents import CTDSection
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, Tier
from regwatch.deficiency.schemas.flaws import Severity, SimilarDeficiency


class TestCatalog:
    def test_normalizes_messy_types(self):
        assert normalize_type("Method/Valdn") == "method-validation"
        assert normalize_type("container/Closure") == "container-closure"
        assert normalize_type("Stability") == "stability"

    def test_unknown_type_is_empty(self):
        assert normalize_type("something-nobody-mapped") == ""


class TestNumericOracle:
    def test_parse_number_and_limit(self):
        assert parse_number("0.17%") == 0.17
        assert parse_number("< 0.05 %") == 0.05
        assert parse_number("ND") is None
        assert parse_limit("NMT 0.25%") == ("max", None, 0.25)
        assert parse_limit("NLT 2.5") == ("min", 2.5, None)
        assert parse_limit("0.9 - 1.5") == ("range", 0.9, 1.5)

    def test_satisfies(self):
        assert satisfies(0.17, ("max", None, 0.25)) is True
        assert satisfies(0.30, ("max", None, 0.25)) is False

    def test_result_vs_limit_flags_only_the_violation(self):
        doc = {
            "pages": [
                {
                    "blocks": [],
                    "tables": [
                        {
                            "kind": "grid",
                            "page": 9,
                            "title": "Impurities",
                            "headers": ["Compound", "Result", "Limit"],
                            "rows": [
                                ["17a-estradiol", "0.30", "NMT 0.25%"],  # violation
                                ["d9-estradiol", "0.05", "NMT 0.1%"],  # ok
                            ],
                        }
                    ],
                }
            ]
        }
        faults = result_vs_limit(doc)
        assert len(faults) == 1
        assert faults[0].evidence_class == EvidenceClass.CODE_VERIFIED
        assert faults[0].tier == Tier.VERIFIED
        assert "17a-estradiol" in faults[0].evidence

    def test_value_vs_inline_limit(self):
        from regwatch.deficiency.detection.oracles import value_vs_inline_limit

        doc = {
            "pages": [
                {
                    "blocks": [],
                    "tables": [
                        {
                            "kind": "grid",
                            "page": 24,
                            "title": "Table 18",
                            "headers": ["Parameter", "In-house"],
                            "rows": [
                                ["% RSD", "1.8\n(NMT 6.0)"],  # ok
                                ["Tailing factor", "3.0\n(0.9 -1.5)"],  # 3.0 > 1.5 -> violation
                            ],
                        }
                    ],
                }
            ]
        }
        faults = value_vs_inline_limit(doc)
        assert len(faults) == 1
        assert "Tailing factor" in faults[0].title
        assert faults[0].evidence_class == EvidenceClass.CODE_VERIFIED


class TestChecklist:
    def test_absent_validation_params_are_flagged(self):
        doc = {
            "pages": [
                {
                    "blocks": [
                        {
                            "role": "paragraph",
                            "text": "This report covers specificity and linearity only.",
                        }
                    ],
                    "tables": [],
                }
            ]
        }
        faults = run_checklists(doc, CTDSection.S_4_3_VALIDATION)
        titles = " ".join(f.title.lower() for f in faults)
        assert "loq" in titles or "limit of quantitation" in titles  # missing
        assert "specificity" not in titles  # present, not flagged
        assert all(f.evidence_class == EvidenceClass.CHECKLIST for f in faults)

    def test_non_validation_doc_skips_checklist(self):
        doc: dict[str, Any] = {"pages": [{"blocks": [], "tables": []}]}
        assert run_checklists(doc, CTDSection.S_7_STABILITY) == []


class TestVerifyAndTier:
    def _doc_with(self, text: str) -> dict:
        return {"pages": [{"blocks": [{"text": text}], "tables": []}]}

    def test_precedent_promotes_to_corroborated_and_anchors_evidence(self):
        doc = self._doc_with("The tailing factor was 3.4 which is out of range.")
        f = Fault(
            title="Tailing factor out of range",
            evidence="tailing factor was 3.4",
            precedents=[SimilarDeficiency(product_name="X", deficiency_text="tailing issue")],
        )
        out = verify_and_tier([f], doc)
        assert out[0].tier == Tier.CORROBORATED
        assert out[0].evidence_class == EvidenceClass.QUOTE_ANCHORED

    def test_no_precedent_stays_advisory_and_novel(self):
        doc = self._doc_with("some unrelated text")
        f = Fault(title="Unprecedented issue", evidence="not in the document")
        out = verify_and_tier([f], doc)
        assert out[0].tier == Tier.ADVISORY
        assert out[0].novel is True
        assert out[0].evidence_class == EvidenceClass.MODEL_JUDGMENT

    def test_dedup_keeps_higher_authority(self):
        doc = self._doc_with("x")
        oracle = Fault(
            title="Result out of specification",
            evidence_class=EvidenceClass.CODE_VERIFIED,
            tier=Tier.VERIFIED,
            severity=Severity.HIGH,
        )
        agent = Fault(title="Result out of specification", evidence="")
        out = verify_and_tier([agent, oracle], doc)
        assert len(out) == 1
        assert out[0].evidence_class == EvidenceClass.CODE_VERIFIED


class TestRender:
    def test_grid_table_renders_as_markdown(self):
        section = {
            "heading": "1.4.1 Specificity",
            "page_start": 10,
            "page_end": 10,
            "blocks": [],
            "tables": [
                {
                    "kind": "grid",
                    "title": "Table 2",
                    "page": 10,
                    "headers": ["Compound", "RRT"],
                    "rows": [["Estradiol", "1.00"]],
                }
            ],
        }
        md = render_section(section)
        assert "| Compound | RRT |" in md
        assert "Estradiol" in md
        assert "Table 2" in md


class TestChallenge:
    def test_grounded_refutation_lowers_confidence(self):
        from regwatch.deficiency.detection.challenge import ChallengeVerdict, _apply_verdict
        from regwatch.deficiency.detection.verify import _doc_corpus

        doc = {
            "pages": [
                {
                    "blocks": [{"text": "The absorptivity factor for Estradiol is 1.02."}],
                    "tables": [],
                }
            ]
        }
        f = Fault(title="Missing absorptivity factor for Estradiol", confidence=0.4)
        verdict = ChallengeVerdict(
            refuted=True, counter_evidence="absorptivity factor for Estradiol is 1.02"
        )
        _apply_verdict(f, verdict, _doc_corpus(doc))
        assert f.confidence < 0.4
        assert f.challenge_note != ""

    def test_ungrounded_refutation_does_not_lower(self):
        from regwatch.deficiency.detection.challenge import ChallengeVerdict, _apply_verdict
        from regwatch.deficiency.detection.verify import _doc_corpus

        doc = {"pages": [{"blocks": [{"text": "unrelated content"}], "tables": []}]}
        f = Fault(title="Some finding", confidence=0.4)
        # claims refuted, but the counter-evidence is not in the document -> not grounded -> survives
        verdict = ChallengeVerdict(
            refuted=True, counter_evidence="a passage that is not in the document"
        )
        _apply_verdict(f, verdict, _doc_corpus(doc))
        assert f.confidence >= 0.4
        assert f.challenge_note == ""

    def test_context_resolves_table_ref(self):
        from regwatch.deficiency.detection.challenge import _sections_for

        sections = [
            {
                "heading": "1.4.5 Absorptivity Factor",
                "text": "",
                "tables": [{"title": "Table 11. Absorptivity Factor"}],
            },
            {
                "heading": "1.4.6 Precision",
                "text": "",
                "tables": [{"title": "Table 12. System Precision"}],
            },
        ]
        picked = _sections_for(Fault(title="x", table_ref="Table 11"), sections)
        assert len(picked) == 1
        assert "Absorptivity" in picked[0]["heading"]


class TestCrossReferenceAET:
    def test_conflicting_aet_values_flagged(self):
        from regwatch.deficiency.detection.oracles import cross_reference_consistency

        doc = {
            "pages": [
                {
                    "page_number": 6,
                    "blocks": [{"text": "the drug product AET level of 8.72 ug/g"}],
                    "tables": [],
                },
                {
                    "page_number": 7,
                    "blocks": [{"text": "leachables above AET level of 22.727 ug/g were reported"}],
                    "tables": [],
                },
            ]
        }
        faults = cross_reference_consistency(doc)
        assert len(faults) == 1
        assert faults[0].evidence_class == EvidenceClass.CODE_VERIFIED
        assert faults[0].tier == Tier.VERIFIED
        assert "8.72" in faults[0].evidence and "22.727" in faults[0].evidence

    def test_consistent_aet_and_unit_split_not_flagged(self):
        from regwatch.deficiency.detection.oracles import cross_reference_consistency

        doc = {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {"text": "AET = 8.72 ug/g"},
                        {"text": "AET of 8.72 ug/g (0.75 ug/patch)"},
                    ],
                    "tables": [],
                },
            ]
        }
        # 8.72 ug/g is consistent; 0.75 ug/patch is a different unit -- neither is a conflict
        assert cross_reference_consistency(doc) == []


class TestElChecklists:
    def test_usp88_coverage_flags_uncovered_component(self):
        from regwatch.deficiency.detection.checklists import usp88_component_coverage

        doc = {
            "pages": [
                {
                    "page_number": 17,
                    "blocks": [],
                    "tables": [
                        {
                            "kind": "grid",
                            "page": 17,
                            "title": "Table 7. USP Compendia Tests",
                            "headers": ["Sr", "Component", "USP Tests", "Tests"],
                            "rows": [
                                ["1", "Pouch (Printed)", "USP <661.2>", "Appearance"],
                                ["", "", "USP <87> - Biological Reactivity", "Direct Contact"],
                                [
                                    "3",
                                    "Release Liner",
                                    "USP <87> - Biological Reactivity",
                                    "Direct Contact",
                                ],
                                ["", "", "USP <88> - Biological Reactivity", "Implantation"],
                                [
                                    "4",
                                    "Backing Film",
                                    "USP <87> - Biological Reactivity",
                                    "Direct Contact",
                                ],
                                ["", "", "USP <88> - Biological Reactivity", "Implantation"],
                            ],
                        }
                    ],
                }
            ]
        }
        faults = usp88_component_coverage(doc)
        assert len(faults) == 1
        assert "pouch" in faults[0].title.lower()
        assert faults[0].evidence_class == EvidenceClass.CHECKLIST

    def test_leachable_commitment_flagged_when_absent(self):
        from regwatch.deficiency.detection.checklists import leachable_commitment

        doc = {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {
                            "text": (
                                "The risk of leachables is low. The product is safe for its "
                                "intended use."
                            )
                        }
                    ],
                    "tables": [],
                }
            ]
        }
        faults = leachable_commitment(doc)
        assert len(faults) == 1
        assert faults[0].category.value == "commitment_missing"

    def test_leachable_commitment_ok_when_present(self):
        from regwatch.deficiency.detection.checklists import leachable_commitment

        doc = {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {
                            "text": (
                                "Amneal commits to continue leachable monitoring through the "
                                "proposed shelf life."
                            )
                        }
                    ],
                    "tables": [],
                }
            ]
        }
        assert leachable_commitment(doc) == []
