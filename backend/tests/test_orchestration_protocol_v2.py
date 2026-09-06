"""Check strict version-two tool identity through the public bridge parser."""

from yinshi.services.orchestration_bridge import parse_orchestration_request


def test_v2_keeps_tool_identity_separate_from_transport_correlation() -> None:
    frame = {
        "type": "orchestration_request",
        "protocol_version": 2,
        "id": "parent-session",
        "request_id": "delivery-one",
        "tool_call_id": "immutable-sdk-call",
        "capability": "query-secret",
        "operation": "spawn_thread",
        "arguments": {"title": "Inspect", "task": "Inspect the implementation."},
    }

    first = parse_orchestration_request(frame, frame_bytes=512)
    second = parse_orchestration_request({**frame, "request_id": "delivery-two"}, frame_bytes=512)

    assert first.tool_call_id == second.tool_call_id == "immutable-sdk-call"
    assert first.request_id != second.request_id
    assert first.protocol_version == second.protocol_version == 2
    assert first.operation == "spawn_thread"


def test_v2_authority_comes_only_from_the_capability() -> None:
    import pytest

    from yinshi.services.orchestration_bridge import (
        OrchestrationProtocolError,
        generate_orchestration_capability,
        verify_orchestration_request,
    )

    capability = generate_orchestration_capability(
        "parent",
        run_id="run",
        tenant_id="tenant",
        runtime_id="workspace-runtime",
        connection_id="socket",
        allowed_operations=frozenset({"spawn_thread"}),
        database_path="/backend/tenant/yinshi.db",
    )
    frame = {
        "type": "orchestration_request",
        "protocol_version": 2,
        "id": "parent",
        "request_id": "request",
        "tool_call_id": "call",
        "capability": capability.token,
        "operation": "spawn_thread",
        "arguments": {},
    }
    parsed = parse_orchestration_request(frame, frame_bytes=256)
    caller = verify_orchestration_request(capability, parsed, connection_id="socket")
    assert (caller.session_id, caller.run_id, caller.tenant_id, caller.tool_call_id) == (
        "parent",
        "run",
        "tenant",
        "call",
    )
    assert caller.database_path == "/backend/tenant/yinshi.db"
    denied = parse_orchestration_request(
        {**frame, "operation": "report_thread_result"}, frame_bytes=256
    )
    with pytest.raises(OrchestrationProtocolError, match="not allowed"):
        verify_orchestration_request(capability, denied, connection_id="socket")
    capability.database_path = None
    with pytest.raises(OrchestrationProtocolError) as missing_binding:
        verify_orchestration_request(capability, parsed, connection_id="socket")
    assert missing_binding.value.code == "capability_invalid"
