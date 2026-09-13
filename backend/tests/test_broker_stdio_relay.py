"""Tests for one bounded stdio exchange with the authenticated broker."""

from __future__ import annotations

import asyncio
import io
import socket
import tomllib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
)


class FlushBuffer(io.BytesIO):
    """Record whether a framed response becomes visible."""

    def __init__(self) -> None:
        super().__init__()
        self.was_flushed = False

    def flush(self) -> None:
        self.was_flushed = True
        super().flush()


class StandardStream:
    """Expose one binary buffer through the process stdio interface."""

    def __init__(self, initial: bytes = b"") -> None:
        self.buffer = FlushBuffer()
        self.buffer.write(initial)
        self.buffer.seek(0)


def _frame(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


@pytest.fixture
def socket_path() -> Generator[Path, None, None]:
    path = Path("/tmp") / f"yinshi-test-{uuid.uuid4().hex}.sock"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@asynccontextmanager
async def _broker_server(
    socket_path: Path,
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[None]:
    server = await asyncio.start_unix_server(handler, path=socket_path)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


def test_blocking_frame_round_trip_flushes_output() -> None:
    from yinshi.broker_stdio_relay import read_blocking_frame, write_blocking_frame

    body = b'\x00{"signed":true}\xff'
    assert read_blocking_frame(io.BytesIO(_frame(body)), maximum=len(body)) == body

    output = FlushBuffer()
    write_blocking_frame(output, body)
    assert output.getvalue() == _frame(body)
    assert output.was_flushed is True


@pytest.mark.parametrize(
    ("framed", "maximum", "code"),
    [
        (b"", 16, "input_invalid"),
        (b"\x00\x00", 16, "input_invalid"),
        (_frame(b""), 16, "input_invalid"),
        (b"\x00\x00\x00\x05ab", 16, "input_invalid"),
        (_frame(b"ok") + b"x", 16, "input_invalid"),
        ((17).to_bytes(4, "big"), 16, "input_limit"),
    ],
)
def test_blocking_frame_rejects_invalid_input(
    framed: bytes,
    maximum: int,
    code: str,
) -> None:
    from yinshi.broker_stdio_relay import RelayError, read_blocking_frame

    with pytest.raises(RelayError, match=f"^{code}$"):
        read_blocking_frame(io.BytesIO(framed), maximum=maximum)


@pytest.mark.asyncio
async def test_relay_forwards_exact_request_after_half_close(socket_path: Path) -> None:
    from yinshi.broker_stdio_relay import relay_broker_request
    from yinshi.execution_daemons import _read_frame, _write_frame

    request = b'{"request":"signed"}'
    response = b'{"response":"signed"}'
    observed: list[bytes] = []
    handled = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            observed.append(await _read_frame(reader, maximum=BROKER_FRAME_BYTES_MAX))
            _write_frame(writer, response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handled.set()

    async with _broker_server(socket_path, handle):
        actual = await relay_broker_request(
            request,
            broker_socket=socket_path,
            timeout_seconds=1.0,
        )
        await asyncio.wait_for(handled.wait(), timeout=1.0)

    assert observed == [request]
    assert actual == response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "delay_seconds", "code"),
    [
        (b"", 0.0, "transport_unknown"),
        (_frame(b"ok") + b"x", 0.0, "verification_unknown"),
        (
            (BROKER_RESPONSE_BYTES_MAX + 1).to_bytes(4, "big"),
            0.0,
            "verification_unknown",
        ),
        (None, 0.2, "timeout_unknown"),
    ],
)
async def test_relay_classifies_unknown_broker_outcomes(
    socket_path: Path,
    response: bytes | None,
    delay_seconds: float,
    code: str,
) -> None:
    from yinshi.broker_stdio_relay import RelayError, relay_broker_request

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            if response is not None:
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with _broker_server(socket_path, handle):
        with pytest.raises(RelayError, match=f"^{code}$"):
            await relay_broker_request(
                b"SENSITIVE_REQUEST_BYTES",
                broker_socket=socket_path,
                timeout_seconds=0.05,
            )


@pytest.mark.asyncio
async def test_relay_classifies_connect_failure_without_payload(socket_path: Path) -> None:
    from yinshi.broker_stdio_relay import RelayError, relay_broker_request

    with pytest.raises(RelayError, match="^transport_unknown$") as raised:
        await relay_broker_request(
            b"SENSITIVE_REQUEST_BYTES",
            broker_socket=socket_path,
            timeout_seconds=0.1,
        )

    assert "SENSITIVE" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("request_body", [b"", b"x" * (BROKER_FRAME_BYTES_MAX + 1)])
async def test_relay_rejects_invalid_direct_request_without_connecting(
    request_body: bytes,
) -> None:
    from yinshi.broker_stdio_relay import RelayError, relay_broker_request

    connected = False

    async def connect(_path: str | Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        nonlocal connected
        connected = True
        raise AssertionError("connect must not run")

    with pytest.raises(RelayError, match="^input_invalid$"):
        await relay_broker_request(
            request_body,
            broker_socket=Path("/run/yinshi/broker.sock"),
            connect=connect,
            timeout_seconds=0.1,
        )

    assert connected is False


def test_main_writes_only_complete_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import yinshi.broker_stdio_relay as relay

    request = b"signed-request"
    response = b"signed-response"
    stdin = StandardStream(_frame(request))
    stdout = StandardStream()

    async def run_relay(actual: bytes) -> bytes:
        assert actual == request
        return response

    monkeypatch.setattr(relay.sys, "stdin", stdin)
    monkeypatch.setattr(relay.sys, "stdout", stdout)
    monkeypatch.setattr(relay, "_run_relay", run_relay)

    assert relay.main([]) == 0
    assert stdout.buffer.getvalue() == _frame(response)
    assert stdout.buffer.was_flushed is True


def test_main_failure_writes_no_response_or_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import yinshi.broker_stdio_relay as relay

    request = b"SENSITIVE_REQUEST_BYTES"
    stdin = StandardStream(_frame(request))
    stdout = StandardStream()

    async def run_relay(_request: bytes) -> bytes:
        raise relay.RelayError("transport_unknown")

    monkeypatch.setattr(relay.sys, "stdin", stdin)
    monkeypatch.setattr(relay.sys, "stdout", stdout)
    monkeypatch.setattr(relay, "_run_relay", run_relay)

    assert relay.main([]) == 1
    assert stdout.buffer.getvalue() == b""
    assert caplog.messages == ["transport_unknown"]
    assert "SENSITIVE" not in caplog.text


async def _release_queued_connection(listener: socket.socket) -> None:
    connection, _address = await asyncio.to_thread(listener.accept)
    connection.close()


@pytest.mark.asyncio
async def test_timeout_aborts_nonaccepting_socket_without_waiting_for_peer(
    socket_path: Path,
) -> None:
    from yinshi.broker_stdio_relay import RelayError, relay_broker_request

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    task = asyncio.create_task(
        relay_broker_request(
            b"x" * BROKER_FRAME_BYTES_MAX,
            broker_socket=socket_path,
            timeout_seconds=0.01,
        )
    )
    try:
        await asyncio.sleep(0.05)
        completed_without_peer = task.done()
        await _release_queued_connection(listener)
        with pytest.raises(RelayError, match="^timeout_unknown$"):
            await task
    finally:
        listener.close()
        if not task.done():
            task.cancel()

    assert completed_without_peer is True


@pytest.mark.asyncio
async def test_cancellation_aborts_nonaccepting_socket_without_waiting_for_peer(
    socket_path: Path,
) -> None:
    from yinshi.broker_stdio_relay import relay_broker_request

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    task = asyncio.create_task(
        relay_broker_request(
            b"x" * BROKER_FRAME_BYTES_MAX,
            broker_socket=socket_path,
            timeout_seconds=1.0,
        )
    )
    try:
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        completed_without_peer = task.done()
        await _release_queued_connection(listener)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        listener.close()
        if not task.done():
            task.cancel()

    assert completed_without_peer is True


@pytest.mark.parametrize(
    "argument",
    ["-h", "--help", "-he", "value", "--broker-socket", "--broker-socket=/tmp/other"],
)
def test_cli_has_fixed_socket_and_rejects_every_argument(argument: str) -> None:
    from yinshi.broker_stdio_relay import (
        DEFAULT_BROKER_SOCKET,
        RELAY_TIMEOUT_SECONDS,
        build_parser,
    )
    from yinshi.execution_daemons import BROKER_TRANSPORT_TIMEOUT_SECONDS

    assert DEFAULT_BROKER_SOCKET == Path("/run/yinshi/broker.sock")
    assert RELAY_TIMEOUT_SECONDS > BROKER_TRANSPORT_TIMEOUT_SECONDS
    assert build_parser().parse_args([]).__dict__ == {}
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args([argument])
    assert raised.value.code != 0


def test_package_exposes_fixed_relay_entry_point() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["yinshi-broker-stdio-relay"] == (
        "yinshi.broker_stdio_relay:main"
    )
