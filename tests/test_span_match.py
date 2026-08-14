"""Quote location: normalization plus a bounded fuzzy fallback (spec S34).

Covers the shared module and both call sites that validate a model-supplied
quote against its claimed page. Non-ASCII fixtures are written as escape
sequences so this source stays ASCII.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import regwatch.process.change_detector as cd
from regwatch.generate.llm import LLMResponse
from regwatch.process import extractor as ext
from regwatch.process.span_match import normalize_for_match, quote_on_page

PAGE_SEP = "\n\f\n"


class _StubLLM:
    """Returns one canned JSON payload, standing in for the extractor LLM."""

    name = "stub"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
        return LLMResponse(text=json.dumps(self.payload), model="stub")


class _RecordingLog:
    """Captures structured log events so observability can be asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def field(self, event: str, key: str) -> Any:
        return next(fields[key] for name, fields in self.events if name == event)


def _document(*pages: str) -> str:
    return PAGE_SEP.join(pages)


def _stub_factory(payload: dict[str, Any]) -> object:
    class _Provider:
        def complete(self, messages: object, **kwargs: object) -> LLMResponse:
            return LLMResponse(text=json.dumps(payload), model="stub")

    def _factory(*_a: object, **_kw: object) -> _Provider:
        return _Provider()

    return _factory


# --- Stage 1: normalization ------------------------------------------------


def test_ligature_matches_its_ascii_pair() -> None:
    page = "Dissolution testing is speci\ufb01c to the dosage form."

    match = quote_on_page("specific to the dosage form", page)

    assert (match.found, match.exact, match.distance) == (True, True, 0)


def test_soft_hyphen_inside_a_word_is_ignored() -> None:
    page = "The dis\u00adsolution method is USP Apparatus 2."

    match = quote_on_page("dissolution method", page)

    assert (match.found, match.exact) == (True, True)


def test_hyphen_at_a_line_break_rejoins_the_word() -> None:
    page = "The dis-\nsolution method follows USP."

    match = quote_on_page("dissolution method", page)

    assert (match.found, match.exact) == (True, True)


def test_soft_hyphen_at_a_line_break_rejoins_the_word() -> None:
    page = "The dis\u00ad\nsolution method follows USP."

    match = quote_on_page("dissolution method", page)

    assert (match.found, match.exact) == (True, True)


def test_a_numeric_range_wrapped_at_an_en_dash_still_matches() -> None:
    # An en dash at a line end is a wrapping range, not a hyphenated word:
    # dehyphenation must not fuse "80-125" into "80125". The remaining
    # difference is one space, recovered by the budget.
    page = "The acceptance interval is 80\u2013\n125 percent of the reference."

    match = quote_on_page("acceptance interval is 80-125 percent", page)

    # The alignment window can be off by a character at either end, so the
    # distance is the one real space plus at most one boundary edit.
    assert (match.found, match.exact) == (True, False)
    assert 1 <= match.distance <= 2


def test_curly_quotes_and_apostrophes_match_straight_ones() -> None:
    page = "The agency\u2019s \u201cfasting\u201d study is recommended."

    match = quote_on_page('The agency\'s "fasting" study', page)

    assert (match.found, match.exact) == (True, True)


def test_em_dash_matches_a_plain_hyphen() -> None:
    page = "The acceptance interval is 80\u2014125 percent."

    match = quote_on_page("80-125 percent", page)

    assert (match.found, match.exact) == (True, True)


def test_doubled_spaces_and_line_breaks_collapse() -> None:
    page = "Dissolution: USP  Apparatus  2\n  at   50 RPM."

    match = quote_on_page("USP Apparatus 2 at 50 RPM", page)

    assert (match.found, match.exact) == (True, True)


def test_case_differences_do_not_matter() -> None:
    page = "II. TWO STUDIES ARE RECOMMENDED."

    match = quote_on_page("Two studies are recommended", page)

    assert (match.found, match.exact) == (True, True)


