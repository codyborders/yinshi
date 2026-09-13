"""Bounded canonical JSON and Ed25519 authentication for broker messages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import TypeAlias, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

BROKER_PROTOCOL_VERSION = "yinshi-broker-v1"
BROKER_FRAME_BYTES_MAX = 65_536
BROKER_RESPONSE_BYTES_MAX = 4_096
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_KEYS = frozenset(
    {
        "broker_incarnation",
        "connection_sequence",
        "database_incarnation",
        "nonce",
        "operation_id",
        "payload",
        "payload_digest",
        "protocol_version",
        "request_type",
        "signature",
    }
)
_RESPONSE_KEYS = frozenset(
    {
        "broker_incarnation",
        "connection_sequence",
        "database_incarnation",
        "error",
        "operation_id",
        "payload_digest",
        "protocol_version",
        "request_nonce",
        "request_type",
        "result",
        "signature",
        "status",
    }
)
_REQUEST_SIGNATURE_DOMAIN = b"YINSHI-BROKER-REQUEST-V1\x00"
_RESPONSE_SIGNATURE_DOMAIN = b"YINSHI-BROKER-RESPONSE-V1\x00"

JsonScalar: TypeAlias = None | bool | int | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class BrokerProtocolError(ValueError):
    """Reject one unauthenticated, malformed, or replayed broker message."""


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    """One verified application-owned logical request."""

    protocol_version: str
    broker_incarnation: str
    database_incarnation: str
    connection_sequence: int
    operation_id: str
    request_type: str
    nonce: str
    payload_digest: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class BrokerResponse:
    """One verified response bound to its complete request identity."""

    protocol_version: str
    broker_incarnation: str
    database_incarnation: str
    connection_sequence: int
    operation_id: str
    request_type: str
    request_nonce: str
    payload_digest: str
    status: str
    error: str | None
    result: dict[str, JsonValue]


def _reject_constant(value: str) -> object:
    raise BrokerProtocolError(f"unsupported JSON constant: {value}")


def _reject_float(value: str) -> object:
    raise BrokerProtocolError("floating-point JSON values are not supported")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerProtocolError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_json(value: object, *, depth: int = 0) -> JsonValue:
    if depth > 8:
        raise BrokerProtocolError("JSON nesting exceeds the protocol limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise BrokerProtocolError("JSON integer exceeds the protocol limit")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 4_096:
            raise BrokerProtocolError("JSON string exceeds the protocol limit")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise BrokerProtocolError("JSON array exceeds the protocol limit")
        return [_validate_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64:
            raise BrokerProtocolError("JSON object exceeds the protocol limit")
        validated: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise BrokerProtocolError("JSON object keys must be non-empty strings")
            if len(key.encode("utf-8")) > 128:
                raise BrokerProtocolError("JSON object key exceeds the protocol limit")
            validated[key] = _validate_json(item, depth=depth + 1)
        return validated
    raise BrokerProtocolError("unsupported JSON value type")


def canonical_json(value: object) -> bytes:
    """Encode a bounded integer-only JSON value in one deterministic form."""
    validated = _validate_json(value)
    encoded = json.dumps(
        validated,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > BROKER_FRAME_BYTES_MAX:
        raise BrokerProtocolError("JSON frame exceeds the protocol limit")
    return encoded


def _parse_canonical_json(frame: bytes, *, maximum: int) -> dict[str, object]:
    if not isinstance(frame, bytes) or not frame or len(frame) > maximum:
        raise BrokerProtocolError("JSON frame size is invalid")
    try:
        text = frame.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BrokerProtocolError("JSON frame is invalid") from exc
    validated = _validate_json(value)
    if not isinstance(validated, dict):
        raise BrokerProtocolError("JSON frame must contain an object")
    if canonical_json(validated) != frame:
        raise BrokerProtocolError("JSON frame is not canonical")
    return cast(dict[str, object], validated)


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: object, *, expected_bytes: int) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise BrokerProtocolError("signature encoding is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise BrokerProtocolError("signature encoding is invalid") from exc
    if len(decoded) != expected_bytes or _encode_base64url(decoded) != value:
        raise BrokerProtocolError("signature encoding is invalid")
    return decoded


def payload_digest(payload: object) -> str:
    """Bind request identity to the exact canonical logical payload."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _validate_token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise BrokerProtocolError(f"{name} is invalid")
    return value


