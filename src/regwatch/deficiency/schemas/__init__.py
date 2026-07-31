# AgentEvent (upstream schemas.events) is not vendored: its only consumer was
# agents/event_bus.py, replaced by the regwatch.deficiency.events log-only seam.
from regwatch.deficiency.schemas.documents import (
    ChunkGroup,
    CTDSection,
    ExtractedTable,
    ParsedSection,
)
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, FaultReport, Tier
from regwatch.deficiency.schemas.flaws import FlawCategory, Severity, SimilarDeficiency
from regwatch.deficiency.schemas.llm import ParseFailed

__all__ = [
    "CTDSection",
    "ChunkGroup",
    "EvidenceClass",
    "ExtractedTable",
    "Fault",
    "FaultReport",
    "FlawCategory",
    "ParseFailed",
    "ParsedSection",
    "Severity",
    "SimilarDeficiency",
    "Tier",
]