def test_normalization_is_idempotent() -> None:
    raw = "Two  dis-\nsolution \u201cstudies\u201d\u00ad are speci\ufb01c."

    once = normalize_for_match(raw)

    assert normalize_for_match(once) == once


# --- Stage 2: bounded fuzzy fallback ---------------------------------------


def test_single_extraction_typo_is_accepted_and_flagged_as_fuzzy() -> None:
    page = "II. Two studies are recomended: a fasting study and a fed study."

    match = quote_on_page("Two studies are recommended: a fasting study and a fed study", page)

    assert (match.found, match.exact, match.distance) == (True, False, 1)


def test_hard_hyphen_lost_to_dehyphenation_is_recovered_by_the_budget() -> None:
    # "single-\ndose" joins to "singledose", so the quote no longer matches
    # verbatim; one edit is inside the budget.
    page = "Perform a single-\ndose fasting study is recommended here."

    match = quote_on_page("single-dose fasting study is recommended", page)

    assert (match.found, match.exact, match.distance) == (True, False, 1)


def test_a_fabricated_quote_is_rejected() -> None:
    page = "I. Real content. The acceptance interval is 80 to 125 percent."

    match = quote_on_page("This sentence is fabricated and not in the page", page)

    assert (match.found, match.distance) == (False, -1)


def test_a_near_miss_beyond_the_budget_is_rejected() -> None:
    # "fasting" -> "fed study in healthy adults" is far past 5% of 42 chars.
    page = "II. A single-dose fed study in healthy adults is recommended."

    match = quote_on_page("a single-dose fasting study is recommended", page)

    assert match.found is False


def test_the_budget_will_not_move_a_digit() -> None:
    # One edit apart, but the changed character is the volume. A fuzzy hit
    # that rewrites a number is a different span, not an artifact.
    page = "Dissolution: USP Apparatus 2 at 50 RPM in 500 mL of pH 6.8 buffer."

    match = quote_on_page("USP Apparatus 2 at 50 RPM in 900 mL of pH 6.8 buffer", page)

    assert match.found is False


def test_a_short_quote_gets_no_fuzzy_leniency() -> None:
    page = "A fedd study is required."

    match = quote_on_page("fed study", page)

    assert match.found is False


def test_a_word_swap_within_the_char_budget_is_rejected() -> None:
    # "fasted" vs "fed" is only three character edits -- inside a long
    # quote's budget -- but a different study condition, not an artifact.
    page = (
        "II. A single-dose study under fed conditions with the highest "
        "strength is recommended for this drug product."
    )

    match = quote_on_page(
        "A single-dose study under fasted conditions with the highest "
        "strength is recommended for this drug product",
        page,
    )

    assert match.found is False


def test_a_spelled_out_magnitude_change_is_rejected() -> None:
    # "milligrams" -> "micrograms" is three character edits and a 1000x
    # dose change; the digit guard cannot see it, the word guard must.
    page = "A single dose of ten micrograms administered orally is recommended."

    match = quote_on_page(
        "A single dose of ten milligrams administered orally is recommended",
        page,
    )

    assert match.found is False


def test_a_dropped_negation_is_rejected() -> None:
    # Losing "no" is three character edits, within a long quote's budget,
    # and inverts the requirement.
    page = "There is no established acceptance criterion for this method of testing."

    match = quote_on_page(
        "There is established acceptance criterion for this method of testing",
        page,
    )

    assert match.found is False


def test_a_negation_word_never_absorbs_an_edit() -> None:
    # "not" -> "now" is a single edit -- typo-class by distance, a meaning
    # flip in fact.
    page = "The waiver is now required for this strength and product."

    match = quote_on_page("The waiver is not required for this strength and product", page)

    assert match.found is False


