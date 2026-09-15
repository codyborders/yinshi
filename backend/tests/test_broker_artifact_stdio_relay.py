"""Check opaque bounded streaming between stdio and fixed artifact upload socket."""

from __future__ import annotations

import asyncio
import io
import tomllib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from yinshi.services.broker_protocol import BROKER_RESPONSE_BYTES_MAX


def _frame(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


def _reader(content: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(content)
    if eof:
        reader.feed_eof()
    return reader


def test_control_reader_accepts_fragmented_unbuffered_reads() -> None:
    from yinshi.broker_artifact_stdio_relay import read_blocking_control_frame

    class Fragmented(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 2))

    control = b"signed-control"
    stream = Fragmented(_frame(control) + b"artifact-bytes")
    assert read_blocking_control_frame(stream) == control
    assert stream.read() == b"artifact-bytes"


@pytest.fixture
def socket_path() -> Generator[Path, None, None]:
    path = Path("/tmp") / f"yinshi-artifact-relay-{uuid.uuid4().hex}.sock"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@asynccontextmanager
async def _server(
    path: Path,
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[None]:
    server = await asyncio.start_unix_server(handler, path=path)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_relay_streams_control_and_artifacts_then_returns_one_response(
    socket_path: Path,
) -> None:
    from yinshi.broker_artifact_stdio_relay import relay_artifact_upload

    control = b"signed-control"
    artifacts = b"bundle" + b"worktree" + b"pack"
    response = b"signed-response"
    observed: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        observed.append(await reader.read())
        writer.write(_frame(response))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with _server(socket_path, handle):
        actual = await relay_artifact_upload(
            control,
            _reader(artifacts),
            broker_socket=socket_path,
            timeout_seconds=1.0,
            input_bytes_max=len(artifacts),
            chunk_bytes=3,
        )

    assert observed == [_frame(control) + artifacts]
    assert actual == response


@pytest.mark.asyncio
async def test_signed_early_response_wins_over_large_remaining_upload(socket_path: Path) -> None:
    from yinshi.broker_artifact_stdio_relay import relay_artifact_upload

    control = b"signed-control"
    response = b"signed-rejection"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        length = int.from_bytes(await reader.readexactly(4), "big")
        assert await reader.readexactly(length) == control
        writer.write(_frame(response))
        await writer.drain()
        writer.write_eof()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    async with _server(socket_path, handle):
        actual = await relay_artifact_upload(
            control,
            _reader(b"x" * (16 * 1024 * 1024)),
            broker_socket=socket_path,
            timeout_seconds=2.0,
        )

    assert actual == response


@pytest.mark.asyncio
async def test_async_control_reader_is_bounded_by_caller_deadline() -> None:
    from yinshi.broker_artifact_stdio_relay import _read_control_frame

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await _read_control_frame(_reader(b"\x00\x00", eof=False))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await _read_control_frame(_reader(b"\x00\x00\x00\x08half", eof=False))


@pytest.mark.asyncio
async def test_relay_rejects_aggregate_overflow_and_aborts_transport(socket_path: Path) -> None:
    from yinshi.broker_artifact_stdio_relay import RelayError, relay_artifact_upload

    closed = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        closed.set()
        writer.close()
        await writer.wait_closed()

    async with _server(socket_path, handle):
        with pytest.raises(RelayError, match="^input_limit$"):
            await relay_artifact_upload(
                b"signed-control",
                _reader(b"12345"),
                broker_socket=socket_path,
                timeout_seconds=1.0,
                input_bytes_max=4,
                chunk_bytes=2,
            )
        await asyncio.wait_for(closed.wait(), timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_response", "code"),
    [
        (b"", "transport_unknown"),
        ((BROKER_RESPONSE_BYTES_MAX + 1).to_bytes(4, "big"), "verification_unknown"),
        (_frame(b"response") + b"extra", "verification_unknown"),
    ],
)
async def test_relay_classifies_incomplete_or_invalid_response(
    socket_path: Path,
    raw_response: bytes,
    code: str,
) -> None:
    from yinshi.broker_artifact_stdio_relay import RelayError, relay_artifact_upload

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.write(raw_response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with _server(socket_path, handle):
        with pytest.raises(RelayError, match=f"^{code}$"):
            await relay_artifact_upload(
                b"signed-control",
                _reader(b"artifact"),
                broker_socket=socket_path,
                timeout_seconds=1.0,
                input_bytes_max=16,
                chunk_bytes=4,
            )


@pytest.mark.asyncio
async def test_relay_cancellation_aborts_stalled_upload(socket_path: Path) -> None:
    from yinshi.broker_artifact_stdio_relay import relay_artifact_upload

    closed = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        closed.set()
        writer.close()
        await writer.wait_closed()

    async with _server(socket_path, handle):
        task = asyncio.create_task(
            relay_artifact_upload(
                b"signed-control",
                _reader(b"", eof=False),
                broker_socket=socket_path,
                timeout_seconds=10.0,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), timeout=1.0)


def test_cli_and_packaging_pin_the_upload_socket() -> None:
    from yinshi.artifact_upload_daemon import ARTIFACT_CONNECTION_TIMEOUT_SECONDS
    from yinshi.broker_artifact_stdio_relay import (
        DEFAULT_ARTIFACT_UPLOAD_SOCKET,
        RELAY_TIMEOUT_SECONDS,
        build_parser,
    )

    assert DEFAULT_ARTIFACT_UPLOAD_SOCKET == Path("/run/yinshi/artifact-upload.sock")
    assert RELAY_TIMEOUT_SECONDS > ARTIFACT_CONNECTION_TIMEOUT_SECONDS
    assert build_parser().parse_args([]).__dict__ == {}
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--socket", "/tmp/attacker.sock"])

    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["yinshi-broker-artifact-stdio-relay"] == (
        "yinshi.broker_artifact_stdio_relay:main"
    )
    assert metadata["project"]["scripts"]["yinshi-artifact-upload"] == (
        "yinshi.artifact_upload_daemon:main"
    )


def test_systemd_units_keep_upload_disabled_and_separate() -> None:
    root = Path(__file__).parents[2]
    socket_unit = (root / "deploy/systemd/yinshi-artifact-upload.socket").read_text(
        encoding="utf-8"
    )
    service_unit = (root / "deploy/systemd/yinshi-artifact-upload.service").read_text(
        encoding="utf-8"
    )
    broker_unit = (root / "deploy/systemd/yinshi-broker.service").read_text(encoding="utf-8")
    tmpfiles = (root / "deploy/tmpfiles.d/yinshi.conf").read_text(encoding="utf-8")

    assert "ListenStream=/run/yinshi/artifact-upload.sock" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "YINSHI_REPLICA_UPLOAD_ENABLED=false" in service_unit
    assert "--replica-upload-enabled" not in service_unit
    assert "artifact-upload" not in broker_unit
    assert "d /var/lib/yinshi/artifact-incoming 0700 yinshi-broker yinshi-broker" in tmpfiles
