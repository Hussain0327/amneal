"""Host-global FDA request-start pacing across worker processes.

The in-process pacing lock is module state, so N concurrent crawler processes
each enforcing crawl_min_interval_ms multiply FDA request pressure by N. With
crawl_pace_dir set, every process serializes request starts through one
flock-guarded timestamp per host, making the interval a true host-wide budget.
These tests drive the REAL _pace_request from multiple spawned processes and
assert on observed start times -- no mocks around the locking itself.
"""

from __future__ import annotations

import itertools
import multiprocessing
import time
from pathlib import Path

import pytest

INTERVAL_S = 0.30
# Slack for scheduler jitter: starts must be spaced by the interval minus a
# small tolerance, never by "roughly zero", which is what a broken limiter
# yields when two processes race.
TOLERANCE_S = 0.05


def _paced_worker(pace_dir: str, out_path: str, starts: int) -> None:
    """Record time.time() after each paced request start. Runs in a child."""
    import os

    os.environ["CRAWL_PACE_DIR"] = pace_dir
    import config.settings as cs

    cs.get_settings.cache_clear()
    from regwatch.sources import http as http_mod

    stamps: list[float] = []
    for _ in range(starts):
        http_mod._pace_request("https://www.accessdata.fda.gov/x.pdf", INTERVAL_S)
        stamps.append(time.time())
    with open(out_path, "a", encoding="ascii") as handle:
        for stamp in stamps:
            handle.write(f"{stamp:.6f}\n")


def test_request_starts_are_spaced_host_wide_across_processes(tmp_path: Path) -> None:
    """Two processes sharing one pace dir must share ONE interval budget.

    Without the host-global limiter each process paces itself, so 2x3 starts
    complete in ~2 intervals of wall time with near-simultaneous cross-process
    starts. With it, all six starts are pairwise spaced by the interval.
    """
    pace_dir = tmp_path / "pace"
    out_path = tmp_path / "starts.txt"
    ctx = multiprocessing.get_context("spawn")
    workers = [
        ctx.Process(target=_paced_worker, args=(str(pace_dir), str(out_path), 3)) for _ in range(2)
    ]
    for proc in workers:
        proc.start()
    for proc in workers:
        proc.join(timeout=60)
        assert proc.exitcode == 0

    stamps = sorted(float(line) for line in out_path.read_text().splitlines())
    assert len(stamps) == 6
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    offenders = [gap for gap in gaps if gap < INTERVAL_S - TOLERANCE_S]
    assert not offenders, f"request starts closer than the host-wide budget: {gaps}"


def test_unset_pace_dir_keeps_single_process_pacing(tmp_path: Path) -> None:
    """Without crawl_pace_dir the legacy in-process path still spaces starts."""
    import config.settings as cs

    cs.get_settings.cache_clear()
    assert cs.get_settings().crawl_pace_dir is None
    from regwatch.sources import http as http_mod

    began = time.monotonic()
    http_mod._pace_request("https://www.fda.gov/a.pdf", 0.2)
    http_mod._pace_request("https://www.fda.gov/b.pdf", 0.2)
    assert time.monotonic() - began >= 0.2 - 0.02


def test_pace_files_are_per_host(tmp_path: Path) -> None:
    """Different FDA hosts must not queue behind each other's budgets."""
    import config.settings as cs

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setenv("CRAWL_PACE_DIR", str(tmp_path / "pace"))
        cs.get_settings.cache_clear()
        from regwatch.sources import http as http_mod

        began = time.monotonic()
        http_mod._pace_request("https://www.fda.gov/a.pdf", 5.0)
        http_mod._pace_request("https://www.accessdata.fda.gov/b.pdf", 5.0)
        elapsed = time.monotonic() - began
        # First start on each host pays no wait; shared-budget coupling would
        # cost ~5s here.
        assert elapsed < 2.0, f"hosts appear to share one pace budget: {elapsed:.2f}s"
        names = sorted(p.name for p in (tmp_path / "pace").iterdir())
        assert names == ["pace-www.accessdata.fda.gov", "pace-www.fda.gov"]
    finally:
        monkey.undo()
        cs.get_settings.cache_clear()