def test_scattered_single_typos_are_accepted_within_the_cap() -> None:
    page = (
        "II. Two disolution studies are recomended: a fasting study and "
        "a fed study in healthy adults."
    )

    match = quote_on_page(
        "Two dissolution studies are recommended: a fasting study and "
        "a fed study in healthy adults",
        page,
    )

    # Two real typos plus at most one alignment-window boundary edit.
    assert (match.found, match.exact) == (True, False)
    assert 2 <= match.distance <= 3


def test_the_total_budget_is_hard_capped() -> None:
    # Five one-char typos: each alone is typo-class, but 5% of this
    # quote's length would allow six edits, enough to stop reading as the
    # same span. The hard cap rejects it.
    page = (
        "II. Two disolution studies are recomended: a fastin study and "
        "a fedd study in helthy adults under this guidance document."
    )

    match = quote_on_page(
        "Two dissolution studies are recommended: a fasting study and "
        "a fed study in healthy adults under this guidance document",
        page,
    )

    assert match.found is False


@pytest.mark.parametrize(
    ("quote", "page"),
    [("", "Some page text."), ("Some quote.", "   \n  "), ("", "")],
)
def test_empty_input_is_never_a_match(quote: str, page: str) -> None:
    assert quote_on_page(quote, page).found is False


def test_plain_ascii_quotes_match_exactly_as_before() -> None:
    page = "II. Recommendations\nTwo studies are recommended: fasting and fed."

    present = quote_on_page("Two studies are recommended: fasting and fed", page)
    absent = quote_on_page("A waiver of in vivo testing is granted", page)

    assert (present.found, present.exact, present.distance) == (True, True, 0)
    assert absent.found is False


# --- Call site: BE-requirements extractor ----------------------------------


def test_extractor_keeps_a_field_whose_page_has_extraction_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        "I. Introduction",
        "II. The dis\u00adsolution method is speci\ufb01c to the "
        "\u201cimmediate-release\u201d form.",
    ]
    payload = {
        "fields": {
            "dissolution": {
                "value": "Immediate-release dissolution method",
                "citation": {
                    "page": 2,
                    "quote": "The dissolution method is specific to the "
                    '"immediate-release" form.',
                },
            }
        }
    }
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: _StubLLM(payload))

    result = ext.extract_be(pages)

    assert result.fields["dissolution"] == "Immediate-release dissolution method"
    assert result.citations["dissolution"]["page"] == 2


def test_extractor_logs_the_fuzzy_stage_only_when_it_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = "Two studies are recommended: a fasting study and a fed study"
    payload = {
        "fields": {
            "study_type": {
                "value": "Fasting and fed",
                "citation": {"page": 1, "quote": quote},
            }
        }
    }
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: _StubLLM(payload))

    verbatim_log = _RecordingLog()
    monkeypatch.setattr(ext, "log", verbatim_log)
    verbatim = ext.extract_be([f"II. {quote}."])

    typo_log = _RecordingLog()
    monkeypatch.setattr(ext, "log", typo_log)
    typo = ext.extract_be(["II. Two studies are recomended: a fasting study and a fed study."])

    assert verbatim.fields["study_type"] == "Fasting and fed"
    assert "be_extraction_quote_fuzzy_match" not in verbatim_log.names()
    assert typo.fields["study_type"] == "Fasting and fed"
    assert "be_extraction_quote_fuzzy_match" in typo_log.names()
    assert typo_log.field("be_extraction_quote_fuzzy_match", "distance") == 1


def test_extractor_still_drops_an_unlocatable_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "fields": {
            "additional_notes": {
                "value": "BE acceptance 80-125%",
                "citation": {
                    "page": 1,
                    "quote": "This sentence is fabricated and not in the page",
                },
            }
        }
    }
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: _StubLLM(payload))
    recorder = _RecordingLog()
    monkeypatch.setattr(ext, "log", recorder)

    result = ext.extract_be(["I. The acceptance interval is 80 to 125 percent."])

    assert result.fields["additional_notes"] is None
    assert "be_extraction_quote_not_found" in recorder.names()


