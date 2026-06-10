"""Password hashing via bcrypt (the library directly — passlib is unmaintained)."""

from __future__ import annotations

import bcrypt


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


# Login verifies against this when the email is unknown, so a missing user costs
# the same bcrypt work as a wrong password — no timing oracle on email existence.
_DUMMY_HASH = hash_password("regwatch-dummy-password-for-uniform-timing")
