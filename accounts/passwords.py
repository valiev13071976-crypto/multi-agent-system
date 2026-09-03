"""Password hashing — Argon2id preferred; scrypt fallback (stdlib)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

_ARGON2 = None
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    _ARGON2 = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
    _VerifyMismatchError = VerifyMismatchError
except ImportError:  # pragma: no cover - exercised when argon2-cffi absent
    _VerifyMismatchError = Exception


def validate_password_policy(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("password_invalid")
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password_policy")
    if password.strip() != password or "\x00" in password:
        raise ValueError("password_policy")


def hash_password(password: str) -> str:
    validate_password_policy(password)
    if _ARGON2 is not None:
        return "argon2id$" + _ARGON2.hash(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        if password_hash.startswith("argon2id$") and _ARGON2 is not None:
            raw = password_hash[len("argon2id$") :]
            try:
                return bool(_ARGON2.verify(raw, password))
            except _VerifyMismatchError:
                return False
        if password_hash.startswith("scrypt$"):
            _, salt_hex, digest_hex = password_hash.split("$", 2)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
            digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
            return hmac.compare_digest(digest, expected)
    except Exception:
        return False
    return False


def looks_like_plaintext_password_store(value: str) -> bool:
    """Detect accidental plaintext persistence."""
    if not value:
        return True
    return not (value.startswith("argon2id$") or value.startswith("scrypt$"))
