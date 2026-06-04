"""BE-requirements extractor: enforces per-field citations (INV-1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from regwatch.generate.llm import LLMResponse
from regwatch.process import extractor as ext


class _StubLLM:
    name = "stub"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.last_messages: list[object] = []

    def complete(
        self,
        messages: list[object],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        return LLMResponse(text=json.dumps(self.payload), model="stub")


def test_extractor_drops_fields_without_valid_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        "I. Introduction\nThis guidance describes bioequivalence study recommendations.",
        "II. Recommendations\n"
        "A. Two studies are recommended: a fasting study and a fed study.\n"
        "B. Dissolution: USP Apparatus 2 at 50 RPM in 900 mL of pH 6.8 buffer.",
    ]
    # A valid citation, an invalid one (page out of range), and a value-without-citation.
    payload = {
        "fields": {
            "study_type": {
                "value": "Single-dose fasting and fed",
                "citation": {
                    "page": 2,
                    "quote": "Two studies are recommended: a fasting study and a fed study",
                },
            },
            "dissolution": {
                "value": "USP Apparatus 2 at 50 RPM",
                "citation": {"page": 99, "quote": "USP Apparatus 2 at 50 RPM"},  # bad page
            },
            "study_design": {
                "value": "Crossover",
                "citation": None,  # missing citation
            },
        }
    }
    stub = _StubLLM(payload)
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: stub)

    result = ext.extract_be(pages)
    # study_type kept (valid citation)
    assert result.fields["study_type"] == "Single-dose fasting and fed"
    assert result.citations["study_type"]["page"] == 2
    # dissolution dropped (invalid page)
    assert result.fields["dissolution"] is None
    assert "dissolution" not in result.citations
    # study_design dropped (no citation)
    assert result.fields["study_design"] is None


def test_extractor_drops_fabricated_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = ["I. Real content. The acceptance interval is 80 to 125 percent."]
    payload = {
        "fields": {
            "additional_notes": {
                "value": "BE acceptance 80-125%",
                "citation": {"page": 1, "quote": "This sentence is fabricated and not in the page"},
            }
        }
    }
    stub = _StubLLM(payload)
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: stub)
    result = ext.extract_be(pages)
    assert result.fields["additional_notes"] is None


def test_extractor_invalid_json_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadLLM:
        name = "bad"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text="not json at all", model="bad")

    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: _BadLLM())
    result = ext.extract_be(["page one"])
    assert all(v is None for v in result.fields.values())
    assert result.citations == {}
