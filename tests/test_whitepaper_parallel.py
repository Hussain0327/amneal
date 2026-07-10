"""Whitepaper populator concurrency + the overall build deadline.

The five fetch stages run as two parallel batches (orange_book || drugsfda,
then dailymed || ndc || psg-store after identity) under an overall
WHITEPAPER_BUILD_TIMEOUT_S deadline. These tests pin:

- real concurrency: stage wall-clock intervals overlap, and the whole build
  finishes in strictly less than the serial sum of the injected sleeps;
- ordering: no batch-B fetch starts before BOTH batch-A fetches completed
  (identity resolution sits between the batches on the caller's thread);
- degrade parity: a failing source collapses the same cells with the same
  warnings as the sequential build did, and when two sources fail in the same
  batch the warning ORDER is canonical (stage order, never thread-finish
  order);
- the deadline breach: a typed ``WhitepaperBuildTimeoutError``, one audited
  mode="whitepaper" status="error" row with reason="build_deadline_exceeded",
  a prompt return (the stalled fetch is abandoned, not joined), a 504 from
  POST /whitepaper, and no durable run row;
- the post-fetch gates: the lazy REMS index fetch is bounded by the remaining
  deadline and the nested PSG ask() never STARTS past it -- both degrade their
  cells to analyst input while the completed paper survives;
- /resolve shares the fetch-phase deadline (504, no audit row).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from regwatch.sources import dailymed, orange_book
from regwatch.store import whitepaper_runs as run_store
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from regwatch.whitepaper import populator
from regwatch.whitepaper.populator import (
    WhitepaperBuildTimeoutError,
    build_whitepaper,
)
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources


def _cells(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for s in result["sections"] for c in s["cells"]}


def _whitepaper_audit_rows() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(QueryLog).where(QueryLog.mode == "whitepaper"))
        return [
            {"status": r.status, "refused": r.refused, "route_json": dict(r.route_json)}
            for r in rows
        ]


def _set_build_timeout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Override WHITEPAPER_BUILD_TIMEOUT_S for this test.

    The autouse ``_isolate_env`` fixture cache-clears settings at the START of
    every test, so the override cannot outlive this test.
    """
    import config.settings as cs

    monkeypatch.setenv("WHITEPAPER_BUILD_TIMEOUT_S", value)
    cs.get_settings.cache_clear()


# --------------------------- concurrency ---------------------------
def test_independent_fetches_overlap_and_beat_serial_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sources(monkeypatch)
    sleep_s = 0.6
    spans: dict[str, tuple[float, float]] = {}

    def slow(name: str, module: Any, attr: str) -> None:
        inner = getattr(module, attr)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            time.sleep(sleep_s)
            try:
                return inner(*args, **kwargs)
            finally:
                spans[name] = (start, time.monotonic())

        monkeypatch.setattr(module, attr, wrapper)

    # One sleeping seam per stage in each batch (OB sleeps once, in its first
    # row fetch; patents/exclusivity stay fast inside the same stage thread).
    slow("orange_book", orange_book, "product_rows")
    slow("drugsfda", populator, "_drugsfda_records")
    slow("dailymed", dailymed, "resolve_setid")
    slow("ndc", populator, "_ndc_records")

    start = time.monotonic()
    result = build_whitepaper(RLD_NAME, APPL_NO)
    elapsed = time.monotonic() - start

    # Strictly less than the serial sum of the sleeps alone: a sequential run
    # could never finish under 4 * sleep_s even with zero overhead.
    assert elapsed < 4 * sleep_s, f"build took {elapsed:.2f}s; not concurrent"

    def overlap(a: str, b: str) -> bool:
        return spans[a][0] < spans[b][1] and spans[b][0] < spans[a][1]

    assert overlap("orange_book", "drugsfda"), "batch A stages did not overlap"
    assert overlap("dailymed", "ndc"), "batch B stages did not overlap"
    # The parallel build still produced the full sequential payload.
    cells = _cells(result)
    assert result["spine"]["application_number"] == APPL_NO
    assert cells["product_name"]["status"] == "populated"
    assert cells["packaging"]["status"] == "populated"
    assert cells["indication"]["status"] == "populated"


