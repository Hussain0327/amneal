"""Prod-config guard: fly.toml must keep TRUST_PROXY_HEADERS on.

The per-IP login limiter lives in the Go proxy since the step-4 auth cutover
(go/internal/api/clientip.go ports the header-trust rationale: Fly-Client-IP
first, rightmost XFF fallback, never the spoofable leftmost hop). The keying
logic is pinned by Go's own tests (TestClientIP,
TestLoginPerIPKeyingUnderTrustProxy) -- but those run with t.Setenv, so
nothing over there notices if someone drops TRUST_PROXY_HEADERS from
fly.toml [env]. This is the one guard that the prod CONFIG actually turns the
attested-IP keying on; without it every caller behind Fly's edge collapses
into one per-IP bucket and the spray guard is gutted, silently.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_prod_fly_toml_enables_trust_proxy_headers() -> None:
    fly_toml = Path(__file__).resolve().parents[1] / "fly.toml"
    cfg = tomllib.loads(fly_toml.read_text(encoding="utf-8"))
    # Must be the string "true": both runtimes parse it to boolean true (Go:
    # go/internal/api/config.go envBool; the Go binary reads it directly).
    assert cfg["env"].get("TRUST_PROXY_HEADERS") == "true"
