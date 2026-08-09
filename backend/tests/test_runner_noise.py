"""Verify persistent runner Noise identity and protocol interoperability.

Tests use canonical Noise vectors and temporary owner-only key files, avoiding
network services while checking the browser-to-runner cryptographic boundary.
"""

from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest

from yinshi.services.runner_noise import (
    NoiseIkResponder,
    load_or_create_runner_noise_keypair,
)


def _bytes(hex_value: str) -> bytes:
    return bytes.fromhex(hex_value)


def test_runner_noise_key_is_persistent_and_owner_only(tmp_path: Path) -> None:
    """Runner identity survives restarts without exposing private key bytes."""
    key_path = tmp_path / "runner" / "noise-static.key"

    first = load_or_create_runner_noise_keypair(key_path)
    second = load_or_create_runner_noise_keypair(key_path)

    assert len(first.private_key) == 32
    assert len(first.public_key) == 32
    assert first == second
    assert key_path.read_bytes() == first.private_key
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert "=" not in first.public_key_base64url
    assert base64.urlsafe_b64decode(first.public_key_base64url + "=") == first.public_key


def test_runner_noise_key_rejects_insecure_permissions(tmp_path: Path) -> None:
    """A group-readable static private key fails closed instead of being reused."""
    key_path = tmp_path / "noise-static.key"
    key_path.write_bytes(b"x" * 32)
    key_path.chmod(0o640)

    with pytest.raises(RuntimeError, match="owner-only permissions"):
        load_or_create_runner_noise_keypair(key_path)


def test_noise_responder_rejects_truncated_handshake() -> None:
    """Responder rejects undersized input before invoking cryptographic parsing."""
    responder = NoiseIkResponder(static_private_key=b"r" * 32)

    with pytest.raises(ValueError, match="invalid length"):
        responder.read_handshake_message(b"x" * 95)


def test_noise_responder_matches_canonical_ik_vector() -> None:
    """Python responder interoperates with canonical browser-side IK messages."""
    with pytest.warns(UserWarning, match="ephemeral keypairs is already set"):
        responder = NoiseIkResponder(
            static_private_key=_bytes(
                "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
            ),
            ephemeral_private_key=_bytes(
                "bbdb4cdbd309f1a1f2e1456967fe288cadd6f712d65dc7b7793d5e63da6b375b"
            ),
            prologue=_bytes("4a6f686e2047616c74"),
        )
    first_payload = responder.read_handshake_message(
        _bytes(
            "ca35def5ae56cec33dc2036731ab14896bc4c75dbb07a61f879f8e3afa4c7944"
            "718da798efbcd91528520204f904b9bd6c7413dccdc214d951e15253e39987f"
            "18146e8cd0873654207148333479d4d16c289f0294b29960a72f48e0b7bba2"
            "e89083169825e59642148d492020664ccf7"
        )
    )
    assert first_payload == _bytes("4c756477696720766f6e204d69736573")
    assert responder.initiator_static_public_key == _bytes(
        "6bc3822a2aa7f4e6981d6538692b3cdf3e6df9eea6ed269eb41d93c22757b75a"
    )

    response = responder.write_handshake_message(_bytes("4d757272617920526f746862617264"))
    assert response == _bytes(
        "95ebc60d2b1fa672c1f46a8aa265ef51bfe38e7ccb39ec5be34069f144808843"
        "5361e70b2ed446e6c9ec387d1d6b3b840f194e373979d241b203c4acafccf5"
    )
    assert responder.handshake_hash == _bytes(
        "0b0f68fb0c27e03ce9b97565995ed4838cc0581b762ef72b062f6a546419fad7"
    )
    assert responder.decrypt(
        _bytes("050e9f3c8fac16b68dbce8f8c4bfbf6617c897f9ada4aa29aa19c8")
    ) == _bytes("462e20412e20486179656b")
    assert responder.encrypt(_bytes("4361726c204d656e676572")) == _bytes(
        "344233a6cabb7141d80f3da2fedc311d9646bbb0f505afe403a667"
    )
