"""Unit tests for Pydantic schema validation.

Ported from upstream tests/unit/test_schemas.py (DefPredict). Import rewritten onto the
vendored schemas; logic and assertions otherwise unchanged.

Deviation beyond the import-rewrite map: upstream's TestAgentEvent class (schemas.events
-- AgentEvent/EventType) is dropped. schemas/events.py was never vendored: its only
consumer was agents/event_bus.py, replaced by the regwatch.deficiency.events log-only
seam (see src/regwatch/deficiency/schemas/__init__.py's leading comment).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from regwatch.deficiency.schemas.documents import ChunkGroup, CTDSection, ParsedSection
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, FaultReport, Tier
from regwatch.deficiency.schemas.flaws import FlawCategory, Severity, SimilarDeficiency


class TestCTDSection:
    def test_known_section_exists(self):
        assert CTDSection.S_4_1_SPECIFICATION.value == "3.2.S.4.1"


class TestFaultSchemas:
    def test_fault_defaults(self):
        f = Fault(title="Missing residual solvent spec")
        assert f.severity == Severity.MEDIUM
        assert f.tier == Tier.ADVISORY
        assert f.evidence_class == EvidenceClass.MODEL_JUDGMENT
        assert f.category == FlawCategory.GENERAL_CMC
        assert f.precedents == []
        assert f.novel is False

    def test_fault_report_empty(self):
        r = FaultReport(job_id="j", faults=[], faults_found=False)
        assert r.faults == []
        assert r.faults_found is False
        # The clean-document shape detection.pipeline emits, and -- through
        # runner.py's model_dump(mode="json") -- the exact payload persisted as
        # DeficiencyRun.report_json and handed back by the API. Pin the serialized
        # key set: renaming or dropping a field rewrites the stored document and
        # every reader of it without failing anywhere else.
        dumped = r.model_dump(mode="json")
        assert dumped["faults"] == []
        assert dumped["faults_found"] is False
        assert set(dumped) == {
            "job_id",
            "faults",
            "faults_found",
            "domains_checked",
            "parse_failures",
            "analysis_seconds",
        }

    def test_confidence_is_bounded(self):
        with pytest.raises(ValidationError):
            Fault(title="x", confidence=1.5)

    def test_fault_roundtrips(self):
        f = Fault(
            title="Result out of specification",
            category=FlawCategory.SPEC_MISMATCH,
            tier=Tier.VERIFIED,
            evidence_class=EvidenceClass.CODE_VERIFIED,
            precedents=[SimilarDeficiency(product_name="X", deficiency_text="y")],
        )
        dumped = f.model_dump()
        assert Fault(**dumped).tier == Tier.VERIFIED
        assert Fault(**dumped).precedents[0].product_name == "X"


class TestParsedSection:
    def test_section_creation(self):
        s = ParsedSection(
            # runtime str->StrEnum coercion is the point of this test
            section_id="3.2.S.4.1",  # type: ignore[arg-type]
            heading="Specifications",
            text="Test content",
        )
        assert s.tables == []

    def test_chunk_group(self):
        sections = [
            ParsedSection(
                section_id=CTDSection.S_1_GENERAL,
                heading="Sec 1",
                text="a" * 100,
            ),
        ]
        g = ChunkGroup(group_id="g1", sections=sections)
        assert len(g.sections) == 1
