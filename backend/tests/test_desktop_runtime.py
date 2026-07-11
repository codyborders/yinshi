"""Tests desktop helper readiness bytes against the Electron protocol contract."""

from __future__ import annotations

import json

import pytest

from yinshi.desktop_runtime import DESKTOP_HELPER_PROTOCOL_VERSION, serialize_ready_message


def test_serialize_ready_message_emits_strict_pipe_contract() -> None:
    """Readiness bytes should carry only validated protocol fields and one newline."""
    nonce = "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD"

    payload = serialize_ready_message(port=43123, instance_nonce=nonce)

    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert json.loads(payload) == {
        "type": "ready",
        "protocolVersion": DESKTOP_HELPER_PROTOCOL_VERSION,
        "port": 43123,
        "instanceNonce": nonce,
    }


@pytest.mark.parametrize(
    ("port", "nonce", "error"),
    [
        (0, "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD", "port"),
        (65536, "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD", "port"),
        (43123, "short", "instance_nonce"),
        (43123, "contains spaces and remains far too short", "instance_nonce"),
    ],
)
def test_serialize_ready_message_rejects_invalid_values(
    port: int,
    nonce: str,
    error: str,
) -> None:
    """Invalid readiness fields must fail before reaching the inherited pipe."""
    with pytest.raises(ValueError, match=error):
        serialize_ready_message(port=port, instance_nonce=nonce)
