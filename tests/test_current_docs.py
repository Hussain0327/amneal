"""Guards the living documentation set against known stale claims.

Every string in this module is a claim that was true once, was corrected in a
documented change, and must not come back. When you fix a documentation defect
that a reader could plausibly reintroduce, add the wrong phrasing here.

`docs/DECISIONS.md` is deliberately exempt from the retired-runtime guard: it is
an append-only history whose older entries describe the world as it was.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CURRENT_DOCS = [
    "README.md",
    "docs/AUTHORITATIVE_FDA_CORPUS.md",
    "docs/ARCHITECTURE.md",
    "docs/CONVERSATIONAL_SESSIONS.md",
    "docs/PRODUCTION_TRUTH.md",
    "docs/ROADMAP.md",
]

AUTHORITATIVE_CORPUS_DOCS = [
    "README.md",
    "docs/AUTHORITATIVE_FDA_CORPUS.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOY.md",
    "docs/MAP.md",
    "docs/NON_TECH_GUIDE.md",
    "docs/PRODUCTION_TRUTH.md",
    "docs/ROADMAP.md",
    "docs/SECRETS_RUNBOOK.md",
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
    "docs/PRODUCTION_TRUTH.md",
    "docs/ROADMAP.md",
    "docs/SECRETS_RUNBOOK.md",
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

# Retired on 2026-08-26 by the documentation handoff pass. Each string names a
# provider, workflow, guard, or migration head that no longer exists. An
# occurrence in a living doc is a factual error, not a style problem.
#
# Sources: src/regwatch/generate/llm.py:732-733 (databricks raises),
# src/regwatch/process/embedder.py (no Qwen3 provider class),
# .github/workflows/ (no databricks-eval.yml), a repo-wide grep proving no D1
# guard exists, migrations/versions/ (head is past 0020), and
# src/regwatch/eval/run_eval.py:107-108 (citation_precision floor is 0.70).
RETIRED_RUNTIME_CLAIMS = [
    "databricks-eval.yml",
    "Qwen3EmbeddingProvider",
    "QWEN_EMBEDDING_BASE_URL",
    "QWEN_EMBEDDING_TOKEN",
    "WATCH_QWEN_EMBEDDING",
    "DATABRICKS_LLM_BASE_URL",
    "DATABRICKS_LLM_TOKEN",
    "DATABRICKS_SERVING_RUNTIME_VERSION",
    'LLM_PROVIDER="databricks"',
    "EMBEDDING_PROVIDER=qwen3",
    'EMBEDDING_PROVIDER = "qwen3"',
    "0020_eval_run",
    "citation_precision >= 0.74",
    "citation_precision` 0.74",
]

# A living doc MAY name a retired identifier in order to say it is gone: the
# residency guard names are discussed in docs/BUILT_BUT_DORMANT.md precisely
# because six documents once claimed the guard shipped. What must never come
# back is the affirmative claim, or the rollback command that deepens an outage.
RETIRED_AFFIRMATIVE_CLAIMS = [
    "The guard ships and is tested",
    "no armed guard was bypassed",
    "fly secrets set LLM_PROVIDER=databricks",
]

RETIRED_RUNTIME_EXEMPT = {"docs/DECISIONS.md"}


def _living_docs() -> list[str]:
    """Returns every living markdown doc that must describe today's runtime."""
    paths = ["README.md", "SECURITY.md"]
    paths.extend(sorted(f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md")))
    return [p for p in paths if p not in RETIRED_RUNTIME_EXEMPT]


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


def test_living_docs_do_not_describe_the_retired_runtime() -> None:
    """No living doc may name a provider, workflow, or guard that is gone."""
    offenders: list[str] = []
    for doc_path in _living_docs():
        path = ROOT / doc_path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for stale in RETIRED_RUNTIME_CLAIMS + RETIRED_AFFIRMATIVE_CLAIMS:
            if stale in text:
                offenders.append(f"{doc_path}: {stale!r}")

    assert not offenders, "living docs name a retired runtime:\n" + "\n".join(offenders)


def test_discovery_denominator_is_not_claimed_as_chunks_or_embeddings() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/AUTHORITATIVE_FDA_CORPUS.md").read_text(encoding="utf-8")
    normalized_runbook = " ".join(runbook.split())

    assert "140,438 source records" in readme
    assert "Those are source records, not chunks or embeddings" in readme
    assert "140,438 frozen source records" in normalized_runbook
    for false_claim in ("140,438 chunks", "140438 chunks", "140,438 embeddings"):
        assert false_claim not in readme
        assert false_claim not in normalized_runbook


def test_corpus_runbook_documents_the_scoped_activation_amendment() -> None:
    """The complete-universe target is unreachable; the runbook must say so.

    `config/settings.py` records that the full 140,438-document universe became
    unreachable under the Lakebase branch size cap on 2026-08-18, and that
    activation now counts against a named manifest instead. A runbook that still
    presents complete-universe coverage as the only acceptance path sends an
    operator after a target that cannot be met.
    """
    runbook = (ROOT / "docs/AUTHORITATIVE_FDA_CORPUS.md").read_text(encoding="utf-8")

    assert "REGWATCH_SERVING_MANIFEST_SHA" in runbook


def test_every_doc_link_resolves() -> None:
    """A relative link in a living doc must point at a file that exists."""
    link_re = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    broken: list[str] = []
    for doc_path in [*_living_docs(), "docs/DECISIONS.md"]:
        path = ROOT / doc_path
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for target in link_re.findall(line):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                if not resolved.exists():
                    broken.append(f"{doc_path}:{lineno} -> {target}")

    assert not broken, "broken relative links:\n" + "\n".join(broken)
