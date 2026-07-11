"""Desktop helper process protocol and loopback runtime primitives."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
from collections.abc import Sequence

import uvicorn

DESKTOP_HELPER_PROTOCOL_VERSION = 1
_INSTANCE_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOOPBACK_HOST = "127.0.0.1"


def serialize_ready_message(*, port: int, instance_nonce: str) -> bytes:
    """Return one validated newline-delimited readiness message."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    if not isinstance(instance_nonce, str):
        raise TypeError("instance_nonce must be a string")
    if _INSTANCE_NONCE_PATTERN.fullmatch(instance_nonce) is None:
        raise ValueError("instance_nonce must be 32-128 base64url characters")

    payload = {
        "type": "ready",
        "protocolVersion": DESKTOP_HELPER_PROTOCOL_VERSION,
        "port": port,
        "instanceNonce": instance_nonce,
    }
    message = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{message}\n".encode("ascii")


def _write_all(file_descriptor: int, payload: bytes) -> None:
    """Write a small protocol message completely, then close the inherited pipe."""
    if isinstance(file_descriptor, bool) or not isinstance(file_descriptor, int):
        raise TypeError("file_descriptor must be an integer")
    if file_descriptor < 0:
        raise ValueError("file_descriptor must not be negative")
    if not payload:
        raise ValueError("payload must not be empty")

    remaining = memoryview(payload)
    try:
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError("readiness pipe write made no progress")
            remaining = remaining[written:]
    finally:
        os.close(file_descriptor)


def _bind_loopback_socket() -> tuple[socket.socket, int]:
    """Bind an inheritable-disabled TCP socket to an OS-assigned loopback port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.set_inheritable(False)
        listener.bind((_LOOPBACK_HOST, 0))
        listener.listen(socket.SOMAXCONN)
        address = listener.getsockname()
        port = int(address[1])
        if port < 1 or port > 65535:
            raise RuntimeError("operating system returned an invalid loopback port")
        return listener, port
    except (OSError, RuntimeError):
        listener.close()
        raise


async def _wait_until_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Wait until Uvicorn finishes startup or propagate an early exit."""
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("desktop helper exited before readiness")
        await asyncio.sleep(0.01)


async def serve_desktop_helper(*, ready_fd: int) -> None:
    """Serve the restricted desktop app and signal readiness over one pipe."""
    if isinstance(ready_fd, bool) or not isinstance(ready_fd, int):
        raise TypeError("ready_fd must be an integer")
    if ready_fd < 0:
        raise ValueError("ready_fd must not be negative")

    from yinshi.main import create_app

    listener, port = _bind_loopback_socket()
    application = create_app(mode="desktop")
    config = uvicorn.Config(
        application,
        host=_LOOPBACK_HOST,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_until_started(server, server_task)
        ready_message = serialize_ready_message(
            port=port,
            instance_nonce=secrets.token_urlsafe(32),
        )
        _write_all(ready_fd, ready_message)
        await server_task
    finally:
        if not server_task.done():
            server.should_exit = True
            await server_task
        listener.close()


def _parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    """Parse the private desktop helper command-line contract."""
    parser = argparse.ArgumentParser(prog="yinshi-desktop-helper")
    parser.add_argument("--ready-fd", required=True, type=int)
    parsed = parser.parse_args(arguments)
    if parsed.ready_fd < 0:
        parser.error("--ready-fd must not be negative")
    return parsed


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the desktop helper until terminated by its Electron parent."""
    parsed = _parse_args(arguments)
    asyncio.run(serve_desktop_helper(ready_fd=parsed.ready_fd))


if __name__ == "__main__":
    main()
