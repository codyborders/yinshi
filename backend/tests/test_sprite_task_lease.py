"""Verify bounded local Sprite task leases."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from yinshi.services.sprite_task_lease import SpriteTaskLease, SpriteTaskLeaseError


@pytest.mark.asyncio
async def test_sprite_task_lease_reference_counts_one_local_task() -> None:
    """Only the first acquire creates a task and the final release deletes it."""
    requests: list[httpx.Request] = []

    async def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    lease = SpriteTaskLease(transport=httpx.MockTransport(handle_request))

    await lease.acquire()
    await lease.acquire()
    await lease.release()

    assert [(request.method, request.url.path) for request in requests] == [
        ("PUT", "/v1/tasks/yinshi-active")
    ]
    assert requests[0].url.host == "sprite"
    assert requests[0].read() == b'{"expire":"5m"}'

    await lease.release()

    assert [(request.method, request.url.path) for request in requests] == [
        ("PUT", "/v1/tasks/yinshi-active"),
        ("DELETE", "/v1/tasks/yinshi-active"),
    ]
    await lease.aclose()


@pytest.mark.asyncio
async def test_sprite_task_lease_refreshes_each_minute_while_referenced() -> None:
    """A held lease refreshes its five-minute expiry after one minute."""
    requests: list[httpx.Request] = []
    sleep_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    sleep_calls = 0

    async def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    async def controlled_sleep(delay: float) -> None:
        nonlocal sleep_calls
        assert delay == 60.0
        sleep_calls += 1
        if sleep_calls == 1:
            sleep_started.set()
            await allow_refresh.wait()
            return
        await asyncio.Event().wait()

    lease = SpriteTaskLease(
        transport=httpx.MockTransport(handle_request),
        sleep=controlled_sleep,
    )
    await lease.acquire()
    await sleep_started.wait()
    allow_refresh.set()

    for _ in range(10):
        if len(requests) >= 2:
            break
        await asyncio.sleep(0)

    assert [request.method for request in requests] == ["PUT", "PUT"]
    assert requests[1].read() == b'{"expire":"5m"}'

    await lease.release()
    await lease.aclose()
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_sprite_task_lease_hides_request_bearing_failures() -> None:
    """Initial lease failure exposes no HTTP request object to callers."""

    async def handle_request(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("socket unavailable", request=request)

    lease = SpriteTaskLease(transport=httpx.MockTransport(handle_request))

    with pytest.raises(SpriteTaskLeaseError) as caught:
        await lease.acquire()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not vars(caught.value)
    await lease.aclose()


@pytest.mark.asyncio
async def test_sprite_task_lease_bounds_each_local_request() -> None:
    """Every local request receives the configured finite timeout."""
    observed_timeouts: list[dict[str, float | None]] = []

    async def handle_request(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions["timeout"])
        return httpx.Response(204, request=request)

    lease = SpriteTaskLease(
        transport=httpx.MockTransport(handle_request),
        request_timeout_seconds=2.5,
    )

    await lease.acquire()
    await lease.release()
    await lease.aclose()

    assert observed_timeouts == [
        {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5},
        {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5},
    ]
