"""Asymmetric compact tokens for desktop access and offline account leases."""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from yinshi.config import get_settings

DesktopTokenType = Literal["access", "lease"]
_TOKEN_TYPE_HEADERS: dict[DesktopTokenType, str] = {
    "access": "YINSHI-ACCESS",
    "lease": "YINSHI-LEASE",
}


@dataclass(frozen=True, slots=True)
class VerifiedDesktopAccess:
    """Identity claims from one valid unexpired desktop access token."""

    user_id: str
    device_id: str


def _decode_base64url(value: str) -> bytes:
    """Strictly decode one unpadded compact-token segment."""
    if not isinstance(value, str) or not value:
        raise ValueError("compact token segment must not be empty")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise ValueError("compact token segment is not valid base64url") from error


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


def verify_desktop_access_token(
    token: str,
    *,
    current_time: int | None = None,
) -> VerifiedDesktopAccess | None:
    """Verify one compact access token and return only its identity claims."""
    if not isinstance(token, str) or not token:
        return None
    segments = token.split(".")
    if len(segments) != 3:
        return None
    encoded_header, encoded_payload, encoded_signature = segments
    try:
        header = json.loads(_decode_base64url(encoded_header))
        payload = json.loads(_decode_base64url(encoded_payload))
        signature = _decode_base64url(encoded_signature)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if header != {"alg": "EdDSA", "typ": "YINSHI-ACCESS", "v": 1}:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("aud") != "yinshi-desktop" or payload.get("v") != 1:
        return None

    user_id = payload.get("sub")
    device_id = payload.get("device_id")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not isinstance(user_id, str) or not user_id:
        return None
    if not isinstance(device_id, str) or not device_id:
        return None
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return None
    now = int(time.time()) if current_time is None else current_time
    if issued_at > now + 60 or expires_at <= now or expires_at - issued_at > 15 * 60:
        return None

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        _signing_key().public_key().verify(signature, signing_input)
    except InvalidSignature:
        return None
    return VerifiedDesktopAccess(user_id=user_id, device_id=device_id)


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
