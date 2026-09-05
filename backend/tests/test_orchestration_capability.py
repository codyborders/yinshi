"""Capability verification tests for the orchestration bridge.

Each test drives one fail-closed rule of ``verify_orchestration_request``:
token identity, expiration, session binding, and connection binding.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from yinshi.services.orchestration_bridge import (
    OrchestrationProtocolError,
    generate_orchestration_capability,
    parse_orchestration_request,
    verify_orchestration_request,
)


def test_capability_repr_hides_token() -> None:
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    rendered = repr(capability)

    assert capability.token
    assert capability.token not in rendered


def valid_request_frame(
    session_id: str = "sess-1",
    request_id: str | None = None,
    capability: str = "cap-token",
    operation: str = "ping_thread_bridge",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "orchestration_request",
        "id": session_id,
        "request_id": request_id or uuid.uuid4().hex,
        "capability": capability,
        "operation": operation,
        "arguments": arguments if arguments is not None else {"message": "ping"},
    }


def test_request_repr_hides_token() -> None:
    parsed = parse_orchestration_request(valid_request_frame(), frame_bytes=512)
    assert parsed.capability not in repr(parsed)


def test_non_ascii_token_is_rejected_safely() -> None:
    capability = generate_orchestration_capability("sess-1", connection_id="conn")
    with pytest.raises(OrchestrationProtocolError):
        parsed = parse_orchestration_request(
            valid_request_frame(capability="\u2603"), frame_bytes=512
        )
        verify_orchestration_request(capability, parsed, connection_id="conn")


def test_verify_rejects_wrong_token() -> None:
    capability = generate_orchestration_capability("sess-1", run_id="run-1", connection_id="conn")
    parsed = parse_orchestration_request(
        valid_request_frame(capability="forged-token"), frame_bytes=512
    )

    with pytest.raises(OrchestrationProtocolError) as excinfo:
        verify_orchestration_request(capability, parsed, connection_id="conn")

    assert excinfo.value.code == "capability_invalid"


def test_verify_rejects_expired_capability() -> None:
    capability = generate_orchestration_capability(
        "sess-1", run_id="run-1", connection_id="conn", ttl_seconds=-1.0
    )
    parsed = parse_orchestration_request(
        valid_request_frame(capability=capability.token), frame_bytes=512
    )

    with pytest.raises(OrchestrationProtocolError) as excinfo:
        verify_orchestration_request(capability, parsed, connection_id="conn")

    assert excinfo.value.code == "capability_expired"


def test_verify_rejects_session_mismatch() -> None:
    capability = generate_orchestration_capability("sess-1", run_id="run-1", connection_id="conn")
    parsed = parse_orchestration_request(
        valid_request_frame(session_id="sess-other", capability=capability.token),
        frame_bytes=512,
    )

    with pytest.raises(OrchestrationProtocolError) as excinfo:
        verify_orchestration_request(capability, parsed, connection_id="conn")

    assert excinfo.value.code == "session_mismatch"
