"""Citation grammar: simple + compound parsing, stripping, filtering."""

from __future__ import annotations

from regwatch.common.citations import (
    filter_citations,
    has_citation,
    iter_psg_citations,
    strip_all_citations,
)


def test_parses_simple_citation() -> None:
    assert list(iter_psg_citations("A claim [PSG_020503, p.3].")) == [("PSG_020503", 3)]


def test_parses_compound_citation() -> None:
    # The model emits several sources in one bracket; the old regex dropped them.
    text = "Both PSGs agree [PSG_020503, p.4; PSG_021730, p.4]."
    assert list(iter_psg_citations(text)) == [("PSG_020503", 4), ("PSG_021730", 4)]


def test_compound_with_three_sources() -> None:
    text = "[PSG_020503, p.2; PSG_020911, p.1; PSG_207921, p.5]"
    assert list(iter_psg_citations(text)) == [
        ("PSG_020503", 2),
        ("PSG_020911", 1),
        ("PSG_207921", 5),
    ]


def test_non_citation_brackets_ignored() -> None:
    assert list(iter_psg_citations("see [appendix] and [note 4]")) == []
    assert has_citation("see [appendix]") is False
    assert has_citation("here [PSG_020503, p.3]") is True


def test_strip_all_citations_keeps_prose() -> None:
    text = "Use a fasting study [PSG_020503, p.4; PSG_021730, p.4] per guidance."
    assert strip_all_citations(text).strip() == "Use a fasting study  per guidance.".strip()


def test_filter_drops_disallowed_pairs_in_compound() -> None:
    text = "Per guidance [PSG_020503, p.4; PSG_FAKE, p.9]."
    out = filter_citations(text, allowed={("PSG_020503", 4)})
    assert "PSG_FAKE" not in out
    assert "[PSG_020503, p.4]" in out


def test_filter_removes_fully_disallowed_bracket() -> None:
    text = "Claim [PSG_FAKE, p.1]."
    assert filter_citations(text, allowed=set()) == "Claim ."
