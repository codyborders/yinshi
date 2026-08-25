"""Verify reconnectable terminal multiplexing against a fake sidecar socket.

The tests inspect exact sidecar messages and poll ordered output through the
bounded terminal journal without opening browser or network sockets.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from yinshi.services import terminal_journal
from yinshi.services.terminal_journal import TerminalJournal, TerminalLimitError


def _terminal_ready_line(
    workspace_id: str,
    **overrides: object,
) -> bytes:
    """Encode one sidecar terminal-ready message with caller-selected faults."""
    message: dict[str, object] = {
        "id": workspace_id,
        "type": "terminal_ready",
        "cwd": "/runner/workspace",
        "pid": 123,
        "replay": "ready\r\n",
    }
    message.update(overrides)
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def _feed_successful_terminal_handshake(
    reader: asyncio.StreamReader,
    workspace_id: str,
) -> None:
    """Feed exact initialization and attach-ready messages emitted by the sidecar."""
    reader.feed_data(b'{"type":"init_status","success":true}\n')
    reader.feed_data(_terminal_ready_line(workspace_id))


async def _wait_for(predicate) -> None:
    """Let bounded background cleanup reach one caller-visible state."""
    for _attempt in range(20):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("background terminal cleanup did not complete")


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
    workspace_id = uuid.uuid4().hex
    _feed_successful_terminal_handshake(reader, workspace_id)

    async def connector(socket_path: str):
        assert socket_path == "/tmp/sidecar.sock"
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    start_result = await journal.start(
        user_id="account-1",
        workspace_id=workspace_id,
        socket_path="/tmp/sidecar.sock",
        cwd="/runner/workspace",
        cols=100,
        rows=30,
    )
    terminal_id = start_result.terminal_id
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
        {
            "id": workspace_id,
            "type": "terminal_ready",
            "cwd": "/runner/workspace",
            "pid": 123,
            "replay": "",
        },
        {"type": "terminal_data", "data": "ready\r\n"},
        {"type": "terminal_data", "data": "/runner/workspace\r\n"},
        {"type": "exit", "code": 0},
    )
    assert first.next_sequence == 4
    assert resumed.events == (
        {"type": "terminal_data", "data": "ready\r\n"},
        {"type": "terminal_data", "data": "/runner/workspace\r\n"},
        {"type": "exit", "code": 0},
    )
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
async def test_same_owner_replaces_lost_terminal_channels_without_reaching_limit() -> None:
    """Nine reload-like starts retain only the newest channel for one tab owner."""
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        _feed_successful_terminal_handshake(reader, workspace_id)
        writers.append(writer)
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    terminal_ids: list[str] = []

    for _index in range(9):
        result = await journal.start(
            user_id="account-1",
            workspace_id=workspace_id,
            socket_path="/tmp/sidecar.sock",
            cwd="/runner/workspace",
            cols=100,
            rows=30,
            owner_id="f" * 32,
        )
        terminal_ids.append(result.terminal_id)

    assert len(set(terminal_ids)) == 9
    await _wait_for(lambda: all(writer.closed for writer in writers[:-1]))
    assert not writers[-1].closed
    assert [message["type"] for message in writers[0].messages] == [
        "terminal_attach",
        "terminal_detach",
    ]

    await journal.close_all()


@pytest.mark.asyncio
async def test_distinct_terminal_owners_still_reach_account_limit() -> None:
    """A ninth distinct tab owner cannot exceed the account terminal cap."""
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        _feed_successful_terminal_handshake(reader, workspace_id)
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    for index in range(8):
        await journal.start(
            user_id="account-1",
            workspace_id=workspace_id,
            socket_path="/tmp/sidecar.sock",
            cwd="/runner/workspace",
            cols=100,
            rows=30,
            owner_id=f"{index:032x}",
        )

    with pytest.raises(TerminalLimitError, match="terminal limit reached"):
        await journal.start(
            user_id="account-1",
            workspace_id=workspace_id,
            socket_path="/tmp/sidecar.sock",
            cwd="/runner/workspace",
            cols=100,
            rows=30,
            owner_id="f" * 32,
        )

    await journal.close_all()


@pytest.mark.asyncio
async def test_legacy_terminal_starts_retain_account_limit() -> None:
    """Owner-free clients retain the existing eight-terminal account cap."""
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        _feed_successful_terminal_handshake(reader, workspace_id)
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
    }
    for _index in range(8):
        await journal.start(**common)

    with pytest.raises(TerminalLimitError, match="terminal limit reached"):
        await journal.start(**common)

    await journal.close_all()


@pytest.mark.asyncio
async def test_failed_owner_replacement_preserves_existing_terminal() -> None:
    """A failed replacement leaves the prior tab terminal attached and writable."""
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        writers.append(writer)
        if len(writers) == 1:
            _feed_successful_terminal_handshake(reader, workspace_id)
        else:
            reader.feed_data(b'{"type":"init_status","success":false}\n')
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    existing = await journal.start(
        user_id="account-1",
        workspace_id=workspace_id,
        socket_path="/tmp/sidecar.sock",
        cwd="/runner/workspace",
        cols=100,
        rows=30,
        owner_id="e" * 32,
    )

    with pytest.raises(ConnectionError, match="initialization failed"):
        await journal.start(
            user_id="account-1",
            workspace_id=workspace_id,
            socket_path="/tmp/sidecar.sock",
            cwd="/runner/workspace",
            cols=100,
            rows=30,
            owner_id="e" * 32,
        )

    await journal.input(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=existing.terminal_id,
        data="pwd\r",
    )
    assert not writers[0].closed
    assert writers[1].closed
    assert writers[0].messages[-1]["type"] == "terminal_input"

    await journal.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attach_reply", "message"),
    [
        (
            b'{"id":"workspace","type":"error","error":"attach failed"}\n',
            "terminal attach failed",
        ),
        (b"", "before terminal ready"),
    ],
)
async def test_failed_attach_reply_preserves_existing_owner_terminal(
    attach_reply: bytes,
    message: str,
) -> None:
    """An attach error or disconnect never commits over the prior owner channel."""
    readers: list[asyncio.StreamReader] = []
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        readers.append(reader)
        writers.append(writer)
        reader.feed_data(b'{"type":"init_status","success":true}\n')
        if len(readers) == 1:
            reader.feed_data(_terminal_ready_line(workspace_id, replay=""))
        elif attach_reply:
            reader.feed_data(attach_reply)
        else:
            reader.feed_eof()
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "a" * 32,
    }
    existing = await journal.start(**common)

    with pytest.raises(ConnectionError, match=message):
        await journal.start(**common)

    await journal.input(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=existing.terminal_id,
        data="echo retained\r",
    )
    assert not writers[0].closed
    assert writers[1].closed
    assert writers[0].messages[-1]["type"] == "terminal_input"

    await journal.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "b" * 32),
        ("type", "terminal_data"),
        ("cwd", ""),
        ("cwd", "relative/path"),
        ("pid", 0),
        ("pid", True),
        ("replay", 42),
        ("unexpected", "field"),
    ],
)
async def test_malformed_terminal_ready_preserves_existing_owner_terminal(
    field: str,
    value: object,
) -> None:
    """A malformed or mismatched ready message cannot replace a live terminal."""
    readers: list[asyncio.StreamReader] = []
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        readers.append(reader)
        writers.append(writer)
        reader.feed_data(b'{"type":"init_status","success":true}\n')
        if len(readers) == 1:
            reader.feed_data(_terminal_ready_line(workspace_id, replay=""))
        else:
            reader.feed_data(_terminal_ready_line(workspace_id, **{field: value}))
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "8" * 32,
    }
    existing = await journal.start(**common)

    with pytest.raises(ConnectionError, match="terminal ready"):
        await journal.start(**common)

    await journal.input(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=existing.terminal_id,
        data="echo retained\r",
    )
    assert not writers[0].closed
    assert writers[1].closed

    await journal.close_all()


@pytest.mark.asyncio
async def test_terminal_ready_timeout_preserves_existing_owner_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar that never confirms attach cannot replace the prior owner channel."""
    readers: list[asyncio.StreamReader] = []
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        readers.append(reader)
        writers.append(writer)
        reader.feed_data(b'{"type":"init_status","success":true}\n')
        if len(readers) == 1:
            reader.feed_data(_terminal_ready_line(workspace_id, replay=""))
        return reader, writer

    monkeypatch.setattr(terminal_journal, "_TERMINAL_ATTACH_TIMEOUT_S", 0.01)
    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "9" * 32,
    }
    existing = await journal.start(**common)

    with pytest.raises(TimeoutError):
        await journal.start(**common)

    await journal.input(
        user_id="account-1",
        workspace_id=workspace_id,
        terminal_id=existing.terminal_id,
        data="echo retained\r",
    )
    assert not writers[0].closed
    assert writers[1].closed

    await journal.close_all()