# --- Call site: Watch change summaries -------------------------------------


def test_change_summary_accepts_evidence_with_extraction_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _document("Cover", "The dissolution method is USP Apparatus 1.")
    current = _document("Cover", "The dis\u00adsolution method is USP Apparatus 2.")
    payload = {
        "claims": [
            {
                "statement": "The apparatus changed",
                "previous": {
                    "page": 2,
                    "quote": "The dissolution method is USP Apparatus 1.",
                },
                "current": {
                    "page": 2,
                    "quote": "The dissolution method is USP Apparatus 2.",
                },
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))

    out = cd.summarize_change(previous, current, current_page_count=2)

    assert out == "The apparatus changed [previous p.2] [p.2]."


def test_change_summary_rejoins_a_quote_split_across_a_line_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _document("Cover", "A fasting study alone is required here.")
    current = _document("Cover", "Two dis-\nsolution profiles are required here.")
    payload = {
        "claims": [
            {
                "statement": "Dissolution profiles were added",
                "previous": None,
                "current": {
                    "page": 2,
                    "quote": "Two dissolution profiles are required here.",
                },
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))

    out = cd.summarize_change(previous, current, current_page_count=2)

    assert out == "Dissolution profiles were added [p.2]."


def test_change_summary_logs_the_fuzzy_stage_when_it_carries_the_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _document("Cover", "A fasting study alone is required here.")
    current = _document("Cover", "Two dissolution profiles are recomended here.")
    payload = {
        "claims": [
            {
                "statement": "Dissolution profiles were added",
                "previous": None,
                "current": {
                    "page": 2,
                    "quote": "Two dissolution profiles are recommended here.",
                },
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))
    recorder = _RecordingLog()
    monkeypatch.setattr(cd, "log", recorder)

    out = cd.summarize_change(previous, current, current_page_count=2)

    assert out == "Dissolution profiles were added [p.2]."
    assert "change_summary_quote_fuzzy_match" in recorder.names()
    assert recorder.field("change_summary_quote_fuzzy_match", "distance") == 1


def test_change_summary_still_rejects_unchanged_cover_page_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _document("FDA Product-Specific Guidance", "Old waiver language")
    current = _document("FDA Product-Specific Guidance", "New waiver language")
    payload = {
        "claims": [
            {
                "statement": "The waiver changed",
                "previous": None,
                "current": {"page": 1, "quote": "FDA Product-Specific Guidance"},
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))

    assert cd.summarize_change(previous, current, current_page_count=2) == ""


def test_change_summary_still_rejects_a_quote_cited_to_the_wrong_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _document("Cover", "Old dissolution method")
    current = _document("Cover", "New dissolution method")
    payload = {
        "claims": [
            {
                "statement": "The dissolution method changed",
                "previous": None,
                "current": {"page": 1, "quote": "New dissolution method"},
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))

    assert cd.summarize_change(previous, current, current_page_count=2) == ""


def test_change_summary_rejects_a_quote_of_the_unchanged_twin_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The current page carries an unchanged line and a changed line one edit
    # apart. Quoting the UNCHANGED one is verbatim on the page but only
    # fuzzy among the changed lines -- the budget must not blur the
    # changed/unchanged distinction the diff established.
    previous = _document("Cover", "The method is described in section IV.")
    current = _document(
        "Cover",
        "The method is described in section IV.\n" "The method as described in section IV.",
    )
    payload = {
        "claims": [
            {
                "statement": "The method reference changed",
                "previous": None,
                "current": {
                    "page": 2,
                    "quote": "The method is described in section IV.",
                },
            }
        ]
    }
    monkeypatch.setattr(cd, "get_llm_provider", _stub_factory(payload))

    assert cd.summarize_change(previous, current, current_page_count=2) == ""
