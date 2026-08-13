"""INV-1 in REAL OpenAI-1536 geometry: no cross-drug leak, cite-or-drop.

The rest of the suite runs ``EMBEDDING_PROVIDER=echo`` (conftest forces it), so
the cross-drug guard is only ever proven against hash-noise vectors. Echo
geometry is degenerate: two unrelated drugs' chunks are near-orthogonal by
construction, which is the EASY case. This test re-proves INV-1 against the
OpenAI-1536 space (text-embedding-3-small) where FDA PSG
boilerplate genuinely pulls unrelated drugs close together -- the space the
``normalized_name`` retrieval filter actually has to defend against.

NOTE (2026-08-11): this is no longer the production space. Prod moved to the
Databricks Qwen3 profile (1024 dims) on 2026-07-30, so this test now proves the
cross-drug guard in the OpenAI-1536 ROLLBACK space only. Nothing re-proves INV-1
geometry in the live 1024-dim space yet, and nothing else in the repo records
that gap: docs/ROADMAP.md tracks only the related 0.30 refusal-threshold
revalidation, not this cross-drug guard. This docstring is the record -- an
audit on 2026-08-13 nearly deleted the file as dead because that pointer was
stale and the skip made it read as coverage nobody had.

It is an EXTRA, opt-in test, gated on a DEDICATED flag so it never conscripts
the standard CI pytest step into live OpenAI spend. The repo's blocking CI job
exports the production OPENAI_API_KEY (for the eval/seed steps) while running
`uv run pytest`, so gating on mere key-presence would silently fire a billable
embed call on every PR/push and -- worse -- turn the whole required suite RED on
any OpenAI 429/5xx/timeout. Instead this mirrors the codebase's existing
live-test precedent (test_pgvector_store.py / test_postgres_bootstrap.py gate on
a separate TEST_DATABASE_URL, never on prod DATABASE_URL): the live path runs
ONLY when ``RUN_LIVE_OPENAI_GEOMETRY=1`` is explicitly set alongside a key. It
also skips at COLLECTION when the `openai` SDK is absent, so it can never break
collection or a keyless run. The only network call is the OpenAI embedding of
the seed chunks + query; the synthesizer LLM is stubbed (no LLM spend, and the
answer-side INV-1 logic under test -- citation validation against the retrieved
set -- is unaffected). A transport failure on that one call degrades to skip,
not a suite failure (engineering standard: every external call has a defined
behavior when it fires).

Run it once locally with:
    RUN_LIVE_OPENAI_GEOMETRY=1 OPENAI_API_KEY=... uv run pytest \\
        tests/test_inv1_real_geometry.py -q
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Skip at COLLECTION time, never error: importorskip no-ops the module when the
# OpenAI SDK extra isn't installed (slim image / CI without `--extra llm`).
openai = pytest.importorskip("openai")

# E402: these regwatch imports MUST follow importorskip so the module no-ops
# (skips) before importing app code when the openai SDK extra is absent.
from regwatch.generate import grounded_qa as qa_mod  # noqa: E402
from regwatch.generate.llm import LLMResponse  # noqa: E402
from regwatch.process.embedder import get_embedding_provider  # noqa: E402
from regwatch.store.db import init_db  # noqa: E402
from regwatch.store.vector_store import add_chunks  # noqa: E402

pytestmark = pytest.mark.invariants

# Dedicated opt-in flag. The blocking CI pytest step runs with the production
# OPENAI_API_KEY exported (it is consumed by the live eval/seed steps), so gating
# the live path on key-presence alone would make this the ONE test that bills a
# real embed call on every CI run and fails the required suite on any OpenAI
# outage. We therefore require an EXPLICIT opt-in -- the same shape as the
# TEST_DATABASE_URL gate the Postgres integration tests use -- so CI keeps
# skipping cleanly even with the eval secret set.
_RUN_LIVE = os.environ.get("RUN_LIVE_OPENAI_GEOMETRY") == "1"
# conftest._isolate_env (autouse) BLANKS OPENAI_API_KEY in os.environ via
# monkeypatch before any test body runs, so we must capture the operator's real
# key at IMPORT time (before that fixture fires) to restore it inside the test.
# Only read it when opted in, so an unrelated exported key never enables this.
_REAL_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "") if _RUN_LIVE else ""

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


def _seed_real() -> None:
    """Seed both drugs with REAL OpenAI-1536 vectors (the one allowed network call).

    Mirrors tests/test_cross_drug_leak.py::_seed but the provider is now openai,
    so the geometry is production-faithful. The per-test Chroma dir (conftest's
    tmp_path) is empty, so these 1536-dim vectors define the collection
    dimension -- no clash with the 384-dim echo/local default.
    """
    init_db()
    emb = get_embedding_provider()
    assert emb.name == "openai" and emb.dim == 1536  # provider swap took effect
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
            "dosage_form": (
                "Tablet, Extended Release" if name.startswith("metformin") else "Aerosol, Metered"
            ),
            "route": "Oral" if name.startswith("metformin") else "Inhalation",
            "psg_type": "draft",
        }
        for idx, (_, appl, name, page) in enumerate(_ROWS)
    ]
    add_chunks(ids=ids, embeddings=vecs, documents=texts, metadatas=metas)


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


def _reload_with_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch THIS test to EMBEDDING_PROVIDER=openai (1536), reverting at teardown.

    Same mechanism as tests/test_provider_guard.py::_reload_settings: set env via
    monkeypatch (auto-reverted) then clear the settings LRU so the next
    get_settings() observes the change. Restores the real OPENAI_API_KEY that
    conftest blanked.
    """
    import config.settings as cs

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", _REAL_OPENAI_KEY)
    cs.get_settings.cache_clear()


