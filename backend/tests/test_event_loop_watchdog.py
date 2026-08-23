"""Verify managed runner diagnostics remain independent from event-loop progress."""

from __future__ import annotations

import asyncio
from typing import Any

from yinshi.services.event_loop_watchdog import EventLoopWatchdog


def test_watchdog_arms_one_nonrepeating_all_thread_dump() -> None:
    """Starting diagnostics should arm one nonrepeating native traceback timer."""
    armed: list[tuple[float, dict[str, Any]]] = []
    cancelled: list[None] = []

    def arm(timeout_seconds: float, **options: Any) -> None:
        armed.append((timeout_seconds, options))

    def cancel() -> None:
        cancelled.append(None)

    watchdog = EventLoopWatchdog(
        timeout_seconds=15.0,
        pulse_interval_seconds=5.0,
        arm_traceback=arm,
        cancel_traceback=cancel,
    )

    watchdog.start()
    watchdog.start()

    assert armed == [(15.0, {"repeat": False, "exit": False})]
    assert cancelled == []
    watchdog.stop()


async def test_watchdog_healthy_pulses_rearm_and_shutdown_cancels() -> None:
    """Healthy loop pulses should rearm diagnostics and shutdown should cancel them."""
    armed: list[float] = []
    cancelled: list[None] = []

    def arm(timeout_seconds: float, **_options: Any) -> None:
        armed.append(timeout_seconds)

    def cancel() -> None:
        cancelled.append(None)

    watchdog = EventLoopWatchdog(
        timeout_seconds=0.1,
        pulse_interval_seconds=0.01,
        arm_traceback=arm,
        cancel_traceback=cancel,
    )
    watchdog.start()
    pulse_task = asyncio.create_task(watchdog.run(), name="test-event-loop-watchdog")
    while len(armed) < 3:
        await asyncio.sleep(0.005)

    watchdog.stop()
    await asyncio.wait_for(pulse_task, timeout=0.1)

    assert armed[:3] == [0.1, 0.1, 0.1]
    assert len(cancelled) == len(armed)


async def test_watchdog_task_cancellation_cancels_pending_timer() -> None:
    """Cancelling the pulse task should cancel the pending native timer."""
    armed: list[None] = []
    cancelled: list[None] = []

    def arm(_timeout_seconds: float, **_options: Any) -> None:
        armed.append(None)

    def cancel() -> None:
        cancelled.append(None)

    watchdog = EventLoopWatchdog(
        timeout_seconds=0.1,
        pulse_interval_seconds=0.01,
        arm_traceback=arm,
        cancel_traceback=cancel,
    )
    pulse_task = asyncio.create_task(watchdog.run(), name="test-cancelled-watchdog")
    await asyncio.sleep(0)
    pulse_task.cancel()
    result = await asyncio.gather(pulse_task, return_exceptions=True)

    assert armed == [None]
    assert isinstance(result[0], asyncio.CancelledError)
    assert cancelled == [None]


async def test_watchdog_missed_pulse_dumps_once_then_rearms_after_recovery() -> None:
    """One stalled interval should dump once and healthy pulses should rearm it."""
    timer: asyncio.TimerHandle | None = None
    dump_count = 0
    arm_count = 0

    def arm(timeout_seconds: float, **_options: Any) -> None:
        nonlocal arm_count, timer
        arm_count += 1

        def dump() -> None:
            nonlocal dump_count, timer
            dump_count += 1
            timer = None

        timer = asyncio.get_running_loop().call_later(timeout_seconds, dump)

    def cancel() -> None:
        nonlocal timer
        if timer is not None:
            timer.cancel()
            timer = None

    watchdog = EventLoopWatchdog(
        timeout_seconds=0.02,
        pulse_interval_seconds=0.005,
        arm_traceback=arm,
        cancel_traceback=cancel,
    )
    watchdog.start()

    await asyncio.sleep(0.06)
    assert dump_count == 1
    await asyncio.sleep(0.03)
    assert dump_count == 1

    pulse_task = asyncio.create_task(watchdog.run(), name="test-recovered-watchdog")
    await asyncio.sleep(0.04)
    watchdog.stop()
    await asyncio.wait_for(pulse_task, timeout=0.1)

    assert dump_count == 1
    assert arm_count > 1


def test_watchdog_process_timer_has_one_owner() -> None:
    """A second watchdog must not replace or cancel the active process timer."""
    first_calls: list[str] = []
    second_calls: list[str] = []

    def first_arm(_timeout_seconds: float, **_options: Any) -> None:
        first_calls.append("arm")

    def first_cancel() -> None:
        first_calls.append("cancel")

    def second_arm(_timeout_seconds: float, **_options: Any) -> None:
        second_calls.append("arm")

    def second_cancel() -> None:
        second_calls.append("cancel")

    first = EventLoopWatchdog(arm_traceback=first_arm, cancel_traceback=first_cancel)
    second = EventLoopWatchdog(arm_traceback=second_arm, cancel_traceback=second_cancel)
    first.start()
    second.start()
    second.stop()
    first._pulse()
    first.stop()

    assert first_calls == ["arm", "cancel", "arm", "cancel"]
    assert second_calls == []


def test_watchdog_failed_arm_releases_process_timer_ownership() -> None:
    """A failed timer arm should let a later watchdog become the owner."""
    successful_calls: list[str] = []

    def fail_arm(_timeout_seconds: float, **_options: Any) -> None:
        raise RuntimeError("traceback timer unavailable")

    def successful_arm(_timeout_seconds: float, **_options: Any) -> None:
        successful_calls.append("arm")

    def successful_cancel() -> None:
        successful_calls.append("cancel")

    failed = EventLoopWatchdog(arm_traceback=fail_arm)
    successful = EventLoopWatchdog(
        arm_traceback=successful_arm,
        cancel_traceback=successful_cancel,
    )
    failed.start()
    successful.start()
    successful.stop()

    assert successful_calls == ["arm", "cancel"]


async def test_watchdog_timer_failures_are_safe_and_idempotent() -> None:
    """Unavailable traceback timers should not fail or retain watchdog state."""
    cancel_calls = 0

    def fail_arm(_timeout_seconds: float, **_options: Any) -> None:
        raise RuntimeError("traceback timer unavailable")

    def cancel() -> None:
        nonlocal cancel_calls
        cancel_calls += 1

    unavailable = EventLoopWatchdog(
        arm_traceback=fail_arm,
        cancel_traceback=cancel,
    )
    unavailable.start()
    await unavailable.run()
    unavailable.stop()
    unavailable.stop()

    def arm(_timeout_seconds: float, **_options: Any) -> None:
        return None

    def fail_cancel() -> None:
        raise OSError("traceback timer disabled")

    disabled = EventLoopWatchdog(
        arm_traceback=arm,
        cancel_traceback=fail_cancel,
    )
    disabled.start()
    disabled.stop()
    disabled.stop()

    assert cancel_calls == 0