def test_stage_b_never_starts_before_batch_a_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity resolution runs between the batches, so every batch-B fetch
    must observe BOTH batch-A fetches finished (and the dailymed query must
    carry the post-identity resolved, prefixed application number)."""
    install_fake_sources(monkeypatch)
    completed: list[str] = []
    seen_at_start: dict[str, set[str]] = {}
    dailymed_appl: list[str] = []

    def finishing(name: str, module: Any, attr: str) -> None:
        inner = getattr(module, attr)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            time.sleep(0.3)  # keep batch A genuinely in flight while checked
            out = inner(*args, **kwargs)
            completed.append(name)
            return out

        monkeypatch.setattr(module, attr, wrapper)

    def snapshotting(name: str, module: Any, attr: str) -> None:
        inner = getattr(module, attr)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            seen_at_start[name] = set(completed)
            return inner(*args, **kwargs)

        monkeypatch.setattr(module, attr, wrapper)

    finishing("orange_book", orange_book, "product_rows")
    finishing("drugsfda", populator, "_drugsfda_records")
    snapshotting("ndc", populator, "_ndc_records")
    snapshotting("psg", populator, "_matching_psg_docs")

    real_resolve = dailymed.resolve_setid

    def spying_resolve(application_number: str, **kwargs: Any) -> Any:
        seen_at_start["dailymed"] = set(completed)
        dailymed_appl.append(application_number)
        return real_resolve(application_number, **kwargs)

    monkeypatch.setattr(dailymed, "resolve_setid", spying_resolve)

    # Bare digits: the prefixed number handed to DailyMed can only come from
    # the identity step between the batches.
    build_whitepaper(RLD_NAME, APPL_NO)

    for name in ("dailymed", "ndc", "psg"):
        assert seen_at_start[name] == {"orange_book", "drugsfda"}, (
            f"batch B stage {name!r} started before batch A completed: " f"{seen_at_start[name]}"
        )
    assert dailymed_appl == [f"NDA{APPL_NO}"]


# --------------------------- degrade parity ---------------------------
def test_single_source_failure_degrades_like_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing DailyMed resolve collapses exactly the SPL cells with the
    sequential build's warning text; unrelated sources stay populated."""
    install_fake_sources(monkeypatch)

    def failing_resolve(application_number: str, **kwargs: Any) -> Any:
        raise RuntimeError("dailymed down")

    monkeypatch.setattr(dailymed, "resolve_setid", failing_resolve)
    result = build_whitepaper(RLD_NAME, APPL_NO)
    cells = _cells(result)
    for cell_id in ("indication", "pllr_format", "plr_format", "pregnancy_registry"):
        assert cells[cell_id]["status"] == "analyst_input_required", cell_id
    assert "DailyMed setid resolution failed (RuntimeError)." in result["spine"]["warnings"]
    # The other batch-B sources are untouched by the failure.
    assert cells["product_name"]["status"] == "populated"
    assert cells["packaging"]["status"] == "populated"


def test_same_batch_double_failure_warning_order_is_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NDC fails instantly, DailyMed fails 0.4s later -- thread-finish order is
    ndc-then-dailymed, but the merged warnings must keep the sequential
    build's stage order (dailymed before ndc)."""
    install_fake_sources(monkeypatch)

    def slow_failing_resolve(application_number: str, **kwargs: Any) -> Any:
        time.sleep(0.4)
        raise RuntimeError("dailymed down")

    def fast_failing_ndc(query: Any) -> Any:
        raise RuntimeError("ndc down")

    monkeypatch.setattr(dailymed, "resolve_setid", slow_failing_resolve)
    monkeypatch.setattr(populator, "_ndc_records", fast_failing_ndc)

    result = build_whitepaper(RLD_NAME, APPL_NO)
    warnings = result["spine"]["warnings"]
    dm = warnings.index("DailyMed setid resolution failed (RuntimeError).")
    ndc = warnings.index("NDC Directory fetch failed (RuntimeError).")
    assert dm < ndc, f"warning order not canonical: {warnings}"
    cells = _cells(result)
    assert cells["packaging"]["status"] == "analyst_input_required"
    assert cells["indication"]["status"] == "analyst_input_required"


# --------------------------- deadline ---------------------------
def test_deadline_breach_raises_promptly_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sources(monkeypatch)
    stall_s = 1.5

    def stalled_products(application_number: str, *, client: Any = None) -> Any:
        time.sleep(stall_s)
        # Raise, never return: the breach abandons this thread, and by the time
        # the sleep ends this test is over and monkeypatch has restored the REAL
        # patent/exclusivity fetchers -- a normal return would let the orphan
        # continue the stage into live network calls. The stage's own except
        # swallows this on its dead context copy and the thread exits cleanly.
        raise RuntimeError("orphan stage thread stopped after deadline breach")

    monkeypatch.setattr(orange_book, "product_rows", stalled_products)
    _set_build_timeout(monkeypatch, "0.2")

    start = time.monotonic()
    with pytest.raises(WhitepaperBuildTimeoutError) as exc_info:
        build_whitepaper(RLD_NAME, APPL_NO)
    elapsed = time.monotonic() - start

    assert "deadline" in exc_info.value.detail
    # The stalled fetch was abandoned, not joined: a shutdown(wait=True)
    # regression would hold the caller for the full stall.
    assert elapsed < stall_s - 0.2, f"breach took {elapsed:.2f}s; stalled fetch was joined"

    rows = _whitepaper_audit_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["refused"] is True
    assert rows[0]["route_json"]["reason"] == "build_deadline_exceeded"
    assert rows[0]["route_json"]["error_type"] == "WhitepaperBuildTimeoutError"


def test_zero_timeout_disables_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    _set_build_timeout(monkeypatch, "0")
    result = build_whitepaper(RLD_NAME, APPL_NO)
    assert _cells(result)["product_name"]["status"] == "populated"