@pytest.mark.skipif(
    not (_RUN_LIVE and _REAL_OPENAI_KEY),
    reason="set RUN_LIVE_OPENAI_GEOMETRY=1 and OPENAI_API_KEY to run the real-geometry test",
)
def test_inv1_no_cross_drug_leak_real_openai_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_with_openai(monkeypatch)
    # The ONE network call (embedding the seed chunks). A transport failure here
    # is an OpenAI-availability issue, not an INV-1 regression, so degrade to
    # skip -- assertion failures below are AssertionError and still propagate.
    try:
        _seed_real()
    except openai.OpenAIError as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"OpenAI embedding call failed ({exc!r}); skipping real-geometry test")

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
    # ask() embeds the query (second/last network call); same skip-on-transport.
    try:
        result = qa_mod.ask(
            "What bioequivalence study design does FDA recommend for albuterol sulfate?"
        )
    except openai.OpenAIError as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"OpenAI embedding call failed ({exc!r}); skipping real-geometry test")

    # INV-1a: retrieval was scoped to drug A -- NOT ONE drug-B chunk surfaced, in
    # real 1536 geometry where the shared BE boilerplate makes B cosine-near.
    retrieved_shorts = {r["short_name"] for r in result.retrieved}
    assert _DRUG_B_SHORT not in retrieved_shorts, retrieved_shorts
    assert retrieved_shorts <= {_DRUG_A_SHORT}, retrieved_shorts
    # Every retrieved chunk is drug A's (belt and suspenders on normalized_name).
    assert all(r.get("normalized_name") == _DRUG_A for r in result.retrieved), result.retrieved

    # INV-1a (citations): every surviving citation resolves to drug A -- zero leak.
    assert all(c.short_name == _DRUG_A_SHORT for c in result.citations), result.citations

    # INV-1b: refuse-or-cite. Either a refusal (allowed -- real cosine for the
    # query may fall under the 0.30 threshold), or a grounded answer that carries
    # BOTH prose and >=1 valid citation. Never an uncited non-refusal claim.
    if result.refused:
        assert result.citations == []
    else:
        assert result.citations, "non-refused answer must carry >=1 citation (INV-1)"
        assert result.answer.strip(), "non-refused answer must carry prose (INV-1)"
        # The cited answer's own short_name must be the one we retrieved.
        assert {c.short_name for c in result.citations} == {_DRUG_A_SHORT}
