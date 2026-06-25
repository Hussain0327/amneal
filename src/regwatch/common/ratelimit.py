"""In-memory, per-process rate limiting.

Pilot scope: one API process, so a threading.Lock plus a deque of monotonic
timestamps per key is sufficient. Distributed rate limiting belongs to the IT
gateway in production (docs/PROD_READINESS.md #1).
"""

from __future__ import annotations

import threading
import time
from collections import deque

# Login brute-force guard: attempts per email per minute (fixed, not settings —
# there is no legitimate reason to raise it).
LOGIN_ATTEMPTS_PER_MINUTE = 10
# Companion per-IP cap so spraying many DISTINCT emails from one host is also
# throttled (the per-email key alone misses a username-enumeration sweep). Set
# above the per-email cap: a small shared office/VPN NAT can have a few users
# legitimately logging in within the same minute, so this guards the abusive
# burst without locking out a shared egress IP at the first wrong password.
LOGIN_ATTEMPTS_PER_IP_PER_MINUTE = 30
# NOTE (scope): this limiter is in-process, so under fly.toml's
# min_machines_running=2 each machine keeps its own window and the EFFECTIVE
# ceiling is ~2x these numbers. A shared-store (Redis/Postgres) limiter is the
# fix for an exact global cap, but that is a separate, parked item
# (docs/PROD_READINESS.md #1) - do NOT build it here.


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

        Login keys embed the client-supplied email, so without eviction an
        unauthenticated caller spraying unique emails grows the dict for the
        life of the process. At most one O(keys) pass per window.
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
login_limiter = RateLimiter()


def reset_for_tests() -> None:
    """Clear limiter state so one test's traffic cannot 429 the next."""
    query_limiter.reset()
    login_limiter.reset()
