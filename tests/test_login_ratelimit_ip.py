"""Per-IP login limiter (#5b).

The login route keys the limiter by BOTH email and client IP. These cover:
  * the per-IP key trips on a credential-spray of DISTINCT emails from one host
    (which the per-email key alone never sees);
  * the IP is un-spoofable: with trust_proxy_headers OFF the limiter keys on
    request.client.host, so a rotating X-Forwarded-For cannot mint fresh budgets;
  * with trust_proxy_headers ON behind Fly, the key is the platform-attested
    Fly-Client-IP, so a rotating (attacker-controlled) leftmost XFF still trips
    the cap. Only when Fly-Client-IP is absent does it fall back to the RIGHTMOST
    XFF hop (the one our trusted edge appended), never the spoofable leftmost.

`authenticate` is stubbed to avoid 30+ real bcrypt verifies; the limiter keying
is what is under test, not the credential check.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import config.settings as cs
import pytest
from fastapi.testclient import TestClient

from regwatch.api import main


def _anon() -> TestClient:
    c = TestClient(main.app)
    c.__enter__()
    return c


@pytest.fixture(autouse=True)
def _fast_auth_and_small_ip_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # No real bcrypt: every login is a fast miss, so the limiter math is what's
    # exercised. Small per-IP cap keeps the spray short and deterministic.
    monkeypatch.setattr(main, "authenticate", lambda *a, **k: None)
    monkeypatch.setattr(main, "LOGIN_ATTEMPTS_PER_IP_PER_MINUTE", 3)


def test_per_ip_key_trips_across_distinct_emails() -> None:
    # Each email is unique, so the per-EMAIL key never accumulates past 1 - only
    # the per-IP key catches this spray. With the IP cap at 3, the 4th distinct
    # email is the first 429 (and it's the limiter, not the 401 credential miss).
    c = _anon()
    for i in range(3):
        r = c.post("/auth/login", json={"email": f"user{i}@example.com", "password": "x"})
        assert r.status_code == 401, r.text  # passes the limiter, fails creds
    blocked = c.post("/auth/login", json={"email": "user99@example.com", "password": "x"})
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}


def test_xff_ignored_when_proxy_not_trusted() -> None:
    # trust_proxy_headers defaults False: a rotating X-Forwarded-For must NOT
    # create fresh IP budgets - the limiter keys on request.client.host, which a
    # direct caller cannot spoof. So a spoofed-XFF spray still trips at the cap.
    c = _anon()
    for i in range(3):
        r = c.post(
            "/auth/login",
            json={"email": f"u{i}@example.com", "password": "x"},
            headers={"x-forwarded-for": f"203.0.113.{i}"},  # different each time
        )
        assert r.status_code == 401
    blocked = c.post(
        "/auth/login",
        json={"email": "u99@example.com", "password": "x"},
        headers={"x-forwarded-for": "203.0.113.250"},
    )
    assert blocked.status_code == 429  # XFF rotation did not help the attacker


def test_spoofed_leftmost_xff_still_trips_when_fly_client_ip_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production case: behind Fly with trust_proxy_headers on, the attested
    # client is Fly-Client-IP. An attacker who rotates the LEFTMOST X-Forwarded-For
    # hop (the only part they control) must NOT escape the per-IP cap - the limiter
    # keys on the fixed Fly-Client-IP, so the spray still trips at the cap. This is
    # the regression guard for the credential-spray bypass.
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    cs.get_settings.cache_clear()
    c = _anon()
    for i in range(3):
        r = c.post(
            "/auth/login",
            json={"email": f"s{i}@example.com", "password": "x"},
            headers={
                # Rotating, attacker-controlled leftmost hop; fixed attested client.
                "x-forwarded-for": f"203.0.113.{i}, 10.0.0.9",
                "fly-client-ip": "198.51.100.7",
            },
        )
        assert r.status_code == 401
    blocked = c.post(
        "/auth/login",
        json={"email": "s99@example.com", "password": "x"},
        headers={
            "x-forwarded-for": "203.0.113.250, 10.0.0.9",  # new spoofed leftmost
            "fly-client-ip": "198.51.100.7",  # same real client
        },
    )
    assert blocked.status_code == 429  # rotating the spoofed hop did not help


def test_rightmost_xff_used_when_no_fly_client_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fallback path (XFF only, no Fly-Client-IP): the key is the RIGHTMOST hop
    # (appended by our trusted edge), so rotating the LEFTMOST hop while the right
    # hop is fixed still trips the cap - the spoofable leftmost is never the key.
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    cs.get_settings.cache_clear()
    c = _anon()
    for i in range(3):
        r = c.post(
            "/auth/login",
            json={"email": f"r{i}@example.com", "password": "x"},
            headers={"x-forwarded-for": f"203.0.113.{i}, 70.70.70.70"},  # fixed right hop
        )
        assert r.status_code == 401
    blocked = c.post(
        "/auth/login",
        json={"email": "r99@example.com", "password": "x"},
        headers={"x-forwarded-for": "203.0.113.250, 70.70.70.70"},
    )
    assert blocked.status_code == 429


def test_distinct_attested_clients_get_independent_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuinely different attested client (different Fly-Client-IP) has its own
    # budget and is unaffected when another client is over its cap - so the per-IP
    # guard does not collapse all real users into one shared window.
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    cs.get_settings.cache_clear()
    c = _anon()
    for i in range(3):  # exhaust client A's budget
        r = c.post(
            "/auth/login",
            json={"email": f"a{i}@example.com", "password": "x"},
            headers={"fly-client-ip": "198.51.100.1"},
        )
        assert r.status_code == 401
    blocked = c.post(
        "/auth/login",
        json={"email": "a99@example.com", "password": "x"},
        headers={"fly-client-ip": "198.51.100.1"},
    )
    assert blocked.status_code == 429  # client A is over budget

    fresh = c.post(
        "/auth/login",
        json={"email": "b@example.com", "password": "x"},
        headers={"fly-client-ip": "198.51.100.2"},  # a DIFFERENT attested client
    )
    assert fresh.status_code == 401  # has its own budget


def test_per_email_key_still_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    # The original per-email guard is unchanged: hammering ONE email trips the
    # (lower) per-email cap first, before the per-IP cap. Raise the IP cap so the
    # email key is unambiguously what fires.
    monkeypatch.setattr(main, "LOGIN_ATTEMPTS_PER_IP_PER_MINUTE", 1000)
    monkeypatch.setattr(main, "LOGIN_ATTEMPTS_PER_MINUTE", 3)
    c = _anon()
    for _ in range(3):
        assert (
            c.post("/auth/login", json={"email": "same@example.com", "password": "x"}).status_code
            == 401
        )
    blocked = c.post("/auth/login", json={"email": "same@example.com", "password": "x"})
    assert blocked.status_code == 429


def test_prod_fly_toml_enables_trust_proxy_headers() -> None:
    # trust_proxy_headers defaults False, but in prod the app ONLY serves behind
    # Fly's edge, where request.client.host is the Fly proxy - so without this flag
    # set every caller collapses into ONE per-IP bucket and the spray guard above is
    # gutted. The ON-branch logic of _client_ip is proven by the tests above; this
    # is the missing guard that the prod CONFIG actually turns it on. If someone
    # drops TRUST_PROXY_HEADERS from fly.toml [env], this fails instead of silently
    # re-collapsing the limiter in production.
    fly_toml = Path(__file__).resolve().parents[1] / "fly.toml"
    cfg = tomllib.loads(fly_toml.read_text(encoding="utf-8"))
    # Must be the string "true": pydantic-settings parses it to the bool True, and
    # _client_ip then keys on the attested Fly-Client-IP.
    assert cfg["env"].get("TRUST_PROXY_HEADERS") == "true"