@pytest.mark.asyncio
async def test_concurrent_same_owner_starts_are_serialized() -> None:
    """Concurrent reload starts replace in order and retain one owner channel."""
    writers: list[FakeWriter] = []
    release_first_replacement = asyncio.Event()
    workspace_id = uuid.uuid4().hex

    async def connector(_socket_path: str):
        call_index = len(writers)
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        writers.append(writer)
        if call_index == 1:
            await release_first_replacement.wait()
        _feed_successful_terminal_handshake(reader, workspace_id)
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "d" * 32,
    }
    initial = await journal.start(**common)
    first_task = asyncio.create_task(journal.start(**common))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(journal.start(**common))
    await asyncio.sleep(0)

    assert len(writers) == 2
    release_first_replacement.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert len({initial.terminal_id, first.terminal_id, second.terminal_id}) == 3
    assert first.replaced_terminal_id == initial.terminal_id
    assert second.replaced_terminal_id == first.terminal_id
    assert writers[0].closed
    assert writers[1].closed
    assert not writers[2].closed

    await journal.close_all()


@pytest.mark.asyncio
async def test_cancelled_replacement_does_not_break_committed_channel() -> None:
    """Cancellation during old detach leaves the committed replacement usable."""
    detach_started = asyncio.Event()
    release_detach = asyncio.Event()
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    class BlockingDetachWriter(FakeWriter):
        async def drain(self) -> None:
            if self.messages[-1].get("type") == "terminal_detach":
                detach_started.set()
                await release_detach.wait()
            await super().drain()

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = BlockingDetachWriter() if not writers else FakeWriter()
        _feed_successful_terminal_handshake(reader, workspace_id)
        writers.append(writer)
        return reader, writer

    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "c" * 32,
    }
    await journal.start(**common)
    replacement_task = asyncio.create_task(journal.start(**common))
    await detach_started.wait()

    replacement_task.cancel()
    release_detach.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement_task

    await _wait_for(lambda: writers[0].closed)
    assert not writers[1].closed

    await journal.close_all()


