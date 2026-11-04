"""Password hashing and signed session tokens.

New password hashes use a per-password random salt and PBKDF2-HMAC-SHA256.
Legacy demo hashes are still verified so existing demo databases can be
upgraded transparently at the next successful login.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time

_ITERATIONS = 310_000
_LEGACY_SALT = b"dfh-demo-salt"
_LEGACY_ITERATIONS = 20_000
_DEV_SECRET = "dev-secret-change-me"


def _secret() -> bytes:
    value = os.getenv("DFH_SECRET")
    if value:
        return value.encode("utf-8")
    if os.getenv("DFH_ENV", "development").lower() in {"production", "prod"}:
        raise RuntimeError("DFH_SECRET must be configured in production")
    return _DEV_SECRET.encode("utf-8")


def hash_pw(password: str) -> str:
    """Return a salted, versioned PBKDF2 password hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2_sha256$" + str(_ITERATIONS) + "$" + salt.hex() + "$" + digest.hex()


def verify_pw(password: str, encoded: str) -> bool:
    """Verify both current versioned hashes and legacy demo hashes."""
    if not isinstance(encoded, str):
        return False
    try:
        if encoded.startswith("pbkdf2_sha256$"):
            algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            rounds = int(iterations)
            if rounds < 1 or rounds > 2_000_000:
                return False
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
            return hmac.compare_digest(actual, expected)
        if len(encoded) != 64:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _LEGACY_SALT, _LEGACY_ITERATIONS
        ).hex()
        return hmac.compare_digest(actual, encoded)
    except (ValueError, TypeError, UnicodeError):
        return False


def needs_rehash(encoded: str) -> bool:
    return not isinstance(encoded, str) or not encoded.startswith("pbkdf2_sha256$")


def make_token(uid: int, ttl: int = 12 * 3600) -> str:
    msg = f"{uid}|{int(time.time()) + ttl}"
    sig = hmac.new(_secret(), msg.encode(), "sha256").hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def read_token(token: str):
    try:
        uid, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
        good = hmac.new(_secret(), f"{uid}|{exp}".encode(), "sha256").hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return int(uid)
    except (ValueError, TypeError, UnicodeError, RuntimeError):
        pass
    return None
