"""Reconcile managed provider Sprites against durable control-plane ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from yinshi.db import get_control_db
from yinshi.services.managed_runners import managed_sprite_name
from yinshi.services.runners import revoke_managed_restore_runner_for_job
from yinshi.services.sprites import SpriteRecord

logger = logging.getLogger(__name__)


class ManagedSpriteInventoryProvider(Protocol):
    """Provider operations required for inventory reconciliation."""

    async def list_sprites(self, *, prefix: str) -> tuple[SpriteRecord, ...]: ...

    async def get_sprite(self, name: str) -> SpriteRecord | None: ...

    async def delete_sprite(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ManagedSpriteReconciliationResult:
    """Sanitized result for one complete reconciliation pass."""

    examined: int
    retained: int
    deleted: tuple[str, ...]
    deferred: int


@dataclass(frozen=True, slots=True)
class _ManagedSpriteReferences:
    names: frozenset[str]
    restore_jobs_by_name: dict[str, tuple[str, str]]


def _managed_sprite_references(
    *,
    restore_name_prefix: str,
    restore_name_key: str,
) -> _ManagedSpriteReferences:
    """Read all durable runtime and running-operation provider references."""
    names: set[str] = set()
    restore_jobs: dict[str, tuple[str, str]] = {}
    with get_control_db() as database:
        runtime_rows = database.execute(
            """SELECT sprite_external_id FROM managed_runtimes
               WHERE provider_name = ?""",
            ("fly_sprites",),
        ).fetchall()
        operation_rows = database.execute(
            """SELECT user_id, job_id, operation, source_sprite_id, candidate_sprite_id
               FROM managed_backup_operations WHERE status = ?""",
            ("running",),
        ).fetchall()
        restore_runner_rows = database.execute("""SELECT user_id, restore_job_id FROM user_runners
               WHERE kind = 'managed_restore' AND restore_job_id IS NOT NULL
                 AND revoked_at IS NULL""").fetchall()
    names.update(row["sprite_external_id"] for row in runtime_rows)
    for row in operation_rows:
        source_name = row["source_sprite_id"]
        candidate_name = row["candidate_sprite_id"]
        if source_name:
            names.add(source_name)
        if candidate_name:
            names.add(candidate_name)
            if row["operation"] == "restore":
                restore_jobs[candidate_name] = (row["user_id"], row["job_id"])
        if row["operation"] == "restore":
            deterministic_name = managed_sprite_name(
                f"{row['user_id']}:{row['job_id']}",
                prefix=restore_name_prefix,
                secret_key=restore_name_key,
            )
            names.add(deterministic_name)
            restore_jobs[deterministic_name] = (row["user_id"], row["job_id"])
    for row in restore_runner_rows:
        deterministic_name = managed_sprite_name(
            f"{row['user_id']}:{row['restore_job_id']}",
            prefix=restore_name_prefix,
            secret_key=restore_name_key,
        )
        restore_jobs[deterministic_name] = (row["user_id"], row["restore_job_id"])
    return _ManagedSpriteReferences(frozenset(names), restore_jobs)


class ManagedSpriteReconciler:
    """Delete old managed Sprites that have no durable owner."""

    def __init__(
        self,
        *,
        provider: ManagedSpriteInventoryProvider,
        name_prefix: str,
        restore_name_prefix: str,
        restore_name_key: str,
        grace: timedelta,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not name_prefix or not restore_name_prefix or not restore_name_key:
            raise ValueError("Sprite reconciliation names and key must not be empty")
        if grace.total_seconds() <= 0:
            raise ValueError("Sprite reconciliation grace must be positive")
        self._provider = provider
        self._name_prefix = name_prefix
        self._restore_name_prefix = restore_name_prefix
        self._restore_name_key = restore_name_key
        self._grace = grace
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def reconcile_once(self) -> ManagedSpriteReconciliationResult:
        """Reconcile one fully validated provider inventory."""
        records_by_name: dict[str, SpriteRecord] = {}
        for prefix in self._inventory_prefixes():
            for record in await self._provider.list_sprites(prefix=prefix):
                records_by_name[record.name] = record
        references = _managed_sprite_references(
            restore_name_prefix=self._restore_name_prefix,
            restore_name_key=self._restore_name_key,
        )
        now = self._clock().astimezone(timezone.utc)
        deleted: list[str] = []
        retained = 0
        deferred = 0
        for name, record in sorted(records_by_name.items()):
            if not self._owns_name(name):
                continue
            if name in references.names:
                retained += 1
                continue
            if record.created_at is None or now - record.created_at < self._grace:
                deferred += 1
                continue
            current = _managed_sprite_references(
                restore_name_prefix=self._restore_name_prefix,
                restore_name_key=self._restore_name_key,
            )
            if name in current.names:
                retained += 1
                continue
            restore_job = current.restore_jobs_by_name.get(name)
            if restore_job is not None:
                revoke_managed_restore_runner_for_job(*restore_job)
            await self._provider.delete_sprite(name)
            deleted.append(name)
        return ManagedSpriteReconciliationResult(
            examined=len(records_by_name),
            retained=retained,
            deleted=tuple(deleted),
            deferred=deferred,
        )

    async def run(self, *, interval_seconds: float) -> None:
        """Run recurring passes until cancelled."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            await self._sleep(interval_seconds)
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("managed_sprite_reconciliation_failed")

    def _inventory_prefixes(self) -> tuple[str, ...]:
        """Return distinct provider query prefixes including delimiter."""
        prefixes = (f"{self._name_prefix}-", f"{self._restore_name_prefix}-")
        return tuple(dict.fromkeys(prefixes))

    def _owns_name(self, name: str) -> bool:
        """Return whether a name belongs to an exact configured namespace."""
        return any(name.startswith(prefix) for prefix in self._inventory_prefixes())
