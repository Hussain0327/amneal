"""Preferred vs deprecated env names for the embedding config knobs.

INGEST_EMBEDDING_PROVIDER is the preferred name for what EMBEDDING_PROVIDER
configured (the ingest/backfill WRITE-path provider) and
RETRIEVAL_EMBEDDING_PROFILE for ACTIVE_EMBEDDING_PROFILE (the query-path
profile). The old names keep working unchanged -- prod Fly secrets still use
them -- with a one-line FutureWarning nudge; when both are set and disagree
the NEW name wins. Resolution is AliasChoices on the Settings fields; the
warning policy is Settings._warn_deprecated_env_names.

Each test states its own environment explicitly via _configure.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import config.settings as cs
import pytest

# Expected message prefixes, exact by construction so that assertions about
# one knob can never accidentally match the other (plain substring matching
# would: "EMBEDDING_PROVIDER" is a substring of "INGEST_EMBEDDING_PROVIDER").
_DEPRECATED_PROVIDER = "EMBEDDING_PROVIDER is deprecated; set INGEST_EMBEDDING_PROVIDER"
_CONFLICT_PROVIDER = "Both INGEST_EMBEDDING_PROVIDER and EMBEDDING_PROVIDER are set and disagree"
_DEPRECATED_PROFILE = "ACTIVE_EMBEDDING_PROFILE is deprecated; set RETRIEVAL_EMBEDDING_PROFILE"
_CONFLICT_PROFILE = (
    "Both RETRIEVAL_EMBEDDING_PROFILE and ACTIVE_EMBEDDING_PROFILE are set and disagree"
)


def _configure(monkeypatch: pytest.MonkeyPatch, unset: tuple[str, ...] = (), **env: str) -> None:
    for name in unset:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _fresh_settings() -> tuple[cs.Settings, list[str]]:
    """Construct Settings, capturing every warning message it emits.

    simplefilter("always") defeats the once-per-location dedup that the
    default filter applies in production, so each test sees the warnings its
    own construction produced.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = cs.Settings(_env_file=None)  # type: ignore[call-arg]
    return s, [str(w.message) for w in caught]


def _matching(messages: list[str], prefix: str) -> list[str]:
    return [m for m in messages if m.startswith(prefix)]


# ---------- INGEST_EMBEDDING_PROVIDER / EMBEDDING_PROVIDER ----------


def test_new_ingest_provider_name_resolves_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, unset=("EMBEDDING_PROVIDER",), INGEST_EMBEDDING_PROVIDER="echo")
    s, messages = _fresh_settings()
    assert s.embedding_provider == "echo"
    assert not _matching(messages, _DEPRECATED_PROVIDER)
    assert not _matching(messages, _CONFLICT_PROVIDER)


def test_old_ingest_provider_name_still_works_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _configure(monkeypatch, unset=("INGEST_EMBEDDING_PROVIDER",), EMBEDDING_PROVIDER="echo")
    s, messages = _fresh_settings()
    assert s.embedding_provider == "echo"
    assert len(_matching(messages, _DEPRECATED_PROVIDER)) == 1


def test_ingest_provider_conflict_new_name_wins_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, INGEST_EMBEDDING_PROVIDER="echo", EMBEDDING_PROVIDER="qwen3")
    s, messages = _fresh_settings()
    assert s.embedding_provider == "echo"
    assert len(_matching(messages, _CONFLICT_PROVIDER)) == 1
    assert not _matching(messages, _DEPRECATED_PROVIDER)


def test_ingest_provider_agreement_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # The safe transition state a deployment passes through while renaming
    # secrets: both names set to the same value must not nag on every boot.
    _configure(monkeypatch, INGEST_EMBEDDING_PROVIDER="echo", EMBEDDING_PROVIDER="echo")
    s, messages = _fresh_settings()
    assert s.embedding_provider == "echo"
    assert not _matching(messages, _DEPRECATED_PROVIDER)
    assert not _matching(messages, _CONFLICT_PROVIDER)


def test_blank_old_provider_name_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CI templating renders an unconfigured var as "", which must behave as
    # unset for the value (the #247 posture) AND for the warning.
    _configure(monkeypatch, unset=("INGEST_EMBEDDING_PROVIDER",), EMBEDDING_PROVIDER="")
    s, messages = _fresh_settings()
    assert s.embedding_provider is None
    assert not _matching(messages, _DEPRECATED_PROVIDER)


# ---------- RETRIEVAL_EMBEDDING_PROFILE / ACTIVE_EMBEDDING_PROFILE ----------


def test_new_retrieval_profile_name_resolves_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        unset=("ACTIVE_EMBEDDING_PROFILE",),
        RETRIEVAL_EMBEDDING_PROFILE="ep_test",
    )
    s, messages = _fresh_settings()
    assert s.active_embedding_profile == "ep_test"
    assert not _matching(messages, _DEPRECATED_PROFILE)
    assert not _matching(messages, _CONFLICT_PROFILE)


def test_old_retrieval_profile_name_still_works_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _configure(
        monkeypatch,
        unset=("RETRIEVAL_EMBEDDING_PROFILE",),
        ACTIVE_EMBEDDING_PROFILE="legacy",
    )
    s, messages = _fresh_settings()
    assert s.active_embedding_profile == "legacy"
    assert len(_matching(messages, _DEPRECATED_PROFILE)) == 1


def test_retrieval_profile_conflict_new_name_wins_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        RETRIEVAL_EMBEDDING_PROFILE="ep_new",
        ACTIVE_EMBEDDING_PROFILE="legacy",
    )
    s, messages = _fresh_settings()
    assert s.active_embedding_profile == "ep_new"
    assert len(_matching(messages, _CONFLICT_PROFILE)) == 1
    assert not _matching(messages, _DEPRECATED_PROFILE)
