"""Citations must name a product, and must keep naming it after a reload.

Audit #1716: an answer's provenance rendered as "PSG_020911 . p.1" -- an FDA
application number and nothing else -- while the product identity sat unused on
the passage. Separately, recency was joined only on the response path, so every
reopened conversation degraded to "Revision date not recorded" permanently.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from regwatch.generate import grounded_qa
from regwatch.generate.rag_contract import Citation
from regwatch.generate.turn_gate import _citation_for
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.queries import CitationRecency, RecencyIndex


def _passage(**overrides: Any) -> RetrievedPassage:
    base: dict[str, Any] = {
        "chunk_id": "c1",
        "text": "Four in-vitro bioequivalence studies are recommended.",
        "score": 0.61,
        "doc_id": 41,
        "version_id": 4137,
        "page": 1,
        "section_path": None,
        "normalized_name": "beclomethasone dipropionate",
        "source_url": "https://example.test/psg.pdf",
        "short_name": "PSG_020911",
        "metadata": {
            "dosage_form": "AEROSOL, METERED",
            "route": "INHALATION",
            "psg_type": "final",
        },
    }
    base.update(overrides)
    return RetrievedPassage(**base)


def test_citation_carries_product_identity() -> None:
    """The four fields the UI needs all ride the passage already."""
    citation = _citation_for(_passage())
    assert citation.product_name == "beclomethasone dipropionate"
    assert citation.dosage_form == "AEROSOL, METERED"
    assert citation.route == "INHALATION"
    assert citation.psg_type == "final"


def test_blank_metadata_collapses_to_none() -> None:
    """Ingest writes "" for an unknown form/route; "" must not reach the UI.

    A blank string would render as a stray separator and would make "not
    recorded" indistinguishable from "not loaded".
    """
    citation = _citation_for(
        _passage(metadata={"dosage_form": "  ", "route": "", "psg_type": None})
    )
    assert citation.dosage_form is None
    assert citation.route is None
    assert citation.psg_type is None


def test_missing_metadata_keys_do_not_raise() -> None:
    citation = _citation_for(_passage(metadata={}))
    assert citation.dosage_form is None
    assert citation.product_name == "beclomethasone dipropionate"


def test_recency_lands_on_the_domain_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix for reload decay: the date is resolved BEFORE persistence.

    _build_patch serializes the DOMAIN citation, so a recommended_date that
    only ever existed on the wire model could never survive a reload.
    """
    monkeypatch.setattr(
        grounded_qa,
        "fetch_citation_recency",
        lambda version_ids, doc_ids: RecencyIndex(
            by_version={4137: CitationRecency(recommended_date="2021-03-15", diff_summary="rev 4")},
            doc_dates={},
        ),
    )
    enriched = grounded_qa._enrich_citation_recency([_citation_for(_passage())])
    assert enriched[0].recommended_date == "2021-03-15"
    assert enriched[0].diff_summary == "rev 4"
    # The persisted shape is asdict() of exactly this object.
    assert asdict(enriched[0])["recommended_date"] == "2021-03-15"


def test_recency_failure_still_yields_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recency lookup that returns nothing must not cost the answer."""
    monkeypatch.setattr(
        grounded_qa,
        "fetch_citation_recency",
        lambda version_ids, doc_ids: RecencyIndex(by_version={}, doc_dates={}),
    )
    enriched = grounded_qa._enrich_citation_recency([_citation_for(_passage())])
    assert len(enriched) == 1
    assert enriched[0].recommended_date is None
    assert enriched[0].short_name == "PSG_020911"


def test_enrichment_skips_the_query_when_there_is_nothing_to_enrich(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(version_ids: list[int], doc_ids: list[int]) -> RecencyIndex:
        raise AssertionError("must not query for an uncited turn")

    monkeypatch.setattr(grounded_qa, "fetch_citation_recency", _boom)
    assert grounded_qa._enrich_citation_recency([]) == []


def test_legacy_citation_dict_still_deserializes() -> None:
    """A citation persisted before the identity fields existed must load.

    Every new field is optional with a None default precisely so history keeps
    rendering (the UI falls back to short_name when product_name is None).
    """
    citation = Citation(
        short_name="PSG_020911",
        page=1,
        chunk_id="c1",
        doc_id=41,
        version_id=4137,
        source_url="https://example.test/psg.pdf",
        snippet="...",
    )
    assert citation.product_name is None
    assert citation.recommended_date is None
