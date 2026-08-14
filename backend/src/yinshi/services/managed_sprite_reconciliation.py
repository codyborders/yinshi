"""Reconcile managed provider Sprites against durable control-plane ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from yinshi.db import get_control_db
from yinshi.services import managed_operational_failures
from yinshi.services.managed_runners import managed_sprite_name
from yinshi.services.managed_sprite_registry import (
    list_managed_sprite_identities,
    remove_managed_sprite_identity,
)
from yinshi.services.runners import revoke_managed_restore_runner_for_job
from yinshi.services.sprites import SpriteInventoryRecord, SpriteRecord

logger = logging.getLogger(__name__)


class ManagedSpriteInventoryProvider(Protocol):
    """Provider operations required for inventory reconciliation."""

    async def list_sprites(self, *, prefix: str) -> tuple[SpriteInventoryRecord, ...]: ...

    async def get_sprite(self, name: str) -> SpriteRecord | None: ...

    async def delete_sprite(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ManagedSpriteReconciliationResult:
    """Sanitized result for one complete reconciliation pass."""

    examined: int
    retained: int
    deleted: tuple[str, ...]
    deferred: int
    eligible: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ManagedSpriteReferences:
    names: frozenset[str]
    restore_jobs_by_name: dict[str, tuple[str, str]]


def _parse_registry_created_at(value: str) -> datetime | None:
    """Parse one registry timestamp without treating malformed state as old."""
    try:
        created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created_at.tzinfo is None:
        return None
    return created_at.astimezone(timezone.utc)


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

    async def reconcile_once(
        self,
        *,
        dry_run: bool = False,
    ) -> ManagedSpriteReconciliationResult:
        """Reconcile one fully validated provider inventory."""
        inventory_names: set[str] = set()
        for prefix in self._inventory_prefixes():
            for inventory_record in await self._provider.list_sprites(prefix=prefix):
                inventory_names.add(inventory_record.name)
        references = _managed_sprite_references(
            restore_name_prefix=self._restore_name_prefix,
            restore_name_key=self._restore_name_key,
        )
        registered_by_name = {
            identity.sprite_name: identity
            for identity in list_managed_sprite_identities()
            if self._owns_name(identity.sprite_name)
        }
        registered_names = set(registered_by_name)
        absent_registered_names = registered_names - inventory_names
        candidate_names = (inventory_names & registered_names) | absent_registered_names
        records_by_name = {
            name: await self._provider.get_sprite(name) for name in sorted(candidate_names)
        }
        now = self._clock().astimezone(timezone.utc)
        deleted: list[str] = []
        eligible: list[str] = []
        retained = 0
        deferred = 0
        for name in sorted(candidate_names):
            if name in references.names:
                retained += 1
                continue
            record = records_by_name[name]
            provider_absent = name in absent_registered_names and record is None
            if name in absent_registered_names and record is not None:
                deferred += 1
                continue
            if record is None and not provider_absent:
                deferred += 1
                continue
            created_at = (
                _parse_registry_created_at(registered_by_name[name].created_at)
                if provider_absent
                else record.created_at if record is not None else None
            )
            if created_at is None or now - created_at < self._grace:
                deferred += 1
                continue
            current = _managed_sprite_references(
                restore_name_prefix=self._restore_name_prefix,
                restore_name_key=self._restore_name_key,
            )
            if name in current.names:
                retained += 1
                continue
            if dry_run:
                eligible.append(name)
                continue
            restore_job = current.restore_jobs_by_name.get(name)
            if restore_job is not None:
                revoke_managed_restore_runner_for_job(*restore_job)
            if not provider_absent:
                await self._provider.delete_sprite(name)
            remove_managed_sprite_identity(name)
            deleted.append(name)
        return ManagedSpriteReconciliationResult(
            examined=len(inventory_names | absent_registered_names),
            retained=retained,
            deleted=tuple(deleted),
            deferred=deferred,
            eligible=tuple(eligible),
        )

    async def reconcile_classified(
        self,
        *,
        raise_on_failure: bool,
    ) -> ManagedSpriteReconciliationResult | None:
        """Run one pass with the same stable failure classification for every caller."""
        alert_class = (
            managed_operational_failures.ManagedPersistentAlertClass.SPRITE_RECONCILIATION_FAILED
        )
        try:
            result = await self.reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            managed_operational_failures.record_managed_operational_failure(alert_class)
            logger.exception("managed_sprite_reconciliation_failed")
            if raise_on_failure:
                raise
            return None
        managed_operational_failures.clear_managed_operational_failure(alert_class)
        return result

    async def run(self, *, interval_seconds: float) -> None:
        """Run recurring passes until cancelled."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            await self._sleep(interval_seconds)
            await self.reconcile_classified(raise_on_failure=False)

    def _inventory_prefixes(self) -> tuple[str, ...]:
        """Return distinct provider query prefixes including delimiter."""
        prefixes = (f"{self._name_prefix}-", f"{self._restore_name_prefix}-")
        return tuple(dict.fromkeys(prefixes))

    def _owns_name(self, name: str) -> bool:
        """Return whether a name belongs to an exact configured namespace."""
        return any(name.startswith(prefix) for prefix in self._inventory_prefixes())
