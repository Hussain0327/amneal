"""S1 / S2 / S21b: proof the suite is running THROUGH the Go edge.

Every other scenario inherits its meaning from these: if the harness were
accidentally pointed at uvicorn, the relayed paths would still 200 and the
whole matrix would pass vacuously. /healthz and POST /auth/login exist ONLY
in Go (the Python handlers were deleted in step 4/B2), so a working login
through the edge plus a 404 from uvicorn for the same call is the
cross-runtime proof itself.
"""

from __future__ import annotations

import httpx
import pytest

from tests_contract.conftest import (
    CLIENT_TIMEOUT,
    DEFAULT_PASSWORD,
    Harness,
    Stack,
    pg_conn,
    sha256_hex,
)

# 300s: first test per flavor pays go-build/init-db/boot; thread-method kill is diagnostics-poor.
pytestmark = [pytest.mark.contract, pytest.mark.timeout(300)]


def test_s1_healthz_splits_the_runtimes(base_stack: Stack) -> None:
    """EDGE /healthz is answered locally by Go; uvicorn has no such route."""
    edge = httpx.get(f"{base_stack.edge_url}/healthz", timeout=CLIENT_TIMEOUT)
    assert edge.status_code == 200
    assert edge.text == "ok\n"

    upstream = httpx.get(f"{base_stack.uvicorn_url}/healthz", timeout=CLIENT_TIMEOUT)
    assert upstream.status_code == 404


def test_s2_login_exists_only_in_go(harness: Harness, base_stack: Stack) -> None:
    """Go mints the cookie; Python (which only VERIFIES cookies) 404s the
    same call. The auth_session row stores sha256(token), the shared scheme
    both runtimes speak."""
    email = harness.next_email()
    harness.seed_user(email)
    body = {"email": email, "password": DEFAULT_PASSWORD}

    edge = httpx.post(
        f"{base_stack.edge_url}/auth/login",
        json=body,
        headers={"Fly-Client-IP": harness.next_client_ip()},
        timeout=CLIENT_TIMEOUT,
    )
    assert edge.status_code == 200
    user = edge.json()["user"]
    assert set(user.keys()) == {"id", "email", "display_name", "role"}
    assert user["email"] == email.lower()

    set_cookie = edge.headers.get("set-cookie", "")
    assert "regwatch_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/" in set_cookie

    cookie = edge.cookies["regwatch_session"]
    assert len(cookie) == 43  # token_urlsafe(32) parity, pinned by the Go tests
    with pg_conn() as conn:
        row = conn.execute(
            "SELECT token_hash FROM public.auth_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == sha256_hex(cookie)

    # The B2-deletion proof: the same POST against uvicorn directly finds no
    # handler at all.
    upstream = httpx.post(f"{base_stack.uvicorn_url}/auth/login", json=body, timeout=CLIENT_TIMEOUT)
    assert upstream.status_code == 404


def test_s21b_method_mismatch_stays_defined_on_both_sides(base_stack: Stack) -> None:
    """GET /query/stream relays to FastAPI's 405 (POST-only route); GET
    /auth/login is answered by Go's native FastAPI-shaped 405 without touching
    the relay. The Server header discriminates: uvicorn stamps its own,
    Go-native responses carry none (and the relay forwards uvicorn's)."""
    relayed = httpx.get(f"{base_stack.edge_url}/query/stream", timeout=CLIENT_TIMEOUT)
    assert relayed.status_code == 405
    assert relayed.headers.get("allow") == "POST"
    assert relayed.json() == {"detail": "Method Not Allowed"}
    assert relayed.headers.get("server", "").lower() == "uvicorn"

    native = httpx.get(f"{base_stack.edge_url}/auth/login", timeout=CLIENT_TIMEOUT)
    assert native.status_code == 405
    assert native.headers.get("allow") == "POST"
    assert native.json() == {"detail": "Method Not Allowed"}
    assert "server" not in native.headers
