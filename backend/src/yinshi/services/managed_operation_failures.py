"""Persist typed managed operation failures."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from yinshi.db import get_control_db
from yinshi.services.managed_backups import _timestamp

ManagedOperationFailureClass = Literal["restore_failed", "deletion_failed"]


def fail_managed_backup_operation(
    *,
    job_id: str,
    runtime_generation: int,
    lease_owner: str | None,
    lease_token: str | None,
    failure_class: ManagedOperationFailureClass,
    error_code: str,
    now: datetime,
) -> bool:
    """Persist one exact semantic operation failure for monitoring."""
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """UPDATE managed_backup_operations
               SET status = 'failed', failure_class = ?, last_error = ?, updated_at = ?
               WHERE job_id = ? AND runtime_generation = ? AND status = 'running'
                 AND lease_owner IS ? AND lease_token IS ?""",
            (
                failure_class,
                error_code,
                timestamp,
                job_id,
                runtime_generation,
                lease_owner,
                lease_token,
            ),
        )
        database.commit()
    return result.rowcount == 1
