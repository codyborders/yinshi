"""Desktop helper process protocol and loopback runtime primitives."""

from __future__ import annotations

import json
import re

DESKTOP_HELPER_PROTOCOL_VERSION = 1
_INSTANCE_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def serialize_ready_message(*, port: int, instance_nonce: str) -> bytes:
    """Return one validated newline-delimited readiness message."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    if not isinstance(instance_nonce, str):
        raise TypeError("instance_nonce must be a string")
    if _INSTANCE_NONCE_PATTERN.fullmatch(instance_nonce) is None:
        raise ValueError("instance_nonce must be 32-128 base64url characters")

    payload = {
        "type": "ready",
        "protocolVersion": DESKTOP_HELPER_PROTOCOL_VERSION,
        "port": port,
        "instanceNonce": instance_nonce,
    }
    message = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{message}\n".encode("ascii")
