"""INV-1 provenance: diff-summary page citations beyond the document's page
count must be STRIPPED from the returned summary, not merely logged.

These tests exercise summarize_change's validation branch directly by stubbing
the LLM provider so the "model output" is deterministic. A reverted fix (i.e.
log-only, returning the unmodified summary) makes the first two tests fail.
"""

from __future__ import annotations

import pytest

import regwatch.process.change_detector as cd
from regwatch.generate.llm import LLMResponse


def _stub_llm(text: str) -> object:
    """Return a get_llm_provider replacement whose complete() yields `text`."""

    class _Provider:
        def complete(self, *_a: object, **_kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    def _factory(*_a: object, **_kw: object) -> _Provider:
        return _Provider()

    return _factory


def test_out_of_range_cite_sentence_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Page 9 does not exist in a 3-page doc -> that sentence must be removed.
    summary = "Dissolution method changed [p.2]. New fasting study added [p.9]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=3)

    assert "[p.9]" not in out
    assert "fasting study" not in out  # the whole unsupported sentence is gone
    # The in-range citation and its sentence survive verbatim.
    assert "[p.2]" in out
    assert "Dissolution method changed" in out


def test_only_bad_cite_yields_empty_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = "Subjects increased to 36 [p.50]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=4)

    assert "[p.50]" not in out
    assert out == ""


def test_all_in_range_cites_pass_through_unmodified(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = "BE interval narrowed [p.1]. Waiver removed [p.3]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=3)

    assert out == summary


def test_page_equal_to_count_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: [p.N] where N == page_count is in range (pages are 1-indexed).
    summary = "Last-page note [p.3]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=3)

    assert out == summary


def test_page_zero_cite_sentence_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pages are 1-indexed, so [p.0] is as ungrounded as a page beyond the
    # count; a one-sided (> page_count) check silently accepts it.
    summary = "Dissolution acceptance criteria changed [p.0]. Waiver kept [p.2]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=3)

    assert "[p.0]" not in out
    assert "Dissolution acceptance criteria" not in out
    # The in-range citation and its sentence survive verbatim.
    assert "Waiver kept [p.2]." in out


def test_only_page_zero_cite_yields_empty_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = "Study design revised [p.0]."
    monkeypatch.setattr(cd, "get_llm_provider", _stub_llm(summary))

    out = cd.summarize_change("old", "new", current_page_count=5)

    assert out == ""


def test_initial_version_marker_skips_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    # No prior text -> no LLM call, no cite validation; marker returned as-is.
    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("LLM must not be called for the initial version")

    monkeypatch.setattr(cd, "get_llm_provider", _boom)

    out = cd.summarize_change(None, "Some current PSG body text.", current_page_count=2)

    assert out.startswith("Initial version ingested.")
