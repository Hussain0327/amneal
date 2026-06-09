"""Shared test fixtures.

We force a per-test temp data directory so that SQLite, Chroma, and raw PDFs
are isolated. We default LLM_PROVIDER=echo and EMBEDDING_PROVIDER=echo so
tests run with no network and no model downloads.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from regwatch.store import db as db_module
from regwatch.store import vector_store as vs_module


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Network-free providers by default.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    monkeypatch.setenv("LLM_PROVIDER", "echo")
    # Pydantic-settings would otherwise load real keys from `.env`; clear them
    # so tests run from a clean slate regardless of the host's .env.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENFDA_API_KEY", "")
    # Per-test storage.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "regwatch.db"))
    monkeypatch.setenv("RAW_PDF_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("PROCESSED_DIR", str(tmp_path / "processed"))
    # Force settings + storage modules to re-init for the test.
    import config.settings as cs

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    vs_module.reset_for_tests()
    # Also clear any cached env that pydantic-settings stashed in process.
    yield
    # Cleanup (Chroma keeps a file lock; reset clears it).
    vs_module.reset_for_tests()
    db_module.reset_for_tests()


@pytest.fixture
def cleared_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fixture to wipe optional API keys so tests can assert provider errors."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENFDA_API_KEY"):
        if k in os.environ:
            monkeypatch.delenv(k, raising=False)
