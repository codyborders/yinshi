"""Dump managed runner thread stacks when its asyncio event loop stops advancing."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import math
import threading
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

_TIMER_OWNER_LOCK = threading.Lock()
_TIMER_OWNER: object | None = None


class TracebackArmer(Protocol):
    """Arm one process-wide traceback timer."""

    def __call__(
        self,
        timeout: float,
        *,
        repeat: bool = False,
        exit: bool = False,
    ) -> None: ...


class EventLoopWatchdog:
    """Maintain one native traceback timer from healthy event-loop pulses."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        pulse_interval_seconds: float = 5.0,
        arm_traceback: TracebackArmer = faulthandler.dump_traceback_later,
        cancel_traceback: Callable[[], None] = faulthandler.cancel_dump_traceback_later,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if not math.isfinite(pulse_interval_seconds) or pulse_interval_seconds <= 0:
            raise ValueError("pulse_interval_seconds must be a positive finite number")
        if pulse_interval_seconds >= timeout_seconds:
            raise ValueError("pulse_interval_seconds must be less than timeout_seconds")
        if not callable(arm_traceback) or not callable(cancel_traceback):
            raise TypeError("traceback timer operations must be callable")
        self._timeout_seconds = timeout_seconds
        self._pulse_interval_seconds = pulse_interval_seconds
        self._arm_traceback = arm_traceback
        self._cancel_traceback = cancel_traceback
        self._running = False
        self._owns_timer = False

    def _claim_timer(self) -> bool:
        """Claim the process timer when no other watchdog owns it."""
        global _TIMER_OWNER
        with _TIMER_OWNER_LOCK:
            if _TIMER_OWNER is not None and _TIMER_OWNER is not self:
                return False
            _TIMER_OWNER = self
            self._owns_timer = True
            return True

    def _release_timer(self) -> None:
        """Release process timer ownership only from its current owner."""
        global _TIMER_OWNER
        with _TIMER_OWNER_LOCK:
            if _TIMER_OWNER is self:
                _TIMER_OWNER = None
            self._owns_timer = False

    def _arm(self) -> bool:
        """Arm one nonrepeating native timer or disable diagnostics safely."""
        try:
            self._arm_traceback(
                self._timeout_seconds,
                repeat=False,
                exit=False,
            )
        except (OSError, RuntimeError, ValueError):
            logger.warning("Runner event-loop diagnostics could not be armed")
            return False
        return True

    def _cancel(self) -> bool:
        """Cancel the process timer without allowing diagnostics to fail the runner."""
        try:
            self._cancel_traceback()
        except (OSError, RuntimeError, ValueError):
            logger.warning("Runner event-loop diagnostics could not be cancelled")
            return False
        return True

    def start(self) -> None:
        """Arm diagnostics once while preserving process-wide ownership."""
        if self._running or self._owns_timer:
            return
        if not self._claim_timer():
            return
        self._running = self._arm()
        if not self._running:
            self._release_timer()

    def stop(self) -> None:
        """Stop pulses and cancel only this watchdog's process timer."""
        if not self._owns_timer:
            self._running = False
            return
        was_running = self._running
        self._running = False
        try:
            if was_running:
                self._cancel()
        finally:
            self._release_timer()

    def _pulse(self) -> None:
        """Replace the pending timer only while this watchdog owns it."""
        if not self._running or not self._owns_timer:
            return
        if not self._cancel():
            self._running = False
            self._release_timer()
            return
        if not self._arm():
            self._running = False
            self._release_timer()

    async def run(self) -> None:
        """Rearm diagnostics while the event loop continues to make progress."""
        self.start()
        try:
            while self._running:
                await asyncio.sleep(self._pulse_interval_seconds)
                self._pulse()
        finally:
            self.stop()
