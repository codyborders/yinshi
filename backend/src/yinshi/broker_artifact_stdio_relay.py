"""Opaque bounded stdio relay for the fixed broker artifact upload socket."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, TypeAlias, cast

from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
)

LOGGER = logging.getLogger("yinshi.broker_artifact_stdio_relay")
DEFAULT_ARTIFACT_UPLOAD_SOCKET = Path("/run/yinshi/artifact-upload.sock")
RELAY_TIMEOUT_SECONDS = 140.0
LENGTH_PREFIX_BYTES = 4
ARTIFACT_INPUT_BYTES_MAX = 1024 * 1024 * 1024
RELAY_CHUNK_BYTES = 64 * 1024

BrokerConnect: TypeAlias = Callable[
    [str | Path],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


class RelayError(RuntimeError):
    """Carry one stable relay error code without payload details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_blocking_exact(stream: BinaryIO, count: int) -> bytes:
    parts: list[bytes] = []
    remaining = count
    while remaining:
        content = stream.read(remaining)
        if not isinstance(content, bytes) or not content:
            raise RelayError("input_invalid")
        parts.append(content)
        remaining -= len(content)
    return b"".join(parts)


def read_blocking_control_frame(stream: BinaryIO) -> bytes:
    """Read one bounded control frame while leaving artifact bytes unread."""
    prefix = _read_blocking_exact(stream, LENGTH_PREFIX_BYTES)
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= BROKER_FRAME_BYTES_MAX:
        raise RelayError("input_limit" if length > BROKER_FRAME_BYTES_MAX else "input_invalid")
    return _read_blocking_exact(stream, length)


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    try:
        prefix = await reader.readexactly(LENGTH_PREFIX_BYTES)
    except asyncio.IncompleteReadError as error:
        raise RelayError("transport_unknown") from error
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= BROKER_RESPONSE_BYTES_MAX:
        raise RelayError("verification_unknown")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise RelayError("transport_unknown") from error
    if await reader.read(1):
        raise RelayError("verification_unknown")
    return body


async def _settle_task(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _stream_upload(
    artifact_reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    input_bytes_max: int,
    chunk_bytes: int,
) -> None:
    transferred = 0
    while True:
        try:
            content = await artifact_reader.read(chunk_bytes)
        except OSError as error:
            raise RelayError("input_unknown") from error
        if not content:
            break
        transferred += len(content)
        if transferred > input_bytes_max:
            raise RelayError("input_limit")
        writer.write(content)
        await writer.drain()
    writer.write_eof()


async def _exchange(
    control_frame: bytes,
    artifact_reader: asyncio.StreamReader,
    *,
    broker_socket: Path,
    connect: BrokerConnect,
    input_bytes_max: int,
    chunk_bytes: int,
) -> bytes:
    reader, writer = await connect(broker_socket)
    writer.write(len(control_frame).to_bytes(LENGTH_PREFIX_BYTES, "big") + control_frame)
    await writer.drain()
    upload_task = asyncio.create_task(
        _stream_upload(
            artifact_reader,
            writer,
            input_bytes_max=input_bytes_max,
            chunk_bytes=chunk_bytes,
        )
    )
    response_task = asyncio.create_task(_read_response(reader))
    try:
        await asyncio.wait(
            {upload_task, response_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if response_task.done():
            response = response_task.result()
            await _settle_task(upload_task)
            return response
        upload_task.result()
        return await response_task
    except BaseException:
        with suppress(Exception):
            writer.transport.abort()
        raise
    finally:
        writer.close()
        await _settle_task(upload_task)
        await _settle_task(response_task)
        with suppress(OSError):
            await writer.wait_closed()


async def relay_artifact_upload(
    control_frame: bytes,
    artifact_reader: asyncio.StreamReader,
    *,
    broker_socket: Path,
    timeout_seconds: float,
    input_bytes_max: int = ARTIFACT_INPUT_BYTES_MAX,
    chunk_bytes: int = RELAY_CHUNK_BYTES,
    connect: BrokerConnect = asyncio.open_unix_connection,
) -> bytes:
    """Stream one opaque bounded upload and return one broker response frame."""
    if not 1 <= len(control_frame) <= BROKER_FRAME_BYTES_MAX:
        raise RelayError("input_invalid")
    if not isinstance(artifact_reader, asyncio.StreamReader):
        raise RelayError("input_invalid")
    if type(input_bytes_max) is not int or input_bytes_max < 1:
        raise RelayError("input_invalid")
    if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= RELAY_CHUNK_BYTES:
        raise RelayError("input_invalid")
    try:
        return await asyncio.wait_for(
            _exchange(
                control_frame,
                artifact_reader,
                broker_socket=broker_socket,
                connect=connect,
                input_bytes_max=input_bytes_max,
                chunk_bytes=chunk_bytes,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise RelayError("timeout_unknown") from None
    except OSError as error:
        raise RelayError("transport_unknown") from error


def write_blocking_frame(stream: BinaryIO, body: bytes) -> None:
    """Write one bounded response frame and make it visible atomically to the caller."""
    if not body:
        raise RelayError("output_invalid")
    if len(body) > BROKER_RESPONSE_BYTES_MAX:
        raise RelayError("output_limit")
    stream.write(len(body).to_bytes(LENGTH_PREFIX_BYTES, "big") + body)
    stream.flush()


def build_parser() -> argparse.ArgumentParser:
    """Build an argument parser that rejects caller-controlled transport options."""
    return argparse.ArgumentParser(
        prog="yinshi-broker-artifact-stdio-relay",
        add_help=False,
        allow_abbrev=False,
    )


async def _read_control_frame(reader: asyncio.StreamReader) -> bytes:
    try:
        prefix = await reader.readexactly(LENGTH_PREFIX_BYTES)
    except asyncio.IncompleteReadError as error:
        raise RelayError("input_invalid") from error
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= BROKER_FRAME_BYTES_MAX:
        raise RelayError("input_limit" if length > BROKER_FRAME_BYTES_MAX else "input_invalid")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise RelayError("input_invalid") from error


async def _run_relay(source: BinaryIO) -> bytes:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _protocol = await loop.connect_read_pipe(lambda: protocol, source)
    try:
        async with asyncio.timeout(RELAY_TIMEOUT_SECONDS):
            control_frame = await _read_control_frame(reader)
            return await relay_artifact_upload(
                control_frame,
                reader,
                broker_socket=DEFAULT_ARTIFACT_UPLOAD_SOCKET,
                timeout_seconds=RELAY_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        raise RelayError("timeout_unknown") from None
    finally:
        transport.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Relay a control frame and remaining stdin bytes to the fixed upload socket."""
    build_parser().parse_args(argv)
    try:
        buffered_input = sys.stdin.buffer
        source = cast(BinaryIO, getattr(buffered_input, "raw", buffered_input))
        response = asyncio.run(_run_relay(source))
        write_blocking_frame(sys.stdout.buffer, response)
    except RelayError as error:
        LOGGER.error("%s", error.code)
        return 1
    except OSError:
        LOGGER.error("transport_unknown")
        return 1
    return 0
