"""MMR diversity in stage 2 (docs/DSA.md section 33).

Pure selection tests: no DB, no LLM, no embeddings, no network. The unit under
test is `regwatch.retrieve.diversity.mmr_select` plus the one seam that calls
it, `grounded_qa._trim_evidence`, so a stand-in with the two fields MMR reads
is enough for the algorithm and the real `RetrievedPassage` pins the seam.

The load-bearing contract is that the flag is OFF by default and that OFF is
the byte-identical old slice: production behaviour may not move until an eval
A/B flips it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from config.settings import Settings

from regwatch.generate import grounded_qa as qa_mod
from regwatch.retrieve.diversity import mmr_select
from regwatch.retrieve.retriever import RetrievedPassage


@dataclass
class _Passage:
    """Stand-in mirroring the RetrievedPassage fields MMR reads."""

    chunk_id: str
    text: str
    score: float


# Two chunks saying the same thing in almost the same words (the pathology:
# one piece of evidence dressed as two) plus a distinct, lower-scoring one.
_CLONE_A = "Conduct a single dose fasting bioequivalence study in healthy adults."
_CLONE_B = "Conduct a single dose fasting bioequivalence study in healthy adult subjects."
_DISTINCT = "Dissolution testing should use USP Apparatus 2 at 50 rpm."


def _pool() -> list[_Passage]:
    """A score-ordered pool of two near-clones and one distinct passage."""
    return [
        _Passage("c0", _CLONE_A, 0.90),
        _Passage("c1", _CLONE_B, 0.88),
        _Passage("c2", _DISTINCT, 0.60),
    ]


def _ids(passages: list[_Passage]) -> list[str]:
    return [p.chunk_id for p in passages]


def _real(chunk_id: str, text: str, score: float) -> RetrievedPassage:
    """A real RetrievedPassage; only text/score matter to the selection."""
    return RetrievedPassage(
        chunk_id=chunk_id,
        text=text,
        score=score,
        doc_id=1,
        version_id=10,
        page=1,
        section_path="II.A",
        normalized_name="albuterol",
        source_url="http://example/psg.pdf",
        short_name="PSG_020503",
        metadata={},
    )


def _settings(monkeypatch: pytest.MonkeyPatch, *, mmr: bool) -> Settings:
    """Settings with the diversity flag pinned and a final-k of 2."""
    monkeypatch.setenv("REGWATCH_MMR_DIVERSITY", "1" if mmr else "0")
    monkeypatch.setenv("RERANK_TOP_K", "2")
    import config.settings as cs

    cs.get_settings.cache_clear()
    return cs.get_settings()


# ---------- The point of the change ----------


def test_near_duplicate_loses_its_slot_to_a_distinct_passage() -> None:
    """Plain top-k keeps the clone; MMR spends the slot on new evidence."""
    pool = _pool()
    assert _ids(pool[:2]) == ["c0", "c1"], "plain top-k is the clone pair"
    assert _ids(mmr_select(pool, 2)) == ["c0", "c2"]


def test_selection_size_is_unchanged() -> None:
    """MMR diversifies WHICH passages, never how many."""
    assert len(mmr_select(_pool(), 2)) == 2


# ---------- Control arm: lambda_ = 1.0 is pure relevance ----------


def test_lambda_one_is_the_plain_top_k_slice() -> None:
    pool = _pool()
    assert mmr_select(pool, 2, lambda_=1.0) == pool[:2]


def test_lambda_one_keeps_a_non_score_order() -> None:
    """The no-op arm respects the caller's order, not the score order.

    The optional cross-encoder reranker leaves the pool ordered by a score it
    does NOT write back to `.score`, so an argmax-by-score implementation of
    "pure relevance" would silently undo it. This pins the identity.
    """
    pool = _pool()
    reordered = [pool[2], pool[0], pool[1]]
    assert mmr_select(reordered, 2, lambda_=1.0) == [pool[2], pool[0]]


# ---------- Determinism ----------


def test_repeated_calls_select_the_same_passages() -> None:
    pool = _pool()
    first = _ids(mmr_select(pool, 2))
    for _ in range(5):
        assert _ids(mmr_select(pool, 2)) == first


def test_ties_break_by_original_rank() -> None:
    """Equal score AND equal text: the earlier candidate wins, every time."""
    pool = [
        _Passage("dup-first", _CLONE_A, 0.50),
        _Passage("dup-second", _CLONE_A, 0.50),
        _Passage("other", _DISTINCT, 0.40),
    ]
    assert _ids(mmr_select(pool, 1)) == ["dup-first"]
    assert _ids(mmr_select(pool, 2)) == ["dup-first", "other"]


def test_non_finite_score_does_not_win_the_argmax() -> None:
    """A junk score from a broken provider must not poison the comparison."""
    pool = [
        _Passage("nan", _DISTINCT, float("nan")),
        _Passage("good", _CLONE_A, 0.50),
    ]
    assert _ids(mmr_select(pool, 1)) == ["good"]


# ---------- Degenerate inputs ----------


def test_empty_pool_selects_nothing() -> None:
    assert mmr_select([], 5) == []


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_selects_nothing(k: int) -> None:
    assert mmr_select(_pool(), k) == []


@pytest.mark.parametrize("k", [3, 4, 99])
def test_k_at_or_above_pool_size_is_the_identity(k: int) -> None:
    """Nothing to drop means nothing to select: the caller's order survives.

    Deliberately fed in a NON-score order, so an implementation that ran the
    selection anyway would reorder and fail here.
    """
    pool = _pool()
    reordered = [pool[2], pool[0], pool[1]]
    assert mmr_select(reordered, k) == reordered


def test_single_passage_pool() -> None:
    pool = [_Passage("only", _CLONE_A, 0.10)]
    assert mmr_select(pool, 1) == pool


def test_untokenizable_text_is_never_penalized() -> None:
    """Punctuation-only chunks share no tokens, so they cannot look alike."""
    pool = [
        _Passage("p0", "...", 0.90),
        _Passage("p1", "---", 0.80),
        _Passage("p2", _CLONE_A, 0.70),
    ]
    assert _ids(mmr_select(pool, 2)) == ["p0", "p1"]


# ---------- The seam ----------


def test_flag_defaults_off() -> None:
    """Prod stays bit-identical until an eval A/B flips this."""
    assert Settings.model_fields["mmr_diversity_enabled"].default is False


def test_seam_flag_off_is_the_plain_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    passages = [
        _real("c0", _CLONE_A, 0.90),
        _real("c1", _CLONE_B, 0.88),
        _real("c2", _DISTINCT, 0.60),
    ]
    s = _settings(monkeypatch, mmr=False)
    assert qa_mod._trim_evidence(passages, s) == passages[:2]


def test_seam_flag_on_diversifies(monkeypatch: pytest.MonkeyPatch) -> None:
    passages = [
        _real("c0", _CLONE_A, 0.90),
        _real("c1", _CLONE_B, 0.88),
        _real("c2", _DISTINCT, 0.60),
    ]
    s = _settings(monkeypatch, mmr=True)
    kept = qa_mod._trim_evidence(passages, s)
    assert [p.chunk_id for p in kept] == ["c0", "c2"]


def test_seam_flag_on_never_selects_sub_threshold_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diverse but sub-threshold passage must not cost an evidence slot.

    The INV-2 filter downstream would drop it, shrinking the evidence set
    below k and confounding the diversity A/B with a count change.
    """
    passages = [
        _real("c0", _CLONE_A, 0.90),
        _real("c1", _CLONE_B, 0.88),
        _real("c2", _DISTINCT, 0.20),
    ]
    s = _settings(monkeypatch, mmr=True)
    kept = qa_mod._trim_evidence(passages, s)
    assert [p.chunk_id for p in kept] == ["c0", "c1"]


def test_seam_flag_on_all_sub_threshold_is_the_plain_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing eligible: the refusal path must see the flag-off passages."""
    passages = [
        _real("c0", _CLONE_A, 0.25),
        _real("c1", _CLONE_B, 0.22),
        _real("c2", _DISTINCT, 0.10),
    ]
    s = _settings(monkeypatch, mmr=True)
    assert qa_mod._trim_evidence(passages, s) == passages[:2]
