"""Opt-in INV-1 check against real OpenAI embedding geometry.

The normal suite uses deterministic echo vectors. This test instead registers
an OpenAI text-embedding-3-large profile at 1024 dimensions, embeds two drugs
that share FDA boilerplate, and proves product scoping prevents cross-drug
retrieval and citation leakage.

It runs only when RUN_LIVE_OPENAI_GEOMETRY=1 and OPENAI_API_KEY are present.
The synthesizer is stubbed, so the only external spend is embeddings.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider_for_profile
from regwatch.store.db import init_db
from regwatch.store.embedding_profiles import EmbeddingProfileSpec
from regwatch.store.vector_store import (
    add_chunks,
    register_embedding_profile,
    upsert_profile_embeddings,
)

pytestmark = pytest.mark.invariants

# Dedicated opt-in flag; see the module docstring for why credential-presence
# alone must never enable this.
_RUN_LIVE = os.environ.get("RUN_LIVE_OPENAI_GEOMETRY") == "1"
# Capture the opted-in OpenAI values before conftest blanks the API key.
_REAL_OPENAI_ENV: dict[str, str] = (
    {
        name: os.environ.get(name, "")
        for name in (
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_EMBEDDING_MODEL",
            "OPENAI_EMBEDDING_DIMENSION",
        )
    }
    if _RUN_LIVE
    else {}
)

# Two distinct, public, single-ingredient drugs whose FDA guidance shares
# verbatim BE-study boilerplate -- the exact wording that makes cross-drug leak
# plausible in real embedding space (cosine-near, not orthogonal like echo).
_BOILERPLATE = (
    "Type of study: Fasting. Design: single-dose, two-way crossover, in vivo. "
    "Analytes to measure: parent drug in plasma. Statistical information: "
    "equal variance, log-transformed AUC and Cmax, 90% confidence interval."
)
# (text, appl_no, normalized_name, page)
_ROWS = [
    (
        "Albuterol sulfate inhalation. " + _BOILERPLATE,
        "020503",
        "albuterol sulfate",
        2,
    ),
    (
        "Albuterol sulfate strengths: 90 mcg per actuation. Reference listed drug "
        "details and dissolution acceptance criteria for the aerosol product.",
        "020503",
        "albuterol sulfate",
        1,
    ),
    (
        "Metformin hydrochloride extended-release tablets. " + _BOILERPLATE,
        "021202",
        "metformin hydrochloride",
        2,
    ),
    (
        "Metformin hydrochloride strengths: 500 mg, 750 mg, 1000 mg extended "
        "release. Reference listed drug and dissolution method for the tablet.",
        "021202",
        "metformin hydrochloride",
        1,
    ),
]

_DRUG_A = "albuterol sulfate"
_DRUG_A_SHORT = "PSG_020503"
_DRUG_B_SHORT = "PSG_021202"


def _live_profile_spec() -> EmbeddingProfileSpec:
    """A registrable profile matching the runner's live OpenAI settings."""
    from config.settings import get_settings

    from regwatch.process.chunker import CHUNKING_VERSION
    from regwatch.process.embedder import (
        OPENAI_DOCUMENT_PREPROCESSING_VERSION,
        OPENAI_QUERY_INSTRUCTION_VERSION,
    )

    settings = get_settings()
    model = settings.openai_embedding_model or "text-embedding-3-large"
    return EmbeddingProfileSpec(
        provider="openai",
        model=model,
        revision=model,
        dimension=settings.openai_embedding_dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version=OPENAI_QUERY_INSTRUCTION_VERSION,
        preprocessing_version=OPENAI_DOCUMENT_PREPROCESSING_VERSION,
        chunking_version=CHUNKING_VERSION,
        serving_runtime_version="live-geometry-test",
    )


def _seed_real(profile_id: str, provider: Any) -> None:
    """Seed both drugs with real OpenAI vectors.

    Mirrors tests/test_cross_drug_leak.py::_seed but through the profile arm:
    chunk rows carry no legacy vector (embeddings=None, the post-cutover prod
    shape) and the real vectors land in chunk_embedding for the profile.
    """
    texts = [t for t, _, _, _ in _ROWS]
    vecs = provider.embed_documents(texts)
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
            "dosage_form": (
                "Tablet, Extended Release" if name.startswith("metformin") else "Aerosol, Metered"
            ),
            "route": "Oral" if name.startswith("metformin") else "Inhalation",
            "psg_type": "draft",
        }
        for idx, (_, appl, name, page) in enumerate(_ROWS)
    ]
    add_chunks(ids=ids, embeddings=[None] * len(ids), documents=texts, metadatas=metas)
    from regwatch.store.embedding_profiles import content_hash

    upsert_profile_embeddings(profile_id, ids, vecs, [content_hash(t) for t in texts])


