"""Application-owned execution state for staging recovery drills."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from yinshi.managed_source_loss_recovery import (
    ManagedSourceLossReceipt,
    ManagedSourceLossResult,
)

logger = logging.getLogger(__name__)


class ManagedRecoveryDrillController:
    """Own one drill task and expose only sanitized aggregate state."""

    def __init__(
        self,
        *,
        run_drill: Callable[[], Awaitable[ManagedSourceLossReceipt]],
    ) -> None:
        if not callable(run_drill):
            raise TypeError("run_drill must be callable")
        self._run_drill = run_drill
        self._task: asyncio.Task[None] | None = None
        self._result: dict[str, object] = {
            "schema_version": 1,
            "status": "not_started",
        }

    async def start(self, *, commit_sha: str) -> dict[str, object]:
        """Start one run unless current work is still active."""
        if len(commit_sha) != 40 or any(
            character not in "0123456789abcdef" for character in commit_sha
        ):
            raise ValueError("commit_sha must be 40 lowercase hexadecimal characters")
        if self._task is not None and not self._task.done():
            raise RuntimeError("managed recovery drill is active")
        checks = self._result.get("checks")
        if (
            isinstance(checks, dict)
            and checks.get("cleanup_verified") is not True
            and self._result.get("status") in {"failed", "passed"}
        ):
            raise RuntimeError("managed recovery cleanup must be retried")
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._result = {"schema_version": 1, "status": "running"}
        self._task = asyncio.create_task(
            self._execute(commit_sha=commit_sha, started_at=started_at),
            name="managed-recovery-drill",
        )
        return dict(self._result)

    def status(self) -> dict[str, object]:
        """Return a detached copy of current aggregate state."""
        result = dict(self._result)
        checks = result.get("checks")
        if isinstance(checks, dict):
            result["checks"] = dict(checks)
        return result

    async def aclose(self) -> None:
        """Cancel unfinished work during application shutdown."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _execute(self, *, commit_sha: str, started_at: str) -> None:
        try:
            receipt = await self._run_drill()
            result = ManagedSourceLossResult(
                commit_sha=commit_sha,
                started_at=started_at,
                checks={
                    "archive_version_count": receipt.archive_version_count,
                    "cleanup_verified": receipt.cleanup_verified,
                    "data_verified": receipt.data_verified,
                    "multipart_upload_count": receipt.multipart_upload_count,
                    "replacement_authority_verified": (receipt.replacement_authority_verified),
                },
            )
            self._result = result.to_dict()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("managed_recovery_drill_failed")
            self._result = {
                "schema_version": 1,
                "status": "failed",
                "commit_sha": commit_sha,
                "started_at": started_at,
                "checks": {
                    "archive_version_count": 0,
                    "cleanup_verified": False,
                    "data_verified": False,
                    "multipart_upload_count": 0,
                    "replacement_authority_verified": False,
                },
            }
