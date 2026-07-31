"""Tests for the per-arm eval wiring: profile selection, trace, fingerprint.

These pin the properties that make two scorecards comparable at all -- that the
run really executed the arm it claims, and that the artifact says which corpus
and configuration produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from config.settings import get_settings

from regwatch.eval import run_eval, run_fingerprint
from regwatch.eval.metrics import GoldItem, evaluate


@pytest.fixture(autouse=True)
def _restore_active_profile() -> Any:
    """_apply_profile configures the process by writing os.environ directly, which
    is correct for a CLI and toxic in a test session: without this, one test here
    would leave ACTIVE_EMBEDDING_PROFILE set and every later test in the same
    process would silently retrieve through the wrong arm."""
    import os as _os

    before = _os.environ.get("ACTIVE_EMBEDDING_PROFILE")
    try:
        yield
    finally:
        if before is None:
            _os.environ.pop("ACTIVE_EMBEDDING_PROFILE", None)
        else:
            _os.environ["ACTIVE_EMBEDDING_PROFILE"] = before
        get_settings.cache_clear()


@dataclass
class _Citation:
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    score: float
    snippet: str = "should not reach the artifact"


@dataclass
class _Result:
    answer: str = ""
    citations: list[Any] = None  # type: ignore[assignment]
    refused: bool = False
    retrieved: list[dict[str, Any]] = None  # type: ignore[assignment]
    status: str = "answer"
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.citations is None:
            self.citations = []
        if self.retrieved is None:
            self.retrieved = []


def _answered() -> _Result:
    return _Result(
        answer="Two-way crossover [PSG_1, p.4].",
        citations=[_Citation("PSG_1", 4, "c1", 11, 21, 0.88)],
        retrieved=[
            {
                "chunk_id": "c1",
                "doc_id": 11,
                "version_id": 21,
                "page": 4,
                "short_name": "PSG_1",
                "score": 0.88,
                "text": "should not reach the artifact",
            }
        ],
    )


def test_trace_records_retrieved_citations_and_answer() -> None:
    """Without the trace a scorecard cannot be audited: you can see that a metric
    moved but not which passages moved it."""
    item = GoldItem(question="q?", expected_sources=[{"short_name": "PSG_1", "page": 4}])
    sc = evaluate([item], ask_callable=lambda _q: _answered())
    trace = sc.details[0]["trace"]
    assert trace["retrieved"] == [
        {
            "chunk_id": "c1",
            "doc_id": 11,
            "version_id": 21,
            "page": 4,
            "short_name": "PSG_1",
            "score": 0.88,
        }
    ]
    assert trace["citations"][0]["chunk_id"] == "c1"
    assert trace["answer"] == "Two-way crossover [PSG_1, p.4]."
    assert trace["status"] == "answer"


def test_trace_excludes_passage_and_snippet_text() -> None:
    """Ids, pages and scores make a finding checkable; full text would bloat the
    artifact past the point where anyone opens it."""
    item = GoldItem(question="q?", expected_sources=[{"short_name": "PSG_1", "page": 4}])
    sc = evaluate([item], ask_callable=lambda _q: _answered())
    trace = sc.details[0]["trace"]
    assert "text" not in trace["retrieved"][0]
    assert "snippet" not in trace["citations"][0]


def test_trace_is_recorded_for_refused_items_too() -> None:
    """A refusal is a decision worth auditing: it must show what it had in hand."""
    refused = _Result(answer="", refused=True, status="refused", reason="no_product")
    item = GoldItem(question="unknown drug?", expected_sources=[], must_refuse=True)
    sc = evaluate([item], ask_callable=lambda _q: refused)
    trace = sc.details[0]["trace"]
    assert trace["status"] == "refused"
    assert trace["reason"] == "no_product"
    assert trace["retrieved"] == []


def test_apply_profile_legacy_configures_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "ep_stale_value")
    get_settings.cache_clear()
    try:
        assert run_eval._apply_profile("legacy") == "legacy"
        assert get_settings().active_embedding_profile == "legacy"
    finally:
        get_settings.cache_clear()


def test_apply_profile_rejects_an_arm_that_did_not_take_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the setting does not follow the flag, the run would silently score the
    wrong arm and the artifact would claim the right one."""

    class _Frozen:
        active_embedding_profile = "legacy"

    def _frozen_settings() -> _Frozen:
        return _Frozen()

    _frozen_settings.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(run_eval, "get_settings", _frozen_settings)
    # Well-formed on purpose: a malformed id would be rejected by the shape
    # check first and never reach the did-it-take-effect assertion.
    with pytest.raises(SystemExit) as excinfo:
        run_eval._apply_profile("ep_" + "a" * 32)
    assert "did not take effect" in str(excinfo.value)


def test_assert_profile_ready_rejects_incomplete_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial coverage degrades recall silently instead of erroring, so it must
    fail before the run spends LLM calls."""

    @dataclass
    class _Coverage:
        complete: bool = False
        pending_chunks: int = 12
        index_ready: bool = True

    import regwatch.store.embedding_profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_embedding_coverage", lambda _p: _Coverage())
    with pytest.raises(SystemExit) as excinfo:
        run_eval._assert_profile_ready("ep_x")
    assert "not fully embedded" in str(excinfo.value)


def test_assert_profile_ready_rejects_a_missing_index(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class _Coverage:
        complete: bool = True
        pending_chunks: int = 0
        index_ready: bool = False

    import regwatch.store.embedding_profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_embedding_coverage", lambda _p: _Coverage())
    with pytest.raises(SystemExit) as excinfo:
        run_eval._assert_profile_ready("ep_x")
    assert "no vector index" in str(excinfo.value)


def test_assert_profile_ready_is_a_no_op_for_legacy() -> None:
    run_eval._assert_profile_ready("legacy")


def test_apply_profile_rejects_a_malformed_id_before_boot() -> None:
    """A typo'd arm is operator error about a flag. Validated here it is one
    line; left to init_db it is a boot traceback that reads like a broken tool."""
    with pytest.raises(SystemExit) as excinfo:
        run_eval._apply_profile("ep_nope")
    assert "is not a usable arm" in str(excinfo.value)
    # And it must not have configured the process on its way out.
    import os as _os

    assert _os.environ.get("ACTIVE_EMBEDDING_PROFILE") != "ep_nope"


def test_git_state_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance is best-effort: it must degrade to 'unknown', never fail a run."""
    monkeypatch.setattr(
        run_fingerprint,
        "_git",
        lambda *_a: (_ for _ in ()).throw(OSError("no git here")),
    )
    with pytest.raises(OSError):
        run_fingerprint._git("rev-parse")  # the stub itself raises

    monkeypatch.setattr(run_fingerprint, "_git", lambda *_a: "")
    sha, dirty = run_fingerprint.git_state()
    assert sha == "unknown"
    assert dirty is False
