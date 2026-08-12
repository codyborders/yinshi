"""Reference-counted local task lease for managed Fly Sprites."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

_TASK_PATH = "/v1/tasks/yinshi-active"
_TASK_PAYLOAD = {"expire": "5m"}
_REFRESH_INTERVAL_SECONDS = 60.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
_SPRITE_API_SOCKET = "/.sprite/api.sock"

Sleep = Callable[[float], Awaitable[None]]


class SpriteTaskLeaseError(RuntimeError):
    """Raised when a managed Sprite task cannot be acquired."""


class SpriteTaskLease:
    """Keep one bounded Sprite task alive while relay transfers exist."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be positive")
        if transport is None:
            transport = httpx.AsyncHTTPTransport(uds=_SPRITE_API_SOCKET)
        self._client = httpx.AsyncClient(
            base_url="http://sprite",
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._lock = asyncio.Lock()
        self._references = 0
        self._refresh_task: asyncio.Task[None] | None = None
        self._closed = False

    async def acquire(self) -> None:
        """Acquire one reference and create the local task when needed."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Sprite task lease is closed")
            if self._references == 0:
                await self._put_task()
                self._refresh_task = asyncio.create_task(
                    self._refresh_loop(),
                    name="sprite-task-lease-refresh",
                )
            self._references += 1

    async def release(self) -> None:
        """Release one reference and delete the task after the final reference."""
        async with self._lock:
            if self._references == 0:
                raise RuntimeError("Sprite task lease has no references")
            self._references -= 1
            if self._references != 0:
                return
            await self._stop_refresh()
            await self._delete_task()

    async def aclose(self) -> None:
        """Delete any held task and close the local HTTP client."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            had_references = self._references > 0
            self._references = 0
            await self._stop_refresh()
            if had_references:
                await self._delete_task()
        await self._client.aclose()

    async def _refresh_loop(self) -> None:
        """Refresh the task expiry once per minute while references remain."""
        while True:
            await self._sleep(_REFRESH_INTERVAL_SECONDS)
            async with self._lock:
                if self._references == 0 or self._closed:
                    return
                try:
                    await self._put_task()
                except SpriteTaskLeaseError:
                    logger.warning("Could not refresh managed Sprite task lease")

    async def _stop_refresh(self) -> None:
        """Stop and join the current refresh task."""
        refresh_task = self._refresh_task
        self._refresh_task = None
        if refresh_task is None or refresh_task is asyncio.current_task():
            return
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass

    async def _put_task(self) -> None:
        """Create or refresh the fixed local task without exposing HTTP errors."""
        failed = False
        try:
            response = await self._client.put(
                _TASK_PATH,
                json=_TASK_PAYLOAD,
                timeout=self._request_timeout_seconds,
            )
            failed = not 200 <= response.status_code < 300
        except (httpx.RequestError, TimeoutError):
            failed = True
        if failed:
            raise SpriteTaskLeaseError("Could not acquire managed Sprite task lease")

    async def _delete_task(self) -> None:
        """Best-effort delete the task while retaining expiry as crash fallback."""
        failed = False
        try:
            response = await self._client.delete(
                _TASK_PATH,
                timeout=self._request_timeout_seconds,
            )
            failed = not 200 <= response.status_code < 300 and response.status_code != 404
        except (httpx.RequestError, TimeoutError):
            failed = True
        if failed:
            logger.warning("Could not delete managed Sprite task lease; expiry remains active")
