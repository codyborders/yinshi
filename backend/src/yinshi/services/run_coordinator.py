"""Run coordinator for managing active prompt runs and cancellation."""

import asyncio
import logging
from dataclasses import dataclass

from yinshi.services.sidecar import SidecarClient

logger = logging.getLogger(__name__)


@dataclass
class RunRecord:
    """Owns state for a single active prompt run."""

    session_id: str
    sidecar: SidecarClient
    cancel_requested: bool = False
    turn_id: str | None = None
    assistant_msg_id: str | None = None
    accumulated: str = ""
    chunk_count: int = 0


class RunCoordinator:
    """Manages active runs and coordinates cancellation."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        session_id: str,
        sidecar: SidecarClient,
        turn_id: str,
    ) -> RunRecord:
        """Register a new active run."""
        async with self._lock:
            record = RunRecord(session_id=session_id, sidecar=sidecar, turn_id=turn_id)
            self._runs[session_id] = record
            logger.debug("Run registered: session=%s", session_id)
            return record

    def get(self, session_id: str) -> RunRecord | None:
        """Get a run record without locking."""
        return self._runs.get(session_id)

    async def request_cancel(self, session_id: str) -> bool:
        """Request cancellation for a run. Returns True if found and cancelled."""
        async with self._lock:
            record = self._runs.get(session_id)
            if not record:
                return False
            record.cancel_requested = True
            await record.sidecar.cancel(session_id)
            logger.info("Cancel requested: session=%s", session_id)
            return True

    async def release(self, session_id: str) -> None:
        """Remove a run record."""
        async with self._lock:
            self._runs.pop(session_id, None)
            logger.debug("Run released: session=%s", session_id)


# Global coordinator instance
_coordinator: RunCoordinator | None = None


def get_run_coordinator() -> RunCoordinator:
    """Get the global run coordinator instance."""
    global _coordinator
    if _coordinator is None:
        _coordinator = RunCoordinator()
    return _coordinator
