import hashlib

import pytest

from dfh import auth


def test_new_hashes_are_salted_and_verified():
    first = auth.hash_pw("correct horse battery staple")
    second = auth.hash_pw("correct horse battery staple")

    assert first != second
    assert first.startswith("pbkdf2_sha256$")
    assert auth.verify_pw("correct horse battery staple", first)
    assert auth.verify_pw("correct horse battery staple", second)
    assert not auth.verify_pw("wrong password", first)
    assert not auth.needs_rehash(first)


def test_legacy_demo_hash_is_verified_and_marked_for_upgrade():
    legacy = hashlib.pbkdf2_hmac(
        "sha256", b"demo1234", b"dfh-demo-salt", 20_000
    ).hex()

    assert auth.verify_pw("demo1234", legacy)
    assert not auth.verify_pw("incorrect", legacy)
    assert auth.needs_rehash(legacy)


def test_malformed_hash_is_rejected():
    assert not auth.verify_pw("password", "not-a-valid-hash")
    assert not auth.verify_pw("password", "pbkdf2_sha256$999999999$00$00")


def test_session_token_uses_configured_secret(monkeypatch):
    monkeypatch.setenv("DFH_SECRET", "test-only-secret")
    monkeypatch.setenv("DFH_ENV", "test")
    token = auth.make_token(42, ttl=60)

    assert auth.read_token(token) == 42
    assert auth.read_token(token + "tampered") is None


def test_production_requires_explicit_secret(monkeypatch):
    monkeypatch.delenv("DFH_SECRET", raising=False)
    monkeypatch.setenv("DFH_ENV", "production")

    with pytest.raises(RuntimeError, match="DFH_SECRET"):
        auth.make_token(1)