def _unsigned_message(message: dict[str, object]) -> bytes:
    unsigned = dict(message)
    unsigned.pop("signature", None)
    return canonical_json(unsigned)


def create_signed_request(
    *,
    private_key: Ed25519PrivateKey,
    protocol_version: str,
    broker_incarnation: str,
    database_incarnation: str,
    connection_sequence: int,
    operation_id: str,
    request_type: str,
    nonce: str,
    payload: dict[str, JsonValue],
) -> bytes:
    """Create one canonical application request for tests and trusted clients."""
    message: dict[str, object] = {
        "broker_incarnation": broker_incarnation,
        "connection_sequence": connection_sequence,
        "database_incarnation": database_incarnation,
        "nonce": nonce,
        "operation_id": operation_id,
        "payload": payload,
        "payload_digest": payload_digest(payload),
        "protocol_version": protocol_version,
        "request_type": request_type,
    }
    signature = private_key.sign(_REQUEST_SIGNATURE_DOMAIN + canonical_json(message))
    message["signature"] = _encode_base64url(signature)
    return canonical_json(message)


def parse_signed_request(frame: bytes, *, public_key: Ed25519PublicKey) -> BrokerRequest:
    """Authenticate and strictly validate one canonical broker request."""
    message = _parse_canonical_json(frame, maximum=BROKER_FRAME_BYTES_MAX)
    if set(message) != _REQUEST_KEYS:
        raise BrokerProtocolError("broker request schema contains missing or unknown fields")
    signature = _decode_base64url(message.get("signature"), expected_bytes=64)
    try:
        public_key.verify(signature, _REQUEST_SIGNATURE_DOMAIN + _unsigned_message(message))
    except (InvalidSignature, ValueError) as exc:
        raise BrokerProtocolError("broker request signature is invalid") from exc

    if message.get("protocol_version") != BROKER_PROTOCOL_VERSION:
        raise BrokerProtocolError("broker protocol version is invalid")
    broker_incarnation = _validate_token(message.get("broker_incarnation"), "broker incarnation")
    database_incarnation = _validate_token(
        message.get("database_incarnation"), "database incarnation"
    )
    sequence = message.get("connection_sequence")
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise BrokerProtocolError("connection sequence is invalid")
    operation_id = message.get("operation_id")
    if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise BrokerProtocolError("operation ID is invalid")
    request_type = message.get("request_type")
    if not isinstance(request_type, str) or not 1 <= len(request_type) <= 64:
        raise BrokerProtocolError("request type is invalid")
    nonce = _validate_token(message.get("nonce"), "request nonce")
    digest = message.get("payload_digest")
    if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
        raise BrokerProtocolError("payload digest is invalid")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise BrokerProtocolError("request payload must contain an object")
    if payload_digest(payload) != digest:
        raise BrokerProtocolError("request payload digest mismatch")
    return BrokerRequest(
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type=request_type,
        nonce=nonce,
        payload_digest=digest,
        payload=payload,
    )


def create_signed_response(
    request: BrokerRequest,
    *,
    private_key: Ed25519PrivateKey,
    status: str,
    error: str | None,
    result: dict[str, JsonValue],
) -> bytes:
    """Create a bounded broker-signed terminal response."""
    if status not in {"ok", "error"}:
        raise BrokerProtocolError("broker response status is invalid")
    if error is not None and (not isinstance(error, str) or not 1 <= len(error) <= 128):
        raise BrokerProtocolError("broker response error is invalid")
    if status == "ok" and error is not None:
        raise BrokerProtocolError("successful broker response cannot contain an error")
    if status == "error" and error is None:
        raise BrokerProtocolError("failed broker response must contain an error")
    message: dict[str, object] = {
        "broker_incarnation": request.broker_incarnation,
        "connection_sequence": request.connection_sequence,
        "database_incarnation": request.database_incarnation,
        "error": error,
        "operation_id": request.operation_id,
        "payload_digest": request.payload_digest,
        "protocol_version": request.protocol_version,
        "request_nonce": request.nonce,
        "request_type": request.request_type,
        "result": result,
        "status": status,
    }
    signature = private_key.sign(_RESPONSE_SIGNATURE_DOMAIN + canonical_json(message))
    message["signature"] = _encode_base64url(signature)
    response = canonical_json(message)
    if len(response) > BROKER_RESPONSE_BYTES_MAX:
        raise BrokerProtocolError("broker response exceeds the protocol limit")
    return response


