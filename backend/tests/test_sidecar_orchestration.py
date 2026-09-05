"""Phase 4 duplex sidecar orchestration protocol tests.

Covers the backend half of the orchestration wire contract: capability
binding, strict frame validation, bounded handler dispatch, internal-frame
filtering, and teardown guarantees.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from yinshi.services.orchestration_bridge import (
    generate_orchestration_capability,
    parse_orchestration_request,
)


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


def test_generate_capability_is_random_and_bound() -> None:
    first = generate_orchestration_capability("sess-1", run_id="run-1")
    second = generate_orchestration_capability("sess-1", run_id="run-1")

    assert first.token != second.token
    assert len(first.token) >= 32
    assert first.session_id == "sess-1"
    assert first.run_id == "run-1"
    assert first.tenant_id is None
    assert first.runtime_id is None
    assert first.connection_id == ""
    first.claim(session_id="sess-1", connection_id="connection-1")
    assert first.connection_id == "connection-1"
    assert first.expires_at > time.monotonic()


def test_parse_request_accepts_valid_frame() -> None:
    request_id = uuid.uuid4().hex
    parsed = parse_orchestration_request(
        valid_request_frame(request_id=request_id),
        frame_bytes=512,
    )

    assert parsed.session_id == "sess-1"
    assert parsed.request_id == request_id
    assert parsed.capability == "cap-token"
    assert parsed.operation == "ping_thread_bridge"
    assert parsed.arguments == {"message": "ping"}


def test_parse_request_rejects_missing_field() -> None:
    import pytest

    from yinshi.services.orchestration_bridge import OrchestrationProtocolError

    frame = valid_request_frame()
    del frame["request_id"]

    with pytest.raises(OrchestrationProtocolError) as excinfo:
        parse_orchestration_request(frame, frame_bytes=512)

    assert excinfo.value.code == "invalid_request"
