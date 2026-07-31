"""CTD-section detection (salvaged from the removed detection layer).

Rule-based classification of a document into its CTD section, used to key KB retrieval
(by section / dosage form) and to tell a specialist what kind of document it is reading.
"""

from __future__ import annotations

import re

from regwatch.deficiency.schemas.documents import CTDSection

_CTD_PATTERNS: list[tuple[re.Pattern, CTDSection]] = [
    (re.compile(r"3\.2\.\s*S\.4\.1\b", re.IGNORECASE), CTDSection.S_4_1_SPECIFICATION),
    (re.compile(r"3\.2\.\s*S\.4\.2\b", re.IGNORECASE), CTDSection.S_4_2_ANALYTICAL_PROCEDURES),
    (re.compile(r"3\.2\.\s*S\.4\.3\b", re.IGNORECASE), CTDSection.S_4_3_VALIDATION),
    (re.compile(r"3\.2\.\s*S\.4\.4\b", re.IGNORECASE), CTDSection.S_4_4_BATCH_ANALYSES),
    (re.compile(r"3\.2\.\s*S\.7\b", re.IGNORECASE), CTDSection.S_7_STABILITY),
    (re.compile(r"3\.2\.\s*P\.4\.1\b", re.IGNORECASE), CTDSection.P_4_1_SPECIFICATION),
    (re.compile(r"3\.2\.\s*P\.4\.3\b", re.IGNORECASE), CTDSection.P_4_3_VALIDATION),
    (re.compile(r"3\.2\.\s*P\.5\b", re.IGNORECASE), CTDSection.P_5_REFERENCE_STANDARDS),
    (re.compile(r"3\.2\.\s*P\.7\b", re.IGNORECASE), CTDSection.P_7_STABILITY),
    (re.compile(r"3\.2\.\s*P\.3\b", re.IGNORECASE), CTDSection.P_3_MANUFACTURE),
    (re.compile(r"3\.2\.\s*P\.2\b", re.IGNORECASE), CTDSection.P_2_DEVELOPMENT),
]


def detect_ctd_section(text: str) -> CTDSection:
    for pattern, section in _CTD_PATTERNS:
        if pattern.search(text):
            return section
    return CTDSection.UNKNOWN


def describe_document(section: CTDSection) -> str:
    """Name the document in words the model can reason about."""
    if section is CTDSection.UNKNOWN:
        return "an unidentified CTD section"
    if section.value.startswith("3.2.S"):
        subject = "drug substance"
    elif section.value.startswith("3.2.P"):
        subject = "drug product"
    else:
        subject = "regional or appendix material"
    return f"CTD {section.value} ({subject})"
