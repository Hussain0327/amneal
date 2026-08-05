"""The gold-set verifier: page pins must be derivable, not asserted.

Each test below is shaped after a REAL defect found on 2026-08-05 in the twelve
hand-authored gold rows (3 of 12 wrong -- a 25% defect rate). The failure mode is
what makes this worth a module: a wrong page does not break anything loudly, it
quietly depresses recall and citation precision for a CORRECT answer, which
pressures the team into lowering the thresholds to fit the broken asset.

All tests are network- and DB-free: the store lookup is monkeypatched.
"""

from __future__ import annotations

import pytest

from regwatch.eval import verify_gold
from regwatch.eval.metrics import GoldItem


def _stub_corpus(monkeypatch: pytest.MonkeyPatch, corpus: dict[tuple[str, int], list[str]]) -> None:
    """Replace the store lookup with an in-memory (short_name, page) -> texts map."""
    import regwatch.store.vector_store as vs

    monkeypatch.setattr(vs, "chunk_texts_at", lambda s, p: corpus.get((s, p), []))


def _item(sources: list[dict], facts: list[str] | None = None) -> GoldItem:
    return GoldItem(
        question="q?",
        expected_sources=sources,
        expected_facts=facts or [],
        category="current_version",
    )


def test_clean_item_has_no_defects(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_corpus(monkeypatch, {("PSG_1", 4): ["Design: Fasting, single-dose, two-way crossover."]})
    item = _item([{"short_name": "PSG_1", "page": 4, "quote": "two-way crossover"}])
    assert verify_gold.verify_items([item]) == []


def test_quote_absent_from_the_cited_page_is_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The levalbuterol shape: the row cited page 6; the text is on 4 and 5."""
    _stub_corpus(
        monkeypatch,
        {
            ("PSG_1", 4): ["Design: Fasting, single-dose, two-way crossover."],
            ("PSG_1", 6): ["Bronchoprovocation study design."],
        },
    )
    item = _item([{"short_name": "PSG_1", "page": 6, "quote": "two-way crossover"}])
    defects = verify_gold.verify_items([item])
    assert len(defects) == 1
    assert "quote not found on this page" in defects[0]


def test_missing_page_is_reported_differently_from_a_wrong_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct problems: a page the corpus lacks vs a page that is simply wrong.

    Collapsing them would send someone hunting for the right page number when the
    real problem is that the document is not in the corpus at all.
    """
    _stub_corpus(monkeypatch, {})
    item = _item([{"short_name": "PSG_ABSENT", "page": 3, "quote": "anything"}])
    defects = verify_gold.verify_items([item])
    assert len(defects) == 1
    assert "no chunk exists" in defects[0]


def test_a_source_without_a_quote_is_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unquoted pin is exactly the unverifiable assertion this module exists to end."""
    _stub_corpus(monkeypatch, {("PSG_1", 4): ["some text"]})
    defects = verify_gold.verify_items([_item([{"short_name": "PSG_1", "page": 4}])])
    assert len(defects) == 1
    assert "unverifiable" in defects[0]


def test_matching_tolerates_extraction_whitespace_and_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line wrapping and column padding are artifacts of PDF extraction and of where
    the chunker happened to split; a gold author cannot see them."""
    _stub_corpus(
        monkeypatch,
        {("PSG_1", 2): ["The  SAC   study\nshould be PERFORMED at the beginning (B),"]},
    )
    item = _item([{"short_name": "PSG_1", "page": 2, "quote": "the sac study should be performed"}])
    assert verify_gold.verify_items([item]) == []


def test_expected_fact_absent_from_every_cited_source_is_a_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The beclomethasone shape: facts pinned to pages 1 and 5, phrase only on page 2.

    A grounded answer can only contain what its evidence contains, so this row would
    fail fact_recall no matter how well retrieval performed.
    """
    _stub_corpus(
        monkeypatch,
        {
            ("PSG_1", 1): ["Header block and recommended studies summary."],
            ("PSG_1", 2): ["single actuation content (SAC) study design"],
        },
    )
    item = _item(
        [{"short_name": "PSG_1", "page": 1, "quote": "Header block"}],
        facts=["single actuation content"],
    )
    defects = verify_gold.verify_items([item])
    assert len(defects) == 1
    assert "appears in none of its expected_sources" in defects[0]


def test_fact_found_in_any_listed_source_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-source rows are legitimate: the fact need only be in one of them."""
    _stub_corpus(
        monkeypatch,
        {
            ("PSG_1", 1): ["Header block."],
            ("PSG_1", 2): ["single actuation content (SAC)"],
        },
    )
    item = _item(
        [
            {"short_name": "PSG_1", "page": 1, "quote": "Header block"},
            {"short_name": "PSG_1", "page": 2, "quote": "single actuation content"},
        ],
        facts=["single actuation content"],
    )
    assert verify_gold.verify_items([item]) == []


def test_every_defect_is_reported_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixing a 60-row asset one failure per run would take a week."""
    _stub_corpus(monkeypatch, {("PSG_1", 1): ["only this"]})
    items = [
        _item([{"short_name": "PSG_1", "page": 1, "quote": "not here"}]),
        _item([{"short_name": "PSG_2", "page": 9, "quote": "nor here"}]),
        _item([{"short_name": "PSG_1", "page": 1}]),
    ]
    assert len(verify_gold.verify_items(items)) == 3


def test_refusal_rows_have_nothing_to_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """must_refuse rows carry no sources by INV-1, so they cannot be mis-pinned."""
    _stub_corpus(monkeypatch, {})
    item = GoldItem(question="oos", expected_sources=[], category="refusal", must_refuse=True)
    assert verify_gold.verify_items([item]) == []
