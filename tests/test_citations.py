"""Citation grammar: simple + compound parsing, stripping, filtering."""

from __future__ import annotations

from regwatch.common.citations import (
    filter_citations,
    has_citation,
    is_structured_token,
    iter_psg_citations,
    ob_token,
    obexcl_token,
    obpat_token,
    parse_structured_token,
    spl_token,
    strip_all_citations,
    validate_structured_citations,
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
    assert list(iter_psg_citations("see [Table 1, p.3]")) == []
    assert has_citation("see [appendix]") is False
    assert has_citation("here [PSG_020503, p.3]") is True


def test_strip_all_citations_keeps_prose() -> None:
    text = "Use a fasting study [PSG_020503, p.4; PSG_021730, p.4] per guidance."
    assert strip_all_citations(text).strip() == "Use a fasting study  per guidance.".strip()


def test_filter_drops_disallowed_pairs_in_compound() -> None:
    text = "Per guidance [PSG_020503, p.4; PSG_999999, p.9]."
    out = filter_citations(text, allowed={("PSG_020503", 4)})
    assert "PSG_999999" not in out
    assert "[PSG_020503, p.4]" in out


def test_filter_removes_fully_disallowed_bracket() -> None:
    text = "Claim [PSG_999999, p.1]."
    assert filter_citations(text, allowed=set()) == "Claim ."


# --------------------------- structured token grammar (INV-8) ---------------------------
def test_structured_token_builders_roundtrip() -> None:
    assert spl_token("abc-123", "34067-9") == "SPL_abc-123#34067-9"
    assert ob_token("020503", "001") == "OB_020503/001"
    assert obpat_token("RE37410") == "OBPAT_RE37410"
    assert obexcl_token("NCE") == "OBEXCL_NCE"
    for token in (
        spl_token("abc-123", "34067-9"),
        ob_token("020503", "001"),
        obpat_token("RE37410"),
        obexcl_token("NCE"),
    ):
        assert is_structured_token(token)
        assert parse_structured_token(token) is not None


def test_structured_kinds() -> None:
    assert parse_structured_token("SPL_abc-123#42228-7").kind == "spl"  # type: ignore[union-attr]
    assert parse_structured_token("OB_020503/1").kind == "ob"  # type: ignore[union-attr]
    assert parse_structured_token("OBPAT_8048874").kind == "obpat"  # type: ignore[union-attr]
    assert parse_structured_token("OBEXCL_M-123").kind == "obexcl"  # type: ignore[union-attr]


def test_page_locator_is_not_a_structured_token() -> None:
    # The two citation worlds never collide.
    assert parse_structured_token("PSG_020503, p.4") is None
    assert not is_structured_token("PSG_020503, p.4")


def test_validate_structured_drops_unbacked() -> None:
    known = {"OB_020503/001", "SPL_abc#34067-9"}
    valid, invalid = validate_structured_citations(
        ["OB_020503/001", "OB_020503/999", "SPL_abc#34067-9", "not-a-token"],
        known,
    )
    assert valid == ["OB_020503/001", "SPL_abc#34067-9"]
    assert "OB_020503/999" in invalid
    assert "not-a-token" in invalid


def test_validate_structured_dedupes() -> None:
    known = {"OB_020503/001"}
    valid, invalid = validate_structured_citations(["OB_020503/001", "OB_020503/001"], known)
    assert valid == ["OB_020503/001"]
    assert invalid == []