def _stub_synth(text: str) -> Any:
    """A synthesizer stub so the test spends ZERO LLM tokens.

    INV-1's answer-side logic (validate every emitted citation against the
    passages actually retrieved; refuse if none survive) is independent of WHICH
    LLM produced the text, so stubbing it does not weaken what we assert -- the
    geometry under test lives entirely in the real-embedding retrieval above.
    """

    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


def _reload_with_live_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the opted-in OpenAI environment that conftest blanked.

    Same mechanism as tests/test_provider_guard.py::_reload_settings: set env
    via monkeypatch (auto-reverted) then clear the settings LRU so the next
    get_settings() observes the change.
    """
    import config.settings as cs

    monkeypatch.setenv("INGEST_EMBEDDING_PROVIDER", "openai")
    for name, value in _REAL_OPENAI_ENV.items():
        if value:
            monkeypatch.setenv(name, value)
    cs.get_settings.cache_clear()


@pytest.mark.skipif(
    not (_RUN_LIVE and _REAL_OPENAI_ENV.get("OPENAI_API_KEY")),
    reason="set RUN_LIVE_OPENAI_GEOMETRY=1 with OPENAI_API_KEY to run this test",
)
def test_inv1_no_cross_drug_leak_real_openai_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    _reload_with_live_openai(monkeypatch)
    init_db(assert_provider=False)
    profile = register_embedding_profile(_live_profile_spec())
    provider = get_embedding_provider_for_profile(profile)

    # The network calls (embedding the seed chunks, then the query inside
    # ask()). A transport failure is an endpoint-availability issue, not an
    # INV-1 regression, so degrade to skip -- assertion failures below are
    # AssertionError and still propagate.
    try:
        _seed_real(profile.profile_id, provider)
    except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"OpenAI embedding call failed ({exc!r}); skipping real-geometry test")

    # Serve retrieval through the profile arm, exactly like production.
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_PROFILE", profile.profile_id)
    cs.get_settings.cache_clear()

    # A grounded answer that cites drug A's real chunk. If the retrieval filter
    # leaked drug B, the answer-side guard would still strip a B citation (it is
    # never in the retrieved set), so the assertions below test BOTH directions.
    answer = (
        "FDA recommends a fasting, single-dose, two-way crossover in vivo study "
        f"[{_DRUG_A_SHORT}, p.2]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_synth(answer))

    # No normalized_name filter: force ask() to resolve the product from the
    # query text against the REAL-embedding corpus, then defend the scope itself.
    try:
        result = qa_mod.ask(
            "What bioequivalence study design does FDA recommend for albuterol sulfate?"
        )
    except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"OpenAI embedding call failed ({exc!r}); skipping real-geometry test")

    # INV-1a: retrieval was scoped to drug A -- NOT ONE drug-B chunk surfaced,
    # in real OpenAI geometry where the shared BE boilerplate makes B cosine-near.
    retrieved_shorts = {r["short_name"] for r in result.retrieved}
    assert _DRUG_B_SHORT not in retrieved_shorts, retrieved_shorts
    assert retrieved_shorts <= {_DRUG_A_SHORT}, retrieved_shorts
    # Every retrieved chunk is drug A's (belt and suspenders on normalized_name).
    assert all(r.get("normalized_name") == _DRUG_A for r in result.retrieved), result.retrieved

    # INV-1a (citations): every surviving citation resolves to drug A -- zero leak.
    assert all(c.short_name == _DRUG_A_SHORT for c in result.citations), result.citations

    # INV-1b: refuse-or-cite. Either a refusal (allowed -- real cosine for the
    # query may fall under the 0.30 threshold, which is UNVALIDATED in this
    # geometry), or a grounded answer that carries BOTH prose and >=1 valid
    # citation. Never an uncited non-refusal claim.
    if result.refused:
        assert result.citations == []
    else:
        assert result.citations, "non-refused answer must carry >=1 citation (INV-1)"
        assert result.answer.strip(), "non-refused answer must carry prose (INV-1)"
        # The cited answer's own short_name must be the one we retrieved.
        assert {c.short_name for c in result.citations} == {_DRUG_A_SHORT}
