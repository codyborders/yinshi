"""Bounded multiplexed terminal channels for reconnectable JSON transports."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from yinshi.config import get_settings

_TERMINAL_ID_LENGTH = 32
_OUTPUT_EVENT_BYTES_MAX = 24_000
_OUTPUT_BUFFER_BYTES_MAX = 1_048_576
_OUTPUT_EVENT_COUNT_MAX = 1_000
_TERMINALS_PER_USER_MAX = 8
_EVENT_BATCH_BYTES_MAX = 48_000
_EVENT_BATCH_COUNT_MAX = 100


class TerminalWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


TerminalConnector = Callable[[str], Awaitable[tuple[asyncio.StreamReader, TerminalWriter]]]


class TerminalNotFoundError(LookupError):
    """Terminal does not exist for the selected account and workspace."""


class TerminalLimitError(RuntimeError):
    """Account already owns the maximum number of live terminals."""


class TerminalCursorExpiredError(RuntimeError):
    """Requested output cursor has fallen behind the bounded memory journal."""


@dataclass(frozen=True, slots=True)
class TerminalEventBatch:
    terminal_id: str
    events: tuple[dict[str, Any], ...]
    next_sequence: int
    closed: bool


@dataclass(slots=True)
class _TerminalChannel:
    id: str
    user_id: str
    workspace_id: str
    reader: asyncio.StreamReader
    writer: TerminalWriter
    attach_options: dict[str, Any]
    events: list[tuple[int, dict[str, Any], int]] = field(default_factory=list)
    next_sequence: int = 0
    event_bytes: int = 0
    closed: bool = False
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task[None] | None = None
    activity_monotonic: float = field(default_factory=time.monotonic)
    output_available: asyncio.Event = field(default_factory=asyncio.Event)


async def _connect_sidecar(
    socket_path: str,
) -> tuple[asyncio.StreamReader, TerminalWriter]:
    reader, writer = await asyncio.open_unix_connection(
        socket_path,
        limit=8 * 1024 * 1024,
    )
    return reader, writer


class TerminalJournal:
    """Own sidecar terminal sockets while encrypted clients poll bounded output."""

    def __init__(
        self,
        *,
        connector: TerminalConnector | None = None,
        scrollback_lines: int | None = None,
        idle_seconds: int | None = None,
    ) -> None:
        selected_connector = connector or _connect_sidecar
        if not callable(selected_connector):
            raise TypeError("terminal connector must be callable")
        selected_scrollback_lines = (
            get_settings().terminal_scrollback_lines
            if scrollback_lines is None
            else scrollback_lines
        )
        if (
            type(selected_scrollback_lines) is not int
            or not 100 <= selected_scrollback_lines <= 100_000
        ):
            raise ValueError("terminal scrollback_lines must be between 100 and 100000")
        selected_idle_seconds = (
            get_settings().terminal_keepalive_s if idle_seconds is None else idle_seconds
        )
        if type(selected_idle_seconds) is not int or not 60 <= selected_idle_seconds <= 86_400:
            raise ValueError("terminal idle_seconds must be between 60 and 86400")
        self._connector = selected_connector
        self._scrollback_lines = selected_scrollback_lines
        self._idle_seconds = selected_idle_seconds
        self._channels: dict[str, _TerminalChannel] = {}
        self._starting_by_user: dict[str, int] = {}
        self._channels_lock = asyncio.Lock()
        self._closing = False

    async def start(
        self,
        *,
        user_id: str,
        workspace_id: str,
        socket_path: str,
        cwd: str,
        cols: int,
        rows: int,
    ) -> str:
        """Attach one terminal channel after enforcing per-account limits."""
        self._validate_identity(user_id, "user_id")
        self._validate_resource_id(workspace_id, "workspace_id")
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError("socket_path must not be empty")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must not be empty")
        self._validate_size(cols, rows)
        await self._reap_idle_channels()
        async with self._channels_lock:
            if self._closing:
                raise RuntimeError("terminal journal is closing")
            closed_terminal_ids = tuple(
                terminal_id
                for terminal_id, channel in self._channels.items()
                if channel.user_id == user_id and channel.closed
            )
            for terminal_id in closed_terminal_ids:
                self._channels.pop(terminal_id, None)
            active_count = sum(
                1
                for channel in self._channels.values()
                if channel.user_id == user_id and not channel.closed
            )
            starting_count = self._starting_by_user.get(user_id, 0)
            if active_count + starting_count >= _TERMINALS_PER_USER_MAX:
                raise TerminalLimitError("terminal limit reached")
            self._starting_by_user[user_id] = starting_count + 1

        try:
            reader, writer = await self._connector(socket_path)
        except BaseException:
            await self._release_start_reservation(user_id)
            raise
        try:
            init_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not init_line:
                raise ConnectionError("sidecar disconnected before terminal attach")
            init_message = self._decode_message(init_line)
            if init_message.get("type") != "init_status" or not init_message.get("success"):
                raise ConnectionError("sidecar terminal initialization failed")
            terminal_id = uuid.uuid4().hex
            attach_options = {
                "workspaceId": workspace_id,
                "cwd": cwd,
                "cols": cols,
                "rows": rows,
                "scrollbackLines": self._scrollback_lines,
            }
            channel = _TerminalChannel(
                id=terminal_id,
                user_id=user_id,
                workspace_id=workspace_id,
                reader=reader,
                writer=writer,
                attach_options=attach_options,
            )
            await self._send(
                channel,
                {"type": "terminal_attach", "id": workspace_id, "options": attach_options},
            )
            async with self._channels_lock:
                if self._closing:
                    raise RuntimeError("terminal journal is closing")
                if terminal_id in self._channels:
                    raise RuntimeError("terminal ID collision")
                self._channels[terminal_id] = channel
                channel.reader_task = asyncio.create_task(
                    self._read_output(channel),
                    name=f"terminal-journal-{terminal_id}",
                )
            return terminal_id
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        finally:
            await self._release_start_reservation(user_id)

    async def input(
        self,
        *,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
        data: str,
    ) -> None:
        """Forward bounded terminal input to one exact account channel."""
        channel = await self._channel(user_id, workspace_id, terminal_id)
        if not isinstance(data, str) or not data or len(data.encode("utf-8")) > 16_384:
            raise ValueError("terminal input has an invalid length")
        await self._send(
            channel,
            {"type": "terminal_input", "id": workspace_id, "data": data},
        )

    async def resize(
        self,
        *,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
        cols: int,
        rows: int,
    ) -> None:
        """Resize one terminal using bounded positive dimensions."""
        self._validate_size(cols, rows)
        channel = await self._channel(user_id, workspace_id, terminal_id)
        await self._send(
            channel,
            {
                "type": "terminal_resize",
                "id": workspace_id,
                "cols": cols,
                "rows": rows,
            },
        )

    async def restart(
        self,
        *,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Restart one sidecar terminal with its original reviewed options."""
        channel = await self._channel(user_id, workspace_id, terminal_id)
        await self._send(
            channel,
            {
                "type": "terminal_restart",
                "id": workspace_id,
                "options": channel.attach_options,
            },
        )

    async def events(
        self,
        *,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
        next_sequence: int,
        wait_seconds: float = 10.0,
    ) -> TerminalEventBatch:
        """Return one contiguous output page from a reconnect cursor."""
        if type(next_sequence) is not int or next_sequence < 0:
            raise ValueError("next_sequence must be a non-negative integer")
        if not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 10:
            raise ValueError("wait_seconds must be between 0 and 10")
        channel = await self._channel(user_id, workspace_id, terminal_id, allow_closed=True)
        if next_sequence == channel.next_sequence and not channel.closed and wait_seconds:
            try:
                await asyncio.wait_for(
                    channel.output_available.wait(),
                    timeout=float(wait_seconds),
                )
            except TimeoutError:
                pass
            channel.output_available.clear()
        earliest_sequence = channel.events[0][0] if channel.events else channel.next_sequence
        if next_sequence < earliest_sequence:
            raise TerminalCursorExpiredError("terminal output cursor expired")
        if next_sequence > channel.next_sequence:
            raise ValueError("terminal output cursor is ahead of the journal")

        selected_events: list[dict[str, Any]] = []
        selected_bytes = 0
        cursor = next_sequence
        for sequence, event, event_bytes in channel.events:
            if sequence < cursor:
                continue
            if sequence != cursor:
                raise RuntimeError("terminal journal sequence is not contiguous")
            if selected_events and selected_bytes + event_bytes > _EVENT_BATCH_BYTES_MAX:
                break
            selected_events.append(event)
            selected_bytes += event_bytes
            cursor += 1
            if len(selected_events) >= _EVENT_BATCH_COUNT_MAX:
                break
        channel.activity_monotonic = time.monotonic()
        return TerminalEventBatch(
            terminal_id=terminal_id,
            events=tuple(selected_events),
            next_sequence=cursor,
            closed=channel.closed,
        )

    async def close(
        self,
        *,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Detach and remove one terminal channel idempotently."""
        try:
            channel = await self._channel(
                user_id,
                workspace_id,
                terminal_id,
                allow_closed=True,
            )
        except TerminalNotFoundError:
            return
        await self._close_channel(channel, detach=True)
        async with self._channels_lock:
            self._channels.pop(terminal_id, None)

    async def _release_start_reservation(self, user_id: str) -> None:
        """Release one in-flight terminal slot after connection succeeds or fails."""
        async with self._channels_lock:
            count = self._starting_by_user.get(user_id, 0)
            if count <= 0:
                raise RuntimeError("terminal start reservation is missing")
            if count == 1:
                self._starting_by_user.pop(user_id, None)
            else:
                self._starting_by_user[user_id] = count - 1

    async def _reap_idle_channels(self) -> None:
        """Close channels that no client has polled or written within the idle limit."""
        cutoff = time.monotonic() - self._idle_seconds
        async with self._channels_lock:
            stale_channels = tuple(
                channel
                for channel in self._channels.values()
                if channel.activity_monotonic < cutoff
            )
            for channel in stale_channels:
                self._channels.pop(channel.id, None)
        for channel in stale_channels:
            await self._close_channel(channel, detach=True)

    async def close_all(self) -> None:
        """Detach all channels before application shutdown."""
        async with self._channels_lock:
            self._closing = True
            channels = tuple(self._channels.values())
            self._channels.clear()
        for channel in channels:
            await self._close_channel(channel, detach=True)

    async def _read_output(self, channel: _TerminalChannel) -> None:
        try:
            while True:
                line = await channel.reader.readline()
                if not line:
                    self._append_event(
                        channel,
                        {"type": "error", "error": "Terminal runtime disconnected"},
                    )
                    return
                message = self._decode_message(line)
                if message.get("type") == "init_status":
                    continue
                for event in self._split_event(message):
                    self._append_event(channel, event)
        except (ConnectionError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._append_event(
                channel,
                {"type": "error", "error": "Terminal runtime disconnected"},
            )
        finally:
            channel.closed = True
            channel.output_available.set()
            channel.writer.close()
            try:
                await channel.writer.wait_closed()
            except OSError:
                pass

    def _append_event(self, channel: _TerminalChannel, event: dict[str, Any]) -> None:
        serialized = json.dumps(event, separators=(",", ":"), sort_keys=True)
        event_bytes = len(serialized.encode("utf-8"))
        if event_bytes > _OUTPUT_EVENT_BYTES_MAX:
            raise ValueError("terminal event exceeds the byte limit")
        sequence = channel.next_sequence
        channel.next_sequence += 1
        channel.events.append((sequence, event, event_bytes))
        channel.event_bytes += event_bytes
        while (
            channel.event_bytes > _OUTPUT_BUFFER_BYTES_MAX
            or len(channel.events) > _OUTPUT_EVENT_COUNT_MAX
        ):
            _sequence, _event, removed_bytes = channel.events.pop(0)
            channel.event_bytes -= removed_bytes
        assert channel.event_bytes >= 0
        channel.activity_monotonic = time.monotonic()
        channel.output_available.set()

    @staticmethod
    def _split_event(event: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        event_type = event.get("type")
        if event_type == "terminal_ready" and isinstance(event.get("replay"), str):
            replay = event["replay"]
            ready_event = {key: value for key, value in event.items() if key != "replay"}
            ready_event["replay"] = ""
            if not replay:
                return (ready_event,)
            replay_events = TerminalJournal._split_terminal_data(replay)
            return (ready_event, *replay_events)
        if event_type != "terminal_data" or not isinstance(event.get("data"), str):
            return (event,)
        return TerminalJournal._split_terminal_data(event["data"])

    @staticmethod
    def _split_terminal_data(data: str) -> tuple[dict[str, Any], ...]:
        chunks: list[dict[str, Any]] = []
        current = ""
        current_bytes = 0
        for character in data:
            character_bytes = len(character.encode("utf-8"))
            if current and current_bytes + character_bytes > 20_000:
                chunks.append({"type": "terminal_data", "data": current})
                current = ""
                current_bytes = 0
            current += character
            current_bytes += character_bytes
        if current or not chunks:
            chunks.append({"type": "terminal_data", "data": current})
        return tuple(chunks)

    async def _channel(
        self,
        user_id: str,
        workspace_id: str,
        terminal_id: str,
        *,
        allow_closed: bool = False,
    ) -> _TerminalChannel:
        self._validate_identity(user_id, "user_id")
        self._validate_resource_id(workspace_id, "workspace_id")
        self._validate_resource_id(terminal_id, "terminal_id")
        async with self._channels_lock:
            channel = self._channels.get(terminal_id)
        if (
            channel is None
            or channel.user_id != user_id
            or channel.workspace_id != workspace_id
            or (channel.closed and not allow_closed)
        ):
            raise TerminalNotFoundError("terminal not found")
        return channel

    @staticmethod
    async def _send(channel: _TerminalChannel, message: dict[str, Any]) -> None:
        if channel.closed:
            raise TerminalNotFoundError("terminal is closed")
        async with channel.write_lock:
            channel.writer.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            )
            await channel.writer.drain()
        channel.activity_monotonic = time.monotonic()

    async def _close_channel(self, channel: _TerminalChannel, *, detach: bool) -> None:
        if detach and not channel.closed:
            try:
                await self._send(
                    channel,
                    {"type": "terminal_detach", "id": channel.workspace_id},
                )
            except (ConnectionError, OSError, TerminalNotFoundError):
                pass
        channel.closed = True
        channel.output_available.set()
        if channel.reader_task is not None and not channel.reader_task.done():
            channel.reader_task.cancel()
            try:
                await channel.reader_task
            except asyncio.CancelledError:
                pass
        channel.writer.close()
        try:
            await channel.writer.wait_closed()
        except OSError:
            pass

    @staticmethod
    def _decode_message(line: bytes) -> dict[str, Any]:
        if not isinstance(line, bytes) or not line or len(line) > 8 * 1024 * 1024:
            raise ValueError("sidecar terminal message has an invalid length")
        message = json.loads(line.decode("utf-8", errors="strict"))
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ValueError("sidecar terminal message must be a typed object")
        return message

    @staticmethod
    def _validate_resource_id(value: str, name: str) -> None:
        if not isinstance(value, str) or len(value) != _TERMINAL_ID_LENGTH:
            raise ValueError(f"{name} must contain exactly 32 characters")
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{name} must be lowercase hexadecimal")

    @staticmethod
    def _validate_identity(value: str, name: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError(f"{name} has an invalid length")

    @staticmethod
    def _validate_size(cols: int, rows: int) -> None:
        if type(cols) is not int or not 2 <= cols <= 500:
            raise ValueError("terminal cols must be between 2 and 500")
        if type(rows) is not int or not 2 <= rows <= 500:
            raise ValueError("terminal rows must be between 2 and 500")
