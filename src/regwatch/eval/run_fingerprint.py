"""Provenance for an eval run.

An eval scorecard is only comparable to another scorecard when you can prove
both were produced from the same corpus, the same retrieval configuration and
the same code. A bare score is not evidence: the 2026-07-31 legacy-vs-qwen3
comparison could not be audited afterwards because the artifact recorded no
profile, no corpus state, no reranker setting and no commit.

Everything here is read-only and cheap enough to run before every eval.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text as sa_text

LEGACY = "legacy"


@dataclass
class CorpusDigest:
    """Identity of the corpus the eval ran against.

    ``digest`` is an order-independent hash of the chunk ids, so two runs over
    the same rows agree even if the re-chunk rewrote them in a different order,
    while any add/drop/rekey changes it.
    """

    chunks: int = 0
    docs: int = 0
    digest: str = ""


@dataclass
class RunFingerprint:
    profile: str = LEGACY
    profile_detail: dict[str, Any] = field(default_factory=dict)
    corpus: CorpusDigest = field(default_factory=CorpusDigest)
    retrieval: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    commit: str = "unknown"
    dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git(*args: str) -> str:
    try:
        # S603/S607: fixed argv, no shell, and every element is a literal from
        # this module -- nothing here is caller- or user-supplied. Resolving git
        # by PATH is deliberate: an absolute path would not survive the
        # difference between a developer machine and the CI image.
        out = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_state() -> tuple[str, bool]:
    """(commit sha, working tree dirty). Never raises: provenance must not be
    able to fail an eval, it just degrades to 'unknown'."""
    sha = _git("rev-parse", "HEAD") or "unknown"
    # --porcelain lists tracked modifications; untracked files do not change
    # what the eval executed, so they are deliberately not counted as dirty.
    return sha, bool(_git("status", "--porcelain", "--untracked-files=no"))


def corpus_digest() -> CorpusDigest:
    from regwatch.store.db import get_engine

    sql = sa_text(
        "SELECT count(*) AS chunks, count(DISTINCT doc_id) AS docs, "
        "md5(string_agg(id, ',' ORDER BY id)) AS digest FROM chunk"
    )
    with get_engine().connect() as conn:
        row = conn.execute(sql).mappings().one()
    return CorpusDigest(
        chunks=int(row["chunks"] or 0),
        docs=int(row["docs"] or 0),
        digest=str(row["digest"] or ""),
    )


def profile_detail(profile_id: str) -> dict[str, Any]:
    """Geometry-defining fields of the active embedding configuration.

    For a registered profile these come from the immutable profile row; for the
    legacy column there is no row, so they come from settings -- which is the
    honest answer, and is exactly why the legacy arm is harder to pin down.
    """
    from config.settings import get_settings

    s = get_settings()
    if profile_id == LEGACY:
        return {
            "source": "settings (legacy chunk.embedding column)",
            "provider": s.embedding_provider,
            "model": getattr(s, "embedding_model", "") or "",
        }
    from regwatch.store.embedding_profiles import (
        get_embedding_profile,
        profile_embedding_coverage,
    )

    profile = get_embedding_profile(profile_id)
    coverage = profile_embedding_coverage(profile_id)
    return {
        "source": "embedding_profile row",
        "profile_id": getattr(profile, "profile_id", profile_id),
        "provider": getattr(profile, "provider", ""),
        "model": getattr(profile, "model", ""),
        "dimension": getattr(profile, "dimension", None),
        "revision": getattr(profile, "revision", ""),
        "query_instruction_version": getattr(profile, "query_instruction_version", ""),
        "preprocessing_version": getattr(profile, "preprocessing_version", ""),
        "chunking_recipe": getattr(profile, "chunking_recipe", ""),
        "coverage_complete": bool(coverage.complete),
        "embedded_chunks": int(coverage.embedded_chunks),
        "pending_chunks": int(coverage.pending_chunks),
        "index_ready": bool(getattr(coverage, "index_ready", False)),
    }


def build(profile_id: str, thresholds: dict[str, float]) -> RunFingerprint:
    from config.settings import get_settings

    s = get_settings()
    sha, dirty = git_state()
    return RunFingerprint(
        profile=profile_id,
        profile_detail=profile_detail(profile_id),
        corpus=corpus_digest(),
        retrieval={
            # The wide net and the cited set are different numbers; recording
            # only one of them makes a scorecard impossible to reproduce.
            "vector_top_k": s.vector_top_k,
            "rerank_top_k": s.effective_rerank_top_k,
            "reranker_enabled": s.reranker_enabled,
            "refusal_score_threshold": s.refusal_score_threshold,
        },
        models={
            "llm_provider": s.llm_provider,
            "llm_model": getattr(s, "databricks_llm_model", None) or "unconfigured",
        },
        thresholds=dict(thresholds),
        commit=sha,
        dirty=dirty,
    )
