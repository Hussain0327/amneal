"""In-memory, per-process rate limiting.

Pilot scope: one API process, so a threading.Lock plus a deque of monotonic
timestamps per key is sufficient. Distributed rate limiting belongs to the IT
gateway in production (docs/PROD_READINESS.md #1).
"""

from __future__ import annotations

import threading
import time
from collections import deque

# NOTE (scope): this limiter is in-process, so under fly.toml's
# min_machines_running=2 each machine keeps its own window and the EFFECTIVE
# ceiling is ~2x the configured limit. A shared-store (Redis/Postgres) limiter
# is the fix for an exact global cap, but that is a separate, parked item
# (docs/PROD_READINESS.md #1) - do NOT build it here. The LOGIN limiter moved
# to the Go proxy with the step-4 auth cutover (go/internal/api/ratelimit.go
# ports this class); only query_limiter remains Python-side.


class RateLimiter:
    """Sliding one-minute window per key. ``limit <= 0`` disables the check."""

    def __init__(self, window_s: float = 60.0) -> None:
        self._window_s = window_s
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = 0.0

    def allow(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window_s:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        """Evict keys whose whole window has expired. Caller holds the lock.

        Keys can embed caller-supplied identifiers, so without eviction a
        spray of unique keys grows the dict for the life of the process (the
        original motivation was login emails, now the Go limiter's problem;
        the guard stays for any future key shape). One O(keys) pass per window.
        """
        if now - self._last_sweep < self._window_s:
            return
        self._last_sweep = now
        stale = [
            key for key, hits in self._hits.items() if not hits or now - hits[-1] >= self._window_s
        ]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


query_limiter = RateLimiter()


def reset_for_tests() -> None:
    """Clear limiter state so one test's traffic cannot 429 the next."""
    query_limiter.reset()
