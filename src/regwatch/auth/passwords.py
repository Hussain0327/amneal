"""Password hashing via bcrypt (the library directly - passlib is unmaintained).

Also the account-provisioning strength policy (a minimum length + a basic
character-class check) and an OPTIONAL HIBP k-anonymity breach lookup. The
breach lookup FAILS OPEN: an HIBP outage must never block creating an account.
"""

from __future__ import annotations

import hashlib

import bcrypt
import httpx

from regwatch.common.logging import get_logger

log = get_logger(__name__)

# Minimum length and the class floor for a new/changed password. 12 is the
# NIST-aligned floor for human-chosen secrets; the class check only rejects the
# trivially weak (a single character class) - length does the real work, so a
# long all-lowercase passphrase still passes.
MIN_PASSWORD_LENGTH = 12

# HIBP Pwned Passwords range API. We send only the first 5 hex chars of the
# SHA-1 (k-anonymity): the password itself never leaves the process. A short,
# explicit timeout bounds the call; any failure fails open (see below).
_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/"
_HIBP_TIMEOUT_S = 3.0


def _password_bytes(password: str) -> bytes:
    # bcrypt 5.x raises on inputs longer than its 72-byte limit; truncate
    # explicitly so a long passphrase hashes instead of erroring.
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _char_classes(password: str) -> int:
    """Count how many of {lower, upper, digit, other} the password uses."""
    return sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )


def password_breach_count(password: str, *, client: httpx.Client | None = None) -> int:
    """HIBP Pwned Passwords k-anonymity count for ``password`` (0 = not seen).

    Sends only the SHA-1 prefix (first 5 hex chars); the API returns every
    suffix sharing that prefix with its breach count, and we match the rest of
    the digest locally - the password never leaves the process. FAILS OPEN: any
    network error, timeout, non-200, or malformed body returns 0 (treated as
    "not known breached") rather than blocking account provisioning on an HIBP
    outage. The only external call here carries an explicit short timeout.
    """
    # SHA-1 is HIBP's required k-anonymity hash, not a security primitive here.
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    owns_client = client is None
    active = client or httpx.Client(timeout=_HIBP_TIMEOUT_S)
    try:
        resp = active.get(
            f"{_HIBP_RANGE_URL}{prefix}",
            headers={"Add-Padding": "true"},
            timeout=_HIBP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            log.warning("hibp_check_non_200", status=resp.status_code)
            return 0
        for line in resp.text.splitlines():
            candidate, _, count = line.partition(":")
            if candidate.strip().upper() == suffix:
                try:
                    return int(count.strip())
                except ValueError:
                    return 0
        return 0
    except Exception as exc:  # fail open: never block provisioning on an HIBP outage
        log.warning("hibp_check_failed", error_type=type(exc).__name__)
        return 0
    finally:
        if owns_client:
            active.close()


def validate_password_strength(
    password: str, *, check_breach: bool = True, client: httpx.Client | None = None
) -> str | None:
    """Return a human-readable rejection reason, or None when the password passes.

    Enforces a minimum length and a basic character-class floor, then (opt-in,
    default on) an HIBP breach lookup. The breach lookup fails open, so a network
    outage degrades to "length + classes only" rather than rejecting a valid
    password or blocking provisioning entirely.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if _char_classes(password) < 2:
        return (
            "password must mix at least two character types "
            "(lowercase, uppercase, digits, or symbols)"
        )
    if check_breach and password_breach_count(password, client=client) > 0:
        return (
            "this password appears in a known public breach corpus (HIBP) - "
            "choose a different one"
        )
    return None


# Executable spec twin of the Go login's uniform-timing dummy hash
# (go/internal/api/auth.go dummyHash): the login that verifies against this on
# unknown emails moved to the Go proxy in step 4, and Go hardcodes the SAME
# string at the same bcrypt cost so a missing user costs the same work as a
# wrong password - no timing oracle on email existence. Kept here so the
# scheme has one documented Python-side anchor next to hash_password.
_DUMMY_HASH = hash_password("regwatch-dummy-password-for-uniform-timing")
