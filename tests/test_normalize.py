"""Active-ingredient normalization (the matcher's bedrock)."""

from __future__ import annotations

import pytest

from regwatch.common.text_normalize import (
    canonical_name,
    is_combo,
    split_ingredients,
    stripped_name,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Albuterol Sulfate", "albuterol sulfate"),
        ("ALBUTEROL SULFATE", "albuterol sulfate"),
        ("albuterol sulfate (anhydrous)", "albuterol sulfate"),
        ("  Albuterol   Sulfate  ", "albuterol sulfate"),
    ],
)
def test_canonical_single(raw: str, expected: str) -> None:
    assert canonical_name(raw) == expected


def test_canonical_combo_sort_invariant() -> None:
    a = canonical_name("Hydrocodone Bitartrate; Acetaminophen")
    b = canonical_name("Acetaminophen and Hydrocodone Bitartrate")
    c = canonical_name("Acetaminophen, Hydrocodone Bitartrate")
    assert a == b == c
    assert is_combo(a)


def test_split_ingredients() -> None:
    assert split_ingredients("a; b") == ["a", "b"]
    assert split_ingredients("a and b") == ["a", "b"]
    assert split_ingredients("a, b") == ["a", "b"]
    assert split_ingredients("solo") == ["solo"]


def test_stripped_name_drops_salts() -> None:
    assert stripped_name("Albuterol Sulfate") == "albuterol"
    assert stripped_name("Methylphenidate Hydrochloride") == "methylphenidate"
    assert stripped_name("Levothyroxine Sodium") == "levothyroxine"


def test_stripped_name_collapses_pure_electrolytes_to_empty() -> None:
    """Mono-salt electrolytes are all salt tokens, so they strip to '' -- the
    empty-key guard then refuses to cross-match distinct ones as the same drug
    (e.g. potassium vs sodium vs calcium chloride)."""
    assert stripped_name("Potassium Chloride") == ""
    assert stripped_name("Sodium Chloride") == ""
    assert stripped_name("Calcium Chloride") == ""
    assert stripped_name("Sodium Acetate") == ""
    assert stripped_name("Calcium Carbonate") == ""
    # A real active with a salt counter-ion still keeps its base name.
    assert stripped_name("Potassium Citrate") == ""  # both tokens -> empty
    assert stripped_name("Diltiazem Hydrochloride") == "diltiazem"


def test_stripped_name_combo_sorted() -> None:
    s = stripped_name("Hydrocodone Bitartrate; Acetaminophen")
    assert s == "acetaminophen; hydrocodone"


def test_is_combo_false_for_single() -> None:
    assert not is_combo("Albuterol Sulfate")
