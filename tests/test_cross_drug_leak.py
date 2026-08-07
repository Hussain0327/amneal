"""INV-9: product resolution before retrieval prevents cross-drug citation leak.

FDA PSG templates reuse identical language across drugs. Here albuterol and
beclomethasone chunks carry the SAME "Single actuation content (SAC)" /
"two-way crossover" boilerplate. A beclomethasone question must:
  - resolve to beclomethasone and filter retrieval to it,
  - never retrieve an albuterol chunk,
  - never let an albuterol citation survive validation,
  - never let the SENTENCE that carried the albuterol citation survive either.

That last clause is new. Under the prose gate, filter_citations rewrote a mixed
bracket down to its allowed pairs, so a claim whose real source was an albuterol
page kept standing under beclomethasone's citation. The structured turn contract
drops such a claim WHOLE, so the cross-drug text is gone, not re-stamped.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

pytestmark = pytest.mark.invariants

# (text, appl_no, normalized_name, page) — same boilerplate across both drugs.
_BOILERPLATE = "Type of study: Single actuation content (SAC). Fasting, two-way crossover, in vivo."
_ROWS = [
    (_BOILERPLATE, "020503", "albuterol sulfate", 2),
    ("Albuterol strengths and reference details.", "020503", "albuterol sulfate", 1),
    (_BOILERPLATE, "020911", "beclomethasone dipropionate", 2),
    ("Beclomethasone strengths and reference details.", "020911", "beclomethasone dipropionate", 1),
]


def _seed() -> None:
    init_db()
    emb = get_embedding_provider()
    texts = [t for t, _, _, _ in _ROWS]
    vecs = emb.embed(texts)
    ids = [f"{appl}-{page}" for _, appl, _, page in _ROWS]
    metas = [
        {
            "doc_id": idx + 1,
            "version_id": (idx + 1) * 10,
            "page": page,
            "normalized_name": name,
            "appl_no": appl,
            "source_url": f"http://example/PSG_{appl}.pdf",
            "section_path": "",
            "dosage_form": "Aerosol, Metered",
            "route": "Inhalation",
            "psg_type": "draft",
        }
        for idx, (_, appl, name, page) in enumerate(_ROWS)
    ]
    add_chunks(ids=ids, embeddings=vecs, documents=texts, metadatas=metas)


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


def _turn(claims: list[tuple[str, list[tuple[str, int]]]]) -> str:
    """One structured synthesizer completion (see generate/turn_schema.py).

    The synthesizer no longer writes prose or markers: it declares
    (short_name, page) per claim and the renderer stamps validated passages.
    Built through the ONE shared payload seam (tests/conftest.synth_turn_json)
    so a synthesis-format change is one edit, not one per stub module (F10).
    """
    return synth_turn_json(claims)


def _zero_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reach the LLM path regardless of the calibrated retrieval floor."""
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()


def test_beclomethasone_leaked_citation_drops_its_claim_not_the_valid_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-drug cite kills its own claim; the in-drug claim still answers.

    MEANING CHANGED. The old assertion was "the leaked pair is scrubbed from a
    bracket and the sentence is SURFACED anyway" -- which is precisely the hole:
    the surfaced sentence was the model's cross-drug text wearing
    beclomethasone's citation. Now the whole claim is dropped, so the leak
    cannot be laundered onto a valid passage, and a separate, genuinely grounded
    claim still reaches the user (previously the whole turn refused).
    """
    _seed()
    _zero_threshold(monkeypatch)
    completion = _turn(
        [
            # Grounded in the beclomethasone page that WAS retrieved.
            (
                "The recommended study type is single actuation content (SAC).",
                [("PSG_020911", 2)],
            ),
            # The leaked albuterol pair rides in the SAME claim as a valid pair:
            # the whole claim must die, not just the bad pair (OD-4). No
            # materiality word here, so this exercises the PARTIAL branch.
            (
                "The albuterol product uses the same fasting two-way crossover design.",
                [("PSG_020911", 2), ("PSG_020503", 2)],
            ),
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))

    result = qa_mod.ask("What type of study does the beclomethasone dipropionate PSG recommend?")

    assert not result.refused
    # Retrieval was constrained to beclomethasone -- no albuterol chunk surfaced.
    assert {r["short_name"] for r in result.retrieved} == {"PSG_020911"}
    # The leaked albuterol citation did not survive validation...
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020911", 2)}
    # ...it is absent from the rendered answer...
    assert "PSG_020503" not in result.answer
    # ...and so is the sentence that carried it: NOT re-stamped onto the
    # beclomethasone passage, which is what the old bracket rewrite did.
    assert "albuterol" not in result.answer.lower()
    # The valid neighbouring claim survives, cited.
    assert (
        "The recommended study type is single actuation content (SAC) [PSG_020911, p.2]."
        in result.answer
    )
    assert tg.PARTIAL_DROP_DISCLOSURE in result.answer


def test_beclomethasone_answer_built_only_on_albuterol_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every claim citing the wrong drug leaves nothing to render -> refuse.

    The other half of whole-claim drop: with no admitted claim there is no
    partial answer to disclose, and the boilerplate sentence (identical text in
    both PSGs, so it "looks" supported) must not be quietly re-attributed to the
    beclomethasone passage that WAS retrieved.
    """
    _seed()
    _zero_threshold(monkeypatch)
    completion = _turn(
        [
            (
                "Type of study: single actuation content (SAC), fasting, two-way crossover.",
                [("PSG_020503", 2)],
            )
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))

    result = qa_mod.ask("What type of study does the beclomethasone dipropionate PSG recommend?")

    assert result.refused
    assert result.citations == []
    assert "PSG_020503" not in result.answer
    assert "PSG_020911" not in result.answer
    assert "single actuation" not in result.answer.lower()
