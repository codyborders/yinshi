"""Run coordinator for managing active prompt runs and cancellation."""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

from yinshi.exceptions import SidecarError
from yinshi.services.sidecar import SidecarClient

logger = logging.getLogger(__name__)


class CancelOutcome(Enum):
    """Terminal result of one cancellation request."""

    REQUESTED = "requested"
    FINISHED = "finished"
    ABSENT = "absent"


@dataclass
class _RegisteredRun:
    """One active run registration plus its release bookkeeping."""

    sidecar: SidecarClient
    released: bool = field(default=False)


class RunCoordinator:
    """Manages active sidecar runs keyed by session id."""

    def __init__(self) -> None:
        self._runs: dict[str, _RegisteredRun] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, sidecar: SidecarClient) -> None:
        """Register a new active run."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        cancel_method = getattr(sidecar, "cancel", None)
        if not callable(cancel_method):
            raise TypeError("sidecar must expose a callable cancel method")

        async with self._lock:
            self._runs[session_id] = _RegisteredRun(sidecar=sidecar)
            logger.debug("Run registered")

    async def request_cancel(self, session_id: str) -> CancelOutcome:
        """Request cancellation for a run.

        The sidecar cancellation runs without the coordinator lock because it
        can wait on sidecar network traffic. When release wins that race the
        run is already terminal, so its transport failure becomes a finished
        outcome instead of an error. Failures on a run that stays registered
        remain errors callers can act on.
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")

        async with self._lock:
            entry = self._runs.get(session_id)
            if entry is None:
                return CancelOutcome.ABSENT

        try:
            await entry.sidecar.cancel(session_id)
        except SidecarError:
            async with self._lock:
                run_was_released = entry.released
            if not run_was_released:
                raise
            logger.info("Run finished before its cancellation completed")
            return CancelOutcome.FINISHED
        logger.info("Run cancellation requested")
        return CancelOutcome.REQUESTED

    async def release(self, session_id: str) -> None:
        """Remove a run record and mark its registration as released."""
        if not session_id:
            raise ValueError("session_id must be non-empty")

        async with self._lock:
            entry = self._runs.pop(session_id, None)
            if entry is not None:
                entry.released = True
            logger.debug("Run released")


_coordinator: RunCoordinator | None = None


def get_run_coordinator() -> RunCoordinator:
    """Get the global run coordinator instance."""
    global _coordinator
    if _coordinator is None:
        _coordinator = RunCoordinator()
    return _coordinator
