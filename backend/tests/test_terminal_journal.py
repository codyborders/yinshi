"""Verify reconnectable terminal multiplexing against a fake sidecar socket.

The tests inspect exact sidecar messages and poll ordered output through the
bounded terminal journal without opening browser or network sockets.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from yinshi.services.terminal_journal import TerminalJournal


class FakeWriter:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        assert not self.closed
        self.messages.append(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_terminal_journal_forwards_input_and_reconnects_output_cursor() -> None:
    """Input reaches one sidecar terminal and output resumes by sequence."""
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    reader.feed_data(b'{"type":"init_status","success":true}\n')

    async def connector(socket_path: str):
        assert socket_path == "/tmp/sidecar.sock"
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    workspace_id = uuid.uuid4().hex
    terminal_id = await journal.start(
        user_id="account-1",
        workspace_id=workspace_id,
        socket_path="/tmp/sidecar.sock",
        cwd="/runner/workspace",
        cols=100,
        rows=30,
    )
    await journal.input(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=terminal_id,
        data="pwd\r",
    )
    reader.feed_data(b'{"type":"terminal_data","data":"/runner/workspace\\r\\n"}\n')
    reader.feed_data(b'{"type":"exit","code":0}\n')
    await asyncio.sleep(0)

    first = await journal.events(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=terminal_id,
        next_sequence=0,
    )
    resumed = await journal.events(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=terminal_id,
        next_sequence=1,
    )

    assert first.events == (
        {"type": "terminal_data", "data": "/runner/workspace\r\n"},
        {"type": "exit", "code": 0},
    )
    assert first.next_sequence == 2
    assert resumed.events == ({"type": "exit", "code": 0},)
    assert writer.messages[0] == {
        "type": "terminal_attach",
        "id": workspace_id,
        "options": {
            "workspaceId": workspace_id,
            "cwd": "/runner/workspace",
            "cols": 100,
            "rows": 30,
            "scrollbackLines": 1000,
        },
    }
    assert writer.messages[1] == {
        "type": "terminal_input",
        "id": workspace_id,
        "data": "pwd\r",
    }

    await journal.close(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=terminal_id,
    )
    await journal.close(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=terminal_id,
    )
    assert writer.closed


@pytest.mark.asyncio
async def test_terminal_journal_rejects_start_after_shutdown() -> None:
    """A concurrent shutdown cannot leave a newly attached terminal untracked."""
    connector_called = False

    async def connector(socket_path: str):
        nonlocal connector_called
        connector_called = True
        raise AssertionError(f"unexpected connector call for {socket_path}")

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    await journal.close_all()

    with pytest.raises(RuntimeError, match="closing"):
        await journal.start(
            user_id="account-1",
            workspace_id=uuid.uuid4().hex,
            socket_path="/tmp/sidecar.sock",
            cwd="/tmp/workspace",
            cols=80,
            rows=24,
        )
    assert not connector_called