def verify_broker_response(
    frame: bytes,
    *,
    public_key: Ed25519PublicKey,
    expected_request: BrokerRequest,
) -> BrokerResponse:
    """Verify a broker response and bind it to one expected request."""
    message = _parse_canonical_json(frame, maximum=BROKER_RESPONSE_BYTES_MAX)
    if set(message) != _RESPONSE_KEYS:
        raise BrokerProtocolError("broker response schema contains missing or unknown fields")
    signature = _decode_base64url(message.get("signature"), expected_bytes=64)
    try:
        public_key.verify(signature, _RESPONSE_SIGNATURE_DOMAIN + _unsigned_message(message))
    except (InvalidSignature, ValueError) as exc:
        raise BrokerProtocolError("broker response signature is invalid") from exc

    request_payload = {
        "broker_incarnation": message.get("broker_incarnation"),
        "connection_sequence": message.get("connection_sequence"),
        "database_incarnation": message.get("database_incarnation"),
        "nonce": message.get("request_nonce"),
        "operation_id": message.get("operation_id"),
        "payload": {},
        "payload_digest": message.get("payload_digest"),
        "protocol_version": message.get("protocol_version"),
        "request_type": message.get("request_type"),
        "signature": "x",
    }
    if message.get("protocol_version") != BROKER_PROTOCOL_VERSION:
        raise BrokerProtocolError("broker protocol version is invalid")
    broker_incarnation = _validate_token(message.get("broker_incarnation"), "broker incarnation")
    database_incarnation = _validate_token(
        message.get("database_incarnation"), "database incarnation"
    )
    sequence = message.get("connection_sequence")
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise BrokerProtocolError("connection sequence is invalid")
    operation_id = request_payload["operation_id"]
    if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise BrokerProtocolError("operation ID is invalid")
    request_type = request_payload["request_type"]
    if not isinstance(request_type, str) or not 1 <= len(request_type) <= 64:
        raise BrokerProtocolError("request type is invalid")
    request_nonce = _validate_token(message.get("request_nonce"), "request nonce")
    digest = message.get("payload_digest")
    if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
        raise BrokerProtocolError("payload digest is invalid")
    status = message.get("status")
    error_value = message.get("error")
    result = message.get("result")
    if not isinstance(status, str) or status not in {"ok", "error"} or not isinstance(result, dict):
        raise BrokerProtocolError("broker response body is invalid")
    if status == "ok":
        if error_value is not None:
            raise BrokerProtocolError("broker response error is invalid")
        error = None
    else:
        if not isinstance(error_value, str) or not 1 <= len(error_value) <= 128:
            raise BrokerProtocolError("broker response error is invalid")
        error = error_value
    response = BrokerResponse(
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type=request_type,
        request_nonce=request_nonce,
        payload_digest=digest,
        status=status,
        error=error,
        result=result,
    )
    response_identity = (
        response.protocol_version,
        response.broker_incarnation,
        response.database_incarnation,
        response.connection_sequence,
        response.operation_id,
        response.request_type,
        response.request_nonce,
        response.payload_digest,
    )
    expected_identity = (
        expected_request.protocol_version,
        expected_request.broker_incarnation,
        expected_request.database_incarnation,
        expected_request.connection_sequence,
        expected_request.operation_id,
        expected_request.request_type,
        expected_request.nonce,
        expected_request.payload_digest,
    )
    if response_identity != expected_identity:
        raise BrokerProtocolError("broker response request identity mismatch")
    return response
