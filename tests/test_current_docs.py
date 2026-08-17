from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CURRENT_DOCS = [
    "README.md",
    "docs/AUTHORITATIVE_FDA_CORPUS.md",
    "docs/ARCHITECTURE.md",
    "docs/CONVERSATIONAL_SESSIONS.md",
    "docs/PROD_READINESS.md",
    "docs/ROADMAP.md",
    "docs/TECH_GUIDE_SIMPLE.md",
]

AUTHORITATIVE_CORPUS_DOCS = [
    "README.md",
    "docs/AUTHORITATIVE_FDA_CORPUS.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOY.md",
    "docs/MAP.md",
    "docs/NON_TECH_GUIDE.md",
    "docs/PROD_READINESS.md",
    "docs/PROJECT_SPEC.md",
    "docs/ROADMAP.md",
    "docs/SECRETS_RUNBOOK.md",
    "docs/TECH_GUIDE_SIMPLE.md",
    "docs/whitepaper_schema.md",
]

RETIRED_SOURCE_RUNTIME_CLAIMS = [
    "api.fda.gov",
    "download.open.fda.gov",
    "OPENFDA_API_KEY",
    "openFDA",
    "OpenFDA",
    "dailymed.nlm.nih.gov",
]

STALE_CLAIMS = [
    "no `/query/stream` endpoint",
    "backend has no `/query/stream` endpoint",
    "backend does not implement",
    "nothing actually streams today",
    "Every endpoint except `GET /health`",
    "`GET /health` — the only unauthenticated endpoint",
    "Fly/Vercel deploy keeps Watch out of scope",
    "ad-hoc runs",
    "Needs a supported scheduled worker with monitored run history",
    "the `alerted_at` / durable-diff residual",
    "production Watch/Dagster worker deployment",
]

WATCH_CURRENT_DOCS = [
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/CI_CD.md",
    "docs/DEPLOY.md",
    "docs/PROD_READINESS.md",
    "docs/ROADMAP.md",
    "docs/SECRETS_RUNBOOK.md",
    "docs/TECH_GUIDE_SIMPLE.md",
]

STALE_WATCH_PROFILE_CLAIMS = [
    "does not have the embedding-profile secrets",
    "still hardcodes `EMBEDDING_PROVIDER: openai`",
    'still hardcodes `EMBEDDING_PROVIDER: "openai"`',
    "does not map a dimension",
    "never maps `QWEN_EMBEDDING_DIMENSION`",
    "five `WATCH_*` embedding-profile secrets",
    "four `WATCH_QWEN_EMBEDDING_*`",
    "LEGACY OpenAI-1536 arm",
    "profile block in `watch-daily.yml` is inert",
]


@pytest.mark.parametrize("doc_path", CURRENT_DOCS)
def test_current_docs_do_not_reintroduce_stale_runtime_claims(doc_path: str) -> None:
    text = (ROOT / doc_path).read_text(encoding="utf-8")

    for stale in STALE_CLAIMS:
        assert stale not in text, f"{doc_path} contains stale claim: {stale!r}"


@pytest.mark.parametrize("doc_path", WATCH_CURRENT_DOCS)
def test_current_docs_do_not_reintroduce_stale_watch_profile_claims(
    doc_path: str,
) -> None:
    text = (ROOT / doc_path).read_text(encoding="utf-8")

    for stale in STALE_WATCH_PROFILE_CLAIMS:
        assert stale not in text, f"{doc_path} contains stale Watch claim: {stale!r}"


@pytest.mark.parametrize("doc_path", AUTHORITATIVE_CORPUS_DOCS)
def test_current_corpus_docs_do_not_reintroduce_retired_source_paths(doc_path: str) -> None:
    text = (ROOT / doc_path).read_text(encoding="utf-8")

    for stale in RETIRED_SOURCE_RUNTIME_CLAIMS:
        assert stale not in text, f"{doc_path} contains retired source claim: {stale!r}"


def test_discovery_denominator_is_not_claimed_as_chunks_or_embeddings() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/AUTHORITATIVE_FDA_CORPUS.md").read_text(encoding="utf-8")
    normalized_runbook = " ".join(runbook.split())

    assert "140,438 source records" in readme
    assert "Those are source records, not chunks or embeddings" in readme
    assert "140,438 frozen source records" in normalized_runbook
    assert "final resolved, chunk, and embedding totals pending" in normalized_runbook
    for false_claim in ("140,438 chunks", "140438 chunks", "140,438 embeddings"):
        assert false_claim not in readme
        assert false_claim not in normalized_runbook
