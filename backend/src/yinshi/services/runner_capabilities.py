"""Short-lived Ed25519 capabilities for end-to-end encrypted runner sessions."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from yinshi.config import get_settings

RUNNER_PROTOCOL_VERSION = "yinshi-runner-v1"
RUNNER_CAPABILITY_TTL_SECONDS = 300
RUNNER_FRAME_BYTES_MAX = 65_535
RUNNER_SESSION_BYTES_MIN = 65_536
RUNNER_SESSION_BYTES_MAX = 1_073_741_824
RUNNER_SCOPES = frozenset(
    {
        "files.read",
        "files.write",
        "pi.configure",
        "provider.configure",
        "repository.read",
        "repository.write",
        "session.read",
        "session.stream",
        "session.write",
        "terminal",
        "transfer.read",
        "transfer.write",
        "worker.health",
        "workspace.read",
        "workspace.write",
    }
)
_HEADER = {"alg": "EdDSA", "typ": "YINSHI-RUNNER-CAP", "v": 1}
_PAYLOAD_KEYS = {
    "aud",
    "exp",
    "iat",
    "initiator_key",
    "jti",
    "max_frame_bytes",
    "max_session_bytes",
    "protocol",
    "runner_id",
    "runner_key",
    "scopes",
    "sub",
    "transfer_id",
    "v",
}
_X25519_VALIDATION_PRIVATE_KEY = X25519PrivateKey.from_private_bytes(b"\x01" * 32)


@dataclass(frozen=True, slots=True)
class VerifiedRunnerCapability:
    """Validated dispatch authority consumed by one runner Noise session."""

    user_id: str
    runner_id: str
    runner_public_key: str
    initiator_public_key: str
    transfer_id: str
    token_id: str
    scopes: tuple[str, ...]
    issued_at: int
    expires_at: int
    max_frame_bytes: int
    max_session_bytes: int


def _decode_base64url(value: str) -> bytes:
    """Strictly decode one canonical unpadded base64url value."""
    if not isinstance(value, str) or not value:
        raise ValueError("base64url value must not be empty")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("value is not valid base64url") from exc
    if _encode_base64url(decoded) != value:
        raise ValueError("value is not canonical base64url")
    return decoded


def _encode_base64url(value: bytes) -> str:
    """Encode non-empty bytes as canonical unpadded base64url."""
    if not isinstance(value, bytes):
        raise TypeError("value must be bytes")
    if not value:
        raise ValueError("value must not be empty")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signing_key() -> Ed25519PrivateKey:
    """Derive the runner-capability key in a domain separate from other tokens."""
    secret = get_settings().secret_key.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("runner capability signing requires a 32-byte session secret")
    seed = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"yinshi-runner-capability-signing-v1",
        info=b"Ed25519 runner dispatch capabilities",
    ).derive(secret)
    if len(seed) != 32:
        raise RuntimeError("runner capability signing key derivation failed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def runner_capability_signing_public_key() -> str:
    """Return the control plane's canonical raw Ed25519 verification key."""
    public_key = (
        _signing_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    if len(public_key) != 32:
        raise RuntimeError("runner capability public key must contain 32 bytes")
    return _encode_base64url(public_key)


def _validate_x25519_public_key(value: str, name: str) -> str:
    """Validate canonical encoding and reject unusable low-order X25519 keys."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    key_bytes = _decode_base64url(value)
    if len(key_bytes) != 32:
        raise ValueError(f"{name} must contain exactly 32 bytes")
    try:
        public_key = X25519PublicKey.from_public_bytes(key_bytes)
        _X25519_VALIDATION_PRIVATE_KEY.exchange(public_key)
    except ValueError as exc:
        raise ValueError(f"{name} is not a usable X25519 key") from exc
    return value


def _validate_scopes(scopes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize a non-empty unique allowlisted runner scope set."""
    if not isinstance(scopes, (tuple, list)) or not scopes:
        raise ValueError("runner capability scopes must not be empty")
    if any(not isinstance(scope, str) or not scope for scope in scopes):
        raise ValueError("runner capability scopes must be non-empty strings")
    normalized_scopes = tuple(sorted(scopes))
    if len(set(normalized_scopes)) != len(normalized_scopes):
        raise ValueError("runner capability scopes must not contain duplicates")
    if any(scope not in RUNNER_SCOPES for scope in normalized_scopes):
        raise ValueError("runner capability contains an unsupported scope")
    return normalized_scopes


def create_runner_capability(
    *,
    user_id: str,
    runner_id: str,
    runner_public_key: str,
    initiator_public_key: str,
    scopes: tuple[str, ...] | list[str],
    max_session_bytes: int,
    current_time: int | None = None,
) -> tuple[str, VerifiedRunnerCapability]:
    """Issue one five-minute capability bound to both Noise static identities."""
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must not be empty")
    if not isinstance(runner_id, str) or not runner_id:
        raise ValueError("runner_id must not be empty")
    normalized_runner_key = _validate_x25519_public_key(runner_public_key, "runner_public_key")
    normalized_initiator_key = _validate_x25519_public_key(
        initiator_public_key,
        "initiator_public_key",
    )
    normalized_scopes = _validate_scopes(scopes)
    if type(max_session_bytes) is not int:
        raise TypeError("max_session_bytes must be an integer")
    if not RUNNER_SESSION_BYTES_MIN <= max_session_bytes <= RUNNER_SESSION_BYTES_MAX:
        raise ValueError("max_session_bytes is outside the allowed range")

    issued_at = int(time.time()) if current_time is None else current_time
    if type(issued_at) is not int or issued_at < 0:
        raise ValueError("current_time must be a non-negative integer")
    expires_at = issued_at + RUNNER_CAPABILITY_TTL_SECONDS
    transfer_id = str(uuid.uuid4())
    token_id = secrets.token_urlsafe(16)
    claims = VerifiedRunnerCapability(
        user_id=user_id,
        runner_id=runner_id,
        runner_public_key=normalized_runner_key,
        initiator_public_key=normalized_initiator_key,
        transfer_id=transfer_id,
        token_id=token_id,
        scopes=normalized_scopes,
        issued_at=issued_at,
        expires_at=expires_at,
        max_frame_bytes=RUNNER_FRAME_BYTES_MAX,
        max_session_bytes=max_session_bytes,
    )
    payload = {
        "aud": "yinshi-runner",
        "exp": claims.expires_at,
        "iat": claims.issued_at,
        "initiator_key": claims.initiator_public_key,
        "jti": claims.token_id,
        "max_frame_bytes": claims.max_frame_bytes,
        "max_session_bytes": claims.max_session_bytes,
        "protocol": RUNNER_PROTOCOL_VERSION,
        "runner_id": claims.runner_id,
        "runner_key": claims.runner_public_key,
        "scopes": list(claims.scopes),
        "sub": claims.user_id,
        "transfer_id": claims.transfer_id,
        "v": 1,
    }
    encoded_header = _encode_base64url(
        json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _encode_base64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = _signing_key().sign(signing_input.encode("ascii"))
    if len(signature) != 64:
        raise RuntimeError("runner capability signature must contain 64 bytes")
    return f"{signing_input}.{_encode_base64url(signature)}", claims


def _parse_verified_payload(
    payload: object,
    *,
    expected_runner_id: str,
    expected_runner_public_key: str,
    current_time: int,
) -> VerifiedRunnerCapability | None:
    """Validate strict capability claims after Ed25519 verification."""
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return None
    if payload.get("aud") != "yinshi-runner" or payload.get("v") != 1:
        return None
    if payload.get("protocol") != RUNNER_PROTOCOL_VERSION:
        return None
    if payload.get("runner_id") != expected_runner_id:
        return None
    if payload.get("runner_key") != expected_runner_public_key:
        return None

    string_fields = ("sub", "initiator_key", "jti", "transfer_id")
    if any(
        not isinstance(payload.get(field), str) or not payload[field] for field in string_fields
    ):
        return None
    integer_fields = ("iat", "exp", "max_frame_bytes", "max_session_bytes")
    if any(type(payload.get(field)) is not int for field in integer_fields):
        return None
    issued_at = payload["iat"]
    expires_at = payload["exp"]
    if issued_at > current_time + 30 or expires_at <= current_time:
        return None
    if expires_at - issued_at != RUNNER_CAPABILITY_TTL_SECONDS:
        return None
    if payload["max_frame_bytes"] != RUNNER_FRAME_BYTES_MAX:
        return None
    if not RUNNER_SESSION_BYTES_MIN <= payload["max_session_bytes"] <= RUNNER_SESSION_BYTES_MAX:
        return None

    scopes_value = payload.get("scopes")
    if not isinstance(scopes_value, list):
        return None
    try:
        initiator_key = _validate_x25519_public_key(payload["initiator_key"], "initiator_key")
        normalized_scopes = _validate_scopes(scopes_value)
        transfer_id = str(uuid.UUID(payload["transfer_id"]))
        _decode_base64url(payload["jti"])
    except (TypeError, ValueError):
        return None
    if transfer_id != payload["transfer_id"]:
        return None
    return VerifiedRunnerCapability(
        user_id=payload["sub"],
        runner_id=expected_runner_id,
        runner_public_key=expected_runner_public_key,
        initiator_public_key=initiator_key,
        transfer_id=transfer_id,
        token_id=payload["jti"],
        scopes=normalized_scopes,
        issued_at=issued_at,
        expires_at=expires_at,
        max_frame_bytes=payload["max_frame_bytes"],
        max_session_bytes=payload["max_session_bytes"],
    )


def verify_runner_capability(
    token: str,
    *,
    signing_public_key: bytes,
    expected_runner_id: str,
    expected_runner_public_key: str,
    current_time: int | None = None,
) -> VerifiedRunnerCapability | None:
    """Verify one capability against pinned control-plane and runner identities."""
    if not isinstance(token, str) or not token or len(token) > 8_192:
        return None
    if not isinstance(signing_public_key, bytes) or len(signing_public_key) != 32:
        return None
    if not isinstance(expected_runner_id, str) or not expected_runner_id:
        return None
    try:
        normalized_runner_key = _validate_x25519_public_key(
            expected_runner_public_key,
            "expected_runner_public_key",
        )
    except (TypeError, ValueError):
        return None

    segments = token.split(".")
    if len(segments) != 3:
        return None
    encoded_header, encoded_payload, encoded_signature = segments
    try:
        header_bytes = _decode_base64url(encoded_header)
        payload_bytes = _decode_base64url(encoded_payload)
        signature = _decode_base64url(encoded_signature)
        header = json.loads(header_bytes)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if header != _HEADER or len(signature) != 64:
        return None
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(signing_public_key)
        public_key.verify(signature, signing_input)
    except (InvalidSignature, ValueError):
        return None
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    now = int(time.time()) if current_time is None else current_time
    if type(now) is not int or now < 0:
        return None
    return _parse_verified_payload(
        payload,
        expected_runner_id=expected_runner_id,
        expected_runner_public_key=normalized_runner_key,
        current_time=now,
    )