def test_whitepaper_deadline_maps_to_504_with_no_run_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)

    def stalled_resolve(application_number: str, **kwargs: Any) -> Any:
        time.sleep(1.5)
        # Same orphan-thread rule as stalled_products above: never return into
        # a post-teardown stage.
        raise RuntimeError("orphan stage thread stopped after deadline breach")

    monkeypatch.setattr(dailymed, "resolve_setid", stalled_resolve)
    _set_build_timeout(monkeypatch, "0.35")

    r = auth_client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 504
    assert "deadline" in r.json()["detail"]
    # The breach is audited but never persisted as a durable run.
    rows = _whitepaper_audit_rows()
    assert [row["route_json"]["reason"] for row in rows] == ["build_deadline_exceeded"]
    _, total = run_store.list_runs(limit=10, offset=0)
    assert total == 0


def test_resolve_deadline_maps_to_504_with_no_audit_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/resolve shares the fetch-phase deadline: a breach is a 504, and the
    surface stays audit-free (it writes no row on success or failure)."""
    install_fake_sources(monkeypatch)

    def stalled_resolve(application_number: str, **kwargs: Any) -> Any:
        time.sleep(1.5)
        # Same orphan-thread rule as stalled_products above.
        raise RuntimeError("orphan stage thread stopped after deadline breach")

    monkeypatch.setattr(dailymed, "resolve_setid", stalled_resolve)
    _set_build_timeout(monkeypatch, "0.35")

    r = auth_client.post("/resolve", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 504
    assert "deadline" in r.json()["detail"]
    assert _whitepaper_audit_rows() == []


# --------------------------- post-fetch deadline gates ---------------------------
def test_rems_fetch_bounded_by_remaining_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REMS index stall during CELL build is abandoned at the deadline: both
    REMS cells degrade like a failed source (ONE fetch attempt total -- the
    breach is cached, never re-fetched), the rest of the paper survives, and
    the run audits as a normal populated build."""
    install_fake_sources(monkeypatch)
    stall_s = 6.0
    calls: list[float] = []

    def stalled_rems_search(query: Any) -> Any:
        calls.append(time.monotonic())
        time.sleep(stall_s)
        # Same orphan-thread rule as stalled_products above: never return into
        # a post-teardown fake.
        raise RuntimeError("orphan rems thread stopped after deadline breach")

    monkeypatch.setattr(populator, "_rems_search", stalled_rems_search)
    _set_build_timeout(monkeypatch, "1.5")

    start = time.monotonic()
    result = build_whitepaper(RLD_NAME, APPL_NO)
    elapsed = time.monotonic() - start

    # Abandoned, not joined: joining the stalled fetch would hold ~stall_s.
    assert elapsed < stall_s - 2.0, f"build took {elapsed:.2f}s; REMS fetch was joined"
    assert len(calls) == 1, "second REMS cell re-fetched instead of reusing the cached breach"
    cells = _cells(result)
    assert cells["rems"]["status"] == "analyst_input_required"
    assert "WhitepaperBuildTimeoutError" in (cells["rems"]["note"] or "")
    assert "WhitepaperBuildTimeoutError" in (cells["restricted_distribution"]["note"] or "")
    # The paper itself completed: fetch-phase cells kept, run audited populated.
    assert cells["product_name"]["status"] == "populated"
    rows = _whitepaper_audit_rows()
    assert [row["route_json"]["reason"] for row in rows] == ["populated"]


def test_post_fetch_gates_skip_rems_and_psg_ask_when_deadline_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deadline fully spent AFTER the fetch batches passed: the lazy REMS
    fetch and the nested PSG ask are never STARTED, their cells degrade to
    analyst input, and the completed paper still audits as populated."""
    install_fake_sources(monkeypatch)

    def never_rems(query: Any) -> Any:
        raise AssertionError("REMS index fetch must not start past the deadline")

    def never_ask(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("scoped PSG ask must not start past the deadline")

    monkeypatch.setattr(populator, "_rems_search", never_rems)
    monkeypatch.setattr(populator, "ask", never_ask)

    real_tokens = populator._build_known_tokens

    def slow_tokens(ctx: Any) -> None:
        time.sleep(0.8)  # spend the whole deadline between batches and cells
        real_tokens(ctx)

    monkeypatch.setattr(populator, "_build_known_tokens", slow_tokens)
    _set_build_timeout(monkeypatch, "0.4")

    result = build_whitepaper(RLD_NAME, APPL_NO)
    cells = _cells(result)
    # Fetch phase passed in time: its cells are intact.
    assert cells["product_name"]["status"] == "populated"
    assert cells["rems"]["status"] == "analyst_input_required"
    assert "WhitepaperBuildTimeoutError" in (cells["rems"]["note"] or "")
    # Cell id "requirements" = the PSG-RAG cell (extractor "psg_requirements").
    assert cells["requirements"]["status"] == "analyst_input_required"
    assert "deadline" in (cells["requirements"]["note"] or "")
    rows = _whitepaper_audit_rows()
    assert [row["route_json"]["reason"] for row in rows] == ["populated"]
