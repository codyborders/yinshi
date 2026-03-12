"""Tests for encryption services (DEK generation, wrap/unwrap)."""

import os

import pytest


def test_generate_dek_returns_32_bytes():
    """generate_dek should return a 32-byte random key."""
    from yinshi.services.crypto import generate_dek

    dek = generate_dek()
    assert isinstance(dek, bytes)
    assert len(dek) == 32


def test_generate_dek_unique():
    """Each call should produce a unique key."""
    from yinshi.services.crypto import generate_dek

    keys = {generate_dek() for _ in range(10)}
    assert len(keys) == 10


def test_wrap_unwrap_roundtrip():
    """Wrapping then unwrapping a DEK should return the original."""
    from yinshi.services.crypto import generate_dek, unwrap_dek, wrap_dek

    dek = generate_dek()
    user_id = "a1b2c3d4e5f6"
    pepper = b"test-pepper-value-32-bytes-long!"

    wrapped = wrap_dek(dek, user_id, pepper)
    assert isinstance(wrapped, bytes)
    assert wrapped != dek

    recovered = unwrap_dek(wrapped, user_id, pepper)
    assert recovered == dek


def test_unwrap_wrong_user_id_fails():
    """Unwrapping with wrong user_id should raise."""
    from yinshi.services.crypto import generate_dek, unwrap_dek, wrap_dek

    dek = generate_dek()
    pepper = b"test-pepper-value-32-bytes-long!"

    wrapped = wrap_dek(dek, "correct-user", pepper)

    with pytest.raises(Exception):
        unwrap_dek(wrapped, "wrong-user", pepper)


def test_unwrap_wrong_pepper_fails():
    """Unwrapping with wrong pepper should raise."""
    from yinshi.services.crypto import generate_dek, unwrap_dek, wrap_dek

    dek = generate_dek()
    user_id = "test-user"

    wrapped = wrap_dek(dek, user_id, b"correct-pepper-32-bytes-long!!!")

    with pytest.raises(Exception):
        unwrap_dek(wrapped, user_id, b"wrong-pepper-value-32-bytes!!!!")


def test_encrypt_decrypt_api_key():
    """encrypt_api_key and decrypt_api_key should roundtrip."""
    from yinshi.services.crypto import decrypt_api_key, encrypt_api_key, generate_dek

    dek = generate_dek()
    api_key = "sk-ant-api03-abc123xyz"

    encrypted = encrypt_api_key(api_key, dek)
    assert isinstance(encrypted, bytes)
    assert api_key.encode() not in encrypted

    decrypted = decrypt_api_key(encrypted, dek)
    assert decrypted == api_key


def test_encrypt_api_key_wrong_dek_fails():
    """Decrypting with wrong DEK should raise."""
    from yinshi.services.crypto import (
        decrypt_api_key,
        encrypt_api_key,
        generate_dek,
    )

    dek1 = generate_dek()
    dek2 = generate_dek()
    api_key = "sk-ant-api03-abc123xyz"

    encrypted = encrypt_api_key(api_key, dek1)

    with pytest.raises(Exception):
        decrypt_api_key(encrypted, dek2)
