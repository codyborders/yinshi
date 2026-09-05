"""Harmless orchestration operation handler tests.

Phase 4 provides ``ping_thread_bridge`` for a bounded duplex round trip.
The handler does not access storage or make network calls.
"""

from __future__ import annotations

import pytest

from yinshi.services.orchestration_bridge import (
    OrchestrationProtocolError,
    handle_ping_thread_bridge,
)


async def test_ping_handler_echoes_bounded_message() -> None:
    result = await handle_ping_thread_bridge(
        {"message": "round trip"},
        session_id="sess-1",
    )

    assert result["status"] == "ok"
    assert result["echo"] == "round trip"
    assert result["session_bound"] is True


async def test_ping_handler_rejects_oversized_message() -> None:
    with pytest.raises(OrchestrationProtocolError) as excinfo:
        await handle_ping_thread_bridge(
            {"message": "x" * 257},
            session_id="sess-1",
        )

    assert excinfo.value.code == "invalid_arguments"


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "null\x00byte",
        "line\r\nbreak",
        "escape\u001b[31mcode",
        "lone\ud800surrogate",
    ],
)
async def test_ping_handler_rejects_unsafe_unicode(unsafe_message: str) -> None:
    with pytest.raises(OrchestrationProtocolError) as excinfo:
        await handle_ping_thread_bridge(
            {"message": unsafe_message},
            session_id="sess-1",
        )

    assert excinfo.value.code == "invalid_arguments"
