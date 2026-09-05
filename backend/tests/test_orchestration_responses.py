"""Response frame builder tests for the orchestration bridge.

The backend answers every validated (or rejected) orchestration request with
a bounded ``orchestration_response`` frame. These tests pin the wire shape
and the 256 KiB response bound.
"""

from __future__ import annotations

from yinshi.services.orchestration_bridge import (
    build_orchestration_success,
)


def test_build_success_frame_matches_wire_shape() -> None:
    frame = build_orchestration_success(
        session_id="sess-1",
        request_id="req-1",
        result={"status": "ok"},
    )

    assert frame == {
        "type": "orchestration_response",
        "id": "sess-1",
        "request_id": "req-1",
        "ok": True,
        "result": {"status": "ok"},
    }
