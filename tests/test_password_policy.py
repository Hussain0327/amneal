"""Password strength + HIBP breach policy (#5a).

Covers the strength floor (length + character classes), the HIBP k-anonymity
breach lookup (only the SHA-1 prefix leaves the process), and the load-bearing
guarantee that an HIBP outage FAILS OPEN - provisioning is never blocked on an
HIBP error. Also asserts both CLI provisioning paths (create-user, set-password)
enforce the policy via _prompt_password.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx
from typer.testing import CliRunner

from regwatch.auth import passwords
from regwatch.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    password_breach_count,
    validate_password_strength,
)
from regwatch.cli import app as cli_app
from tests.conftest import create_user

_HIBP_HOST = "https://api.pwnedpasswords.com"


def _hibp_split(password: str) -> tuple[str, str]:
    # Mirrors the production hash: SHA-1 is HIBP's k-anonymity scheme, not crypto.
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    return digest[:5], digest[5:]


# ---------- strength floor ----------


def test_short_password_rejected_without_network() -> None:
    # Length is checked BEFORE the breach lookup, so a too-short password is
    # rejected with no HTTP call at all (check_breach left default True).
    reason = validate_password_strength("aB3$xy", check_breach=False)
    assert reason is not None and str(MIN_PASSWORD_LENGTH) in reason


def test_single_character_class_rejected() -> None:
    # Long but all-lowercase -> fails the class floor (>= 2 classes required).
    assert validate_password_strength("abcdefghijklmnop", check_breach=False) is not None


def test_long_passphrase_two_words_passes_strength() -> None:
    # A long passphrase with a digit clears length + classes (breach off here).
    assert validate_password_strength("correct horse battery staple 7", check_breach=False) is None


# ---------- HIBP k-anonymity ----------


@respx.mock
def test_breach_lookup_sends_only_prefix_and_matches_suffix() -> None:
    password = "correct horse battery 9 staple"
    prefix, suffix = _hibp_split(password)
    route = respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(
        return_value=httpx.Response(
            200, text=f"0000000000000000000000000000000000A:1\r\n{suffix}:42\r\n"
        )
    )
    assert password_breach_count(password) == 42
    assert route.called
    # k-anonymity: only the 5-char prefix is in the request path, never the
    # password or the full digest.
    requested = str(route.calls.last.request.url)
    assert requested.endswith(f"/range/{prefix}")
    assert suffix not in requested
    assert password not in requested


@respx.mock
def test_breach_lookup_miss_returns_zero() -> None:
    password = "a-very-unlikely-passphrase-2026"
    prefix, _ = _hibp_split(password)
    respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(
        return_value=httpx.Response(200, text="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:3\r\n")
    )
    assert password_breach_count(password) == 0


@respx.mock
def test_validate_rejects_breached_password() -> None:
    password = "Password1234"  # mixes classes + long enough; only HIBP rejects it
    prefix, suffix = _hibp_split(password)
    respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(
        return_value=httpx.Response(200, text=f"{suffix}:99\r\n")
    )
    reason = validate_password_strength(password)
    assert reason is not None and "breach" in reason.lower()


# ---------- fail-open on any HIBP error ----------


@respx.mock
def test_hibp_network_error_fails_open() -> None:
    password = "Password1234"
    prefix, _ = _hibp_split(password)
    respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(side_effect=httpx.ConnectError("hibp down"))
    # FAIL OPEN: a network error must NOT block. Count is 0 and the otherwise
    # strong password passes (provisioning is never gated on an HIBP outage).
    assert password_breach_count(password) == 0
    assert validate_password_strength(password) is None


@respx.mock
def test_hibp_timeout_fails_open() -> None:
    password = "Password1234"
    prefix, _ = _hibp_split(password)
    respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(side_effect=httpx.TimeoutException("slow"))
    assert password_breach_count(password) == 0


@respx.mock
def test_hibp_non_200_fails_open() -> None:
    password = "Password1234"
    prefix, _ = _hibp_split(password)
    respx.get(f"{_HIBP_HOST}/range/{prefix}").mock(return_value=httpx.Response(503))
    assert password_breach_count(password) == 0
    assert validate_password_strength(password) is None


# ---------- both CLI provisioning paths enforce it ----------


def test_cli_create_user_rejects_weak_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # Breach lookup must never run for this assertion (and weak passwords fail on
    # length before it anyway); stub it to 0 so the test is network-free.
    monkeypatch.setattr(passwords, "password_breach_count", lambda *a, **k: 0)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["create-user", "weak@example.com", "--name", "Weak"],
        input="short\nshort\n",
    )
    assert result.exit_code == 2, result.output
    assert "at least" in result.output


def test_cli_set_password_rejects_weak_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords, "password_breach_count", lambda *a, **k: 0)
    create_user("change@example.com", "correct-horse-battery-staple")
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["set-password", "change@example.com"],
        input="tiny\ntiny\n",
    )
    assert result.exit_code == 2, result.output
    assert "at least" in result.output


def test_cli_create_user_accepts_strong_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # A strong, non-breached password provisions successfully (breach stubbed to
    # 0 so the create path is offline and deterministic).
    monkeypatch.setattr(passwords, "password_breach_count", lambda *a, **k: 0)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["create-user", "strong@example.com", "--name", "Strong"],
        input="Tr0ub4dour-and-3\nTr0ub4dour-and-3\n",
    )
    assert result.exit_code == 0, result.output
