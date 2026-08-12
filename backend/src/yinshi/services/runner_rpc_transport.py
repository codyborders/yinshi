"""Canonical encrypted RPC fragmentation constants and header codec."""

from __future__ import annotations

import struct

NOISE_CIPHERTEXT_BYTES_MAX = 65_535
NOISE_TAG_BYTES = 16
TRANSPORT_HEADER = struct.Struct(">4sBIII")
TRANSPORT_MAGIC = b"YRP1"
TRANSPORT_REQUEST = 1
TRANSPORT_ACK = 2
TRANSPORT_RESPONSE = 3
TRANSPORT_PULL = 4
TRANSPORT_PAYLOAD_BYTES_MAX = NOISE_CIPHERTEXT_BYTES_MAX - NOISE_TAG_BYTES - TRANSPORT_HEADER.size


def fragment_count(total: int) -> int:
    """Return canonical fragment count for one positive payload length."""
    if type(total) is not int or total < 1:
        raise ValueError("Runner RPC transport total must be positive")
    return max(1, (total + TRANSPORT_PAYLOAD_BYTES_MAX - 1) // TRANSPORT_PAYLOAD_BYTES_MAX)
