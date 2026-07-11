"""Asymmetric compact tokens for desktop access and offline account leases."""

from __future__ import annotations

import base64
import json
from typing import Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from yinshi.config import get_settings

DesktopTokenType = Literal["access", "lease"]
_TOKEN_TYPE_HEADERS: dict[DesktopTokenType, str] = {
    "access": "YINSHI-ACCESS",
    "lease": "YINSHI-LEASE",
}


def _encode_base64url(value: bytes) -> str:
    """Encode one compact-token segment without padding."""
    if not isinstance(value, bytes):
        raise TypeError("value must be bytes")
    if not value:
        raise ValueError("value must not be empty")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signing_key() -> Ed25519PrivateKey:
    """Derive a domain-separated Ed25519 key from the stable session secret."""
    secret = get_settings().secret_key.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("desktop token signing requires a 32-byte session secret")
    seed = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"yinshi-desktop-token-signing-v1",
        info=b"Ed25519 compact tokens",
    ).derive(secret)
    if len(seed) != 32:
        raise RuntimeError("desktop token signing key derivation failed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def desktop_signing_public_key() -> str:
    """Return the unpadded raw Ed25519 public key for desktop verification."""
    public_key = (
        _signing_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    if len(public_key) != 32:
        raise RuntimeError("desktop token public key must contain 32 bytes")
    return _encode_base64url(public_key)


def create_desktop_token(
    *,
    token_type: DesktopTokenType,
    user_id: str,
    device_id: str,
    issued_at: int,
    expires_at: int,
) -> str:
    """Create one Ed25519-signed access token or offline account lease."""
    if token_type not in _TOKEN_TYPE_HEADERS:
        raise ValueError(f"Unsupported desktop token type: {token_type}")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must not be empty")
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id must not be empty")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise TypeError("token timestamps must be integers")
    if expires_at <= issued_at:
        raise ValueError("expires_at must be after issued_at")

    header = {
        "alg": "EdDSA",
        "typ": _TOKEN_TYPE_HEADERS[token_type],
        "v": 1,
    }
    payload = {
        "aud": "yinshi-desktop",
        "device_id": device_id,
        "exp": expires_at,
        "iat": issued_at,
        "sub": user_id,
        "v": 1,
    }
    encoded_header = _encode_base64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _encode_base64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = _signing_key().sign(signing_input.encode("ascii"))
    if len(signature) != 64:
        raise RuntimeError("desktop token signature must contain 64 bytes")
    return f"{signing_input}.{_encode_base64url(signature)}"
