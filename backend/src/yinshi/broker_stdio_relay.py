"""One bounded opaque stdio relay for the authenticated broker socket.

The relay reads exactly one length-prefixed request frame from stdin,
exchanges it with the fixed broker Unix socket, and writes exactly one
length-prefixed response frame to stdout. It stays opaque: Ed25519 signed
request and response bodies provide the end-to-end authentication, so the
relay never inspects payload bytes and reports only stable error codes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, TypeAlias

from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
)

LOGGER = logging.getLogger("yinshi.broker_stdio_relay")
DEFAULT_BROKER_SOCKET = Path("/run/yinshi/broker.sock")
RELAY_TIMEOUT_SECONDS = 390.0
LENGTH_PREFIX_BYTES = 4

BrokerConnect: TypeAlias = Callable[
    [str | Path],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


class RelayError(RuntimeError):
    """Carry one stable relay error code and expose only that code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def read_blocking_frame(stream: BinaryIO, maximum: int) -> bytes:
    """Read exactly one bounded length-prefixed frame and require EOF."""
    prefix = stream.read(LENGTH_PREFIX_BYTES)
    if len(prefix) != LENGTH_PREFIX_BYTES:
        raise RelayError("input_invalid")
    length = int.from_bytes(prefix, "big")
    if length == 0:
        raise RelayError("input_invalid")
    if length > maximum:
        raise RelayError("input_limit")
    body = stream.read(length)
    if len(body) != length:
        raise RelayError("input_invalid")
    if stream.read(1):
        raise RelayError("input_invalid")
    return body


def write_blocking_frame(stream: BinaryIO, body: bytes) -> None:
    """Write one bounded response frame to a blocking stream and flush it."""
    if not body:
        raise RelayError("output_invalid")
    if len(body) > BROKER_RESPONSE_BYTES_MAX:
        raise RelayError("output_limit")
    stream.write(len(body).to_bytes(LENGTH_PREFIX_BYTES, "big") + body)
    stream.flush()


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    """Read exactly one bounded broker response frame from the socket."""
    try:
        prefix = await reader.readexactly(LENGTH_PREFIX_BYTES)
    except asyncio.IncompleteReadError as exc:
        raise RelayError("transport_unknown") from exc
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= BROKER_RESPONSE_BYTES_MAX:
        raise RelayError("verification_unknown")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise RelayError("transport_unknown") from exc
    if await reader.read(1):
        raise RelayError("verification_unknown")
    return body


async def _exchange(
    request: bytes,
    *,
    broker_socket: Path,
    connect: BrokerConnect,
) -> bytes:
    reader, writer = await connect(broker_socket)
    try:
        writer.write(len(request).to_bytes(LENGTH_PREFIX_BYTES, "big") + request)
        await writer.drain()
        writer.write_eof()
        response = await _read_response(reader)
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        return response
    except BaseException:
        with suppress(Exception):
            writer.transport.abort()
        raise


async def relay_broker_request(
    request: bytes,
    *,
    broker_socket: Path,
    timeout_seconds: float,
    connect: BrokerConnect = asyncio.open_unix_connection,
) -> bytes:
    """Perform one bounded opaque exchange with the broker socket."""
    if not 1 <= len(request) <= BROKER_FRAME_BYTES_MAX:
        raise RelayError("input_invalid")
    try:
        return await asyncio.wait_for(
            _exchange(request, broker_socket=broker_socket, connect=connect),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise RelayError("timeout_unknown") from None
    except OSError as exc:
        raise RelayError("transport_unknown") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser that rejects every option."""
    return argparse.ArgumentParser(
        prog="yinshi-broker-stdio-relay",
        add_help=False,
        allow_abbrev=False,
    )


async def _run_relay(request: bytes) -> bytes:
    return await relay_broker_request(
        request,
        broker_socket=DEFAULT_BROKER_SOCKET,
        timeout_seconds=RELAY_TIMEOUT_SECONDS,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one relay exchange between process stdio and the broker socket."""
    build_parser().parse_args(argv)
    try:
        request = read_blocking_frame(sys.stdin.buffer, maximum=BROKER_FRAME_BYTES_MAX)
        response = asyncio.run(_run_relay(request))
        write_blocking_frame(sys.stdout.buffer, response)
    except RelayError as exc:
        LOGGER.error("%s", exc.code)
        return 1
    except OSError:
        LOGGER.error("transport_unknown")
        return 1
    return 0
