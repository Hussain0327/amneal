package api

import (
	"sync"
	"time"
)

// Port of src/regwatch/common/ratelimit.py -- in-memory, per-process sliding
// one-minute window. The caps are FIXED, not settings (the Python file: "there
// is no legitimate reason to raise it"), and the limiter being per-process is
// the SAME scope as Python's: under min_machines_running=2 each machine keeps
// its own window and the effective ceiling is ~2x these numbers, exactly as
// documented there. A shared-store limiter stays a parked item.
const (
	// Login brute-force guard: attempts per email per minute.
	LoginAttemptsPerMinute = 10
	// Companion per-IP cap so spraying many DISTINCT emails from one host is
	// also throttled; above the per-email cap so a shared office/VPN NAT is
	// not locked out at the first wrong password.
	LoginAttemptsPerIPPerMinute = 30

	rateWindow = time.Minute
)

// RateLimiter is a sliding one-minute window per key. limit <= 0 disables the
// check. now is injectable for tests (Python uses time.monotonic; Go's
// time.Time carries a monotonic clock on time.Now()).
type RateLimiter struct {
	mu        sync.Mutex
	hits      map[string][]time.Time
	lastSweep time.Time
	now       func() time.Time
}

func NewRateLimiter() *RateLimiter {
	return &RateLimiter{hits: map[string][]time.Time{}, now: time.Now}
}

// Allow reports whether key may take another hit, consuming a slot when it
// may. Mirrors ratelimit.py::RateLimiter.allow exactly, including the
// consume-on-success behavior (successful logins also charge the bucket).
func (l *RateLimiter) Allow(key string, limit int) bool {
	if limit <= 0 {
		return true
	}
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.sweep(now)
	hits := l.hits[key]
	for len(hits) > 0 && now.Sub(hits[0]) >= rateWindow {
		hits = hits[1:]
	}
	if len(hits) >= limit {
		l.hits[key] = hits
		return false
	}
	l.hits[key] = append(hits, now)
	return true
}

// sweep evicts keys whose whole window has expired; caller holds the lock.
// Login keys embed the client-supplied email, so without eviction an
// unauthenticated caller spraying unique emails grows the map for the life of
// the process. At most one O(keys) pass per window.
func (l *RateLimiter) sweep(now time.Time) {
	if now.Sub(l.lastSweep) < rateWindow {
		return
	}
	l.lastSweep = now
	for key, hits := range l.hits {
		if len(hits) == 0 || now.Sub(hits[len(hits)-1]) >= rateWindow {
			delete(l.hits, key)
		}
	}
}

// Reset clears limiter state (the reset_for_tests parity hook).
func (l *RateLimiter) Reset() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.hits = map[string][]time.Time{}
}
