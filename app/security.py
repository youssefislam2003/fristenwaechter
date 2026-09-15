"""Cryptographic primitives: password hashing (argon2id) and opaque tokens.

Two distinct secret shapes live here:
  * PASSWORDS — human-chosen, low-entropy → argon2id (memory-hard KDF). Stored
    as the full argon2 encoded string (algorithm + params + salt + hash), so a
    later parameter bump is transparently detected via ``needs_rehash``.
  * TOKENS — machine-generated, high-entropy (session cookies, invitation
    links) → a plain SHA-256 of the raw token is stored. No KDF is needed for
    128+ bits of uniform randomness, and SHA-256 keeps lookups a single
    indexed equality on ``token_hash``.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions

# argon2id with library defaults (OWASP-aligned: 64 MiB, t=3, p=4). Tuned
# centrally so a future hardening bump lands in one place and old hashes are
# re-hashed on next login via needs_rehash().
_hasher = PasswordHasher()

# Raw token entropy in bytes. 32 bytes = 256 bits → token_urlsafe yields ~43
# unguessable characters; brute-forcing the SHA-256-indexed lookup is infeasible.
_TOKEN_BYTES = 32


# ─────────────────────────────── passwords ───────────────────────────────


def hash_password(password: str) -> str:
    """Return the argon2id encoded hash string for storage."""
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-timeish verification. A NULL stored hash (invited-but-not-yet-
    activated account) never verifies. Never raises on a wrong password — it
    returns False, so callers branch on a bool, not on exception control flow."""
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except argon2_exceptions.VerifyMismatchError:
        return False
    except argon2_exceptions.InvalidHashError:
        # Corrupt/legacy hash shape — treat as a failed login, not a crash.
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than current policy;
    the caller should re-hash the just-verified plaintext and persist it."""
    return _hasher.check_needs_rehash(stored_hash)


# ───────────────────────────────── tokens ─────────────────────────────────


def new_token() -> str:
    """A fresh URL-safe opaque token to hand out (cookie value / invite link).
    The RAW value is shown to the client exactly once and never stored."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest — what we persist and look rows up by."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    """Timing-safe comparison for any secret-vs-candidate string check."""
    return hmac.compare_digest(a, b)
