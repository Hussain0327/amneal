"""INV-1 in REAL Qwen3 geometry: no cross-drug leak, cite-or-drop.

The rest of the suite runs ``EMBEDDING_PROVIDER=echo`` (conftest forces it), so
the cross-drug guard is only ever proven against hash-noise vectors. Echo
geometry is degenerate: two unrelated drugs' chunks are near-orthogonal by
construction, which is the EASY case. This test re-proves INV-1 against the
LIVE production space -- the Databricks-served Qwen3 profile arm -- where FDA
PSG boilerplate genuinely pulls unrelated drugs close together. That is the
space the ``normalized_name`` retrieval filter actually has to defend against.

HISTORY: until 2026-08-17 this file proved INV-1 in the OpenAI-1536 ROLLBACK
space (text-embedding-3-small), because nothing yet re-proved the live
1024-dim Qwen geometry -- a gap its own docstring recorded. The OpenAI
embedding provider was then removed outright (prod embeds through Qwen only),
so this test now targets the live geometry directly: it registers a profile
from the runner's QWEN_* settings, seeds both drugs through the REAL endpoint,
and asks through the production profile arm. Do not delete this file: echo
geometry cannot stand in for it (an audit on 2026-08-13 nearly removed it as
dead when its pointer went stale).

It is an EXTRA, opt-in test, gated on a DEDICATED flag so it never conscripts
the standard CI pytest step into live Databricks spend. CI exports live
Qwen/Databricks credentials for the eval/seed steps of other workflows, so
gating on mere credential-presence would silently fire billable embed calls on
every PR/push and -- worse -- turn the whole required suite RED on any
endpoint 429/5xx/timeout. Instead this mirrors the codebase's live-test
precedent (test_pgvector_store.py / test_postgres_bootstrap.py gate on a
separate TEST_DATABASE_URL): the live path runs ONLY when
``RUN_LIVE_QWEN_GEOMETRY=1`` is explicitly set alongside the endpoint values.
The only network calls are the Qwen embeddings of the seed chunks + query; the
synthesizer LLM is stubbed (no LLM spend, and the answer-side INV-1 logic
under test -- citation validation against the retrieved set -- is unaffected).
A transport failure on those calls degrades to skip, not a suite failure
(engineering standard: every external call has a defined behavior when it
fires).

Run it once locally with:
    RUN_LIVE_QWEN_GEOMETRY=1 QWEN_EMBEDDING_BASE_URL=... \\
        QWEN_EMBEDDING_TOKEN=... QWEN_EMBEDDING_MODEL=qwen3-embedding-0-6b \\
        QWEN_EMBEDDING_DIMENSION=1024 .venv/bin/python -m pytest \\
        tests/test_inv1_real_geometry.py -q
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
    ensure_profile_hnsw_index,
    register_embedding_profile,
    upsert_profile_embeddings,
)

pytestmark = pytest.mark.invariants

# Dedicated opt-in flag; see the module docstring for why credential-presence
# alone must never enable this.
_RUN_LIVE = os.environ.get("RUN_LIVE_QWEN_GEOMETRY") == "1"
# conftest._isolate_env (autouse) BLANKS the QWEN_* endpoint values in
# os.environ via monkeypatch before any test body runs, so the operator's real
# values must be captured at IMPORT time (before that fixture fires) to
# restore them inside the test. Only read when opted in, so unrelated exported
# credentials never enable this.
_REAL_QWEN_ENV: dict[str, str] = (
    {
        name: os.environ.get(name, "")
        for name in (
            "QWEN_EMBEDDING_BASE_URL",
            "QWEN_EMBEDDING_TOKEN",
            "QWEN_EMBEDDING_MODEL",
            "QWEN_EMBEDDING_DIMENSION",
            "QWEN_EMBEDDING_REVISION",
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
    """A registrable profile matching the runner's live Qwen settings."""
    from config.settings import get_settings

    from regwatch.process.chunker import CHUNKING_VERSION
    from regwatch.process.embedder import (
        QWEN3_DOCUMENT_PREPROCESSING_VERSION,
    )

    settings = get_settings()
    return EmbeddingProfileSpec(
        provider="qwen3",
        model=settings.qwen_embedding_model,
        revision=settings.qwen_embedding_revision,
        dimension=settings.qwen_embedding_dimension,
        dtype="float32",
        normalization="l2",
        query_instruction_version=settings.qwen_embedding_query_instruction_version,
        preprocessing_version=QWEN3_DOCUMENT_PREPROCESSING_VERSION,
        chunking_version=CHUNKING_VERSION,
        serving_runtime_version="live-geometry-test",
    )


def _seed_real(profile_id: str, provider: Any) -> None:
    """Seed both drugs with REAL Qwen vectors (the one allowed network call).

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


def _reload_with_live_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the operator's real QWEN_* env that conftest blanked.

    Same mechanism as tests/test_provider_guard.py::_reload_settings: set env
    via monkeypatch (auto-reverted) then clear the settings LRU so the next
    get_settings() observes the change.
    """
    import config.settings as cs

    monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
    for name, value in _REAL_QWEN_ENV.items():
        if value:
            monkeypatch.setenv(name, value)
    cs.get_settings.cache_clear()


@pytest.mark.skipif(
    not (
        _RUN_LIVE
        and _REAL_QWEN_ENV.get("QWEN_EMBEDDING_BASE_URL")
        and _REAL_QWEN_ENV.get("QWEN_EMBEDDING_TOKEN")
    ),
    reason=(
        "set RUN_LIVE_QWEN_GEOMETRY=1 with QWEN_EMBEDDING_BASE_URL/TOKEN to "
        "run the real-geometry test"
    ),
)
def test_inv1_no_cross_drug_leak_real_qwen_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    _reload_with_live_qwen(monkeypatch)
    init_db(assert_provider=False)
    profile = register_embedding_profile(_live_profile_spec())
    # concurrently=False: a CONCURRENTLY build cannot run inside the test
    # engine's transaction, and the distinction is irrelevant here.
    ensure_profile_hnsw_index(profile.profile_id, concurrently=False)
    provider = get_embedding_provider_for_profile(profile)

    # The network calls (embedding the seed chunks, then the query inside
    # ask()). A transport failure is an endpoint-availability issue, not an
    # INV-1 regression, so degrade to skip -- assertion failures below are
    # AssertionError and still propagate.
    try:
        _seed_real(profile.profile_id, provider)
    except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"Qwen embedding call failed ({exc!r}); skipping real-geometry test")

    # Serve retrieval through the profile arm, exactly like production.
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", profile.profile_id)
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
        pytest.skip(f"Qwen embedding call failed ({exc!r}); skipping real-geometry test")

    # INV-1a: retrieval was scoped to drug A -- NOT ONE drug-B chunk surfaced,
    # in real Qwen geometry where the shared BE boilerplate makes B cosine-near.
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