@pytest.mark.asyncio
async def test_blocked_old_detach_is_forced_closed_without_holding_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck old socket cleanup remains bounded after replacement commits."""
    detach_started = asyncio.Event()
    never_release = asyncio.Event()
    writers: list[FakeWriter] = []
    workspace_id = uuid.uuid4().hex

    class StuckDetachWriter(FakeWriter):
        async def drain(self) -> None:
            if self.messages[-1].get("type") == "terminal_detach":
                detach_started.set()
                await never_release.wait()
            await super().drain()

    async def connector(_socket_path: str):
        reader = asyncio.StreamReader()
        writer = StuckDetachWriter() if not writers else FakeWriter()
        _feed_successful_terminal_handshake(reader, workspace_id)
        writers.append(writer)
        return reader, writer

    monkeypatch.setattr(terminal_journal, "_TERMINAL_CLOSE_TIMEOUT_S", 0.01)
    journal = TerminalJournal(
        connector=connector,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    common = {
        "user_id": "account-1",
        "workspace_id": workspace_id,
        "socket_path": "/tmp/sidecar.sock",
        "cwd": "/runner/workspace",
        "cols": 100,
        "rows": 30,
        "owner_id": "b" * 32,
    }
    first = await journal.start(**common)
    second = await journal.start(**common)
    await detach_started.wait()
    third = await journal.start(**common)

    assert second.replaced_terminal_id == first.terminal_id
    assert third.replaced_terminal_id == second.terminal_id
    await asyncio.sleep(0.02)
    assert writers[0].closed

    await journal.close_all()


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
