"""Concrete isolated staging boundary for destructive managed recovery drills."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yinshi.db import get_control_db
from yinshi.services.accounts import resolve_or_create_user
from yinshi.services.managed_backups import (
    get_managed_backup_operation,
    list_managed_backup_archives,
)
from yinshi.services.managed_runners import (
    claim_managed_runtime_deletion,
    finalize_managed_runtime_deletion,
    get_managed_runtime_status,
)
from yinshi.services.managed_sprite_registry import (
    list_managed_sprite_identities,
    remove_managed_sprite_identity,
)

_SQLITE_PATH = "/var/lib/yinshi/sqlite/drill.db"
_SQLITE_FIXTURE_MAX_BYTES = 10 * 1024 * 1024
_TEXT_PATH = "/var/lib/yinshi/files/nested/canary.txt"
_BINARY_PATH = "/var/lib/yinshi/files/canary.bin"
_EMPTY_PATH = "/var/lib/yinshi/files/empty"
_POLL_SECONDS = 2.0
_TIMEOUT_SECONDS = 900.0


def list_retained_managed_recovery_tenants() -> tuple[str, ...]:
    """Return durable internal drill tenant IDs across a process restart."""
    with get_control_db() as database:
        rows = database.execute("""SELECT user_id FROM oauth_identities
               WHERE provider = 'managed_recovery_drill' ORDER BY user_id""").fetchall()
    return tuple(str(row["user_id"]) for row in rows)


class StagingManagedRecoveryBoundary:
    """Compose existing hosted managers for one isolated internal tenant."""

    def __init__(
        self,
        *,
        runtime_manager: Any,
        backup_manager: Any,
        provider: Any,
        store: Any,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._backup_manager = backup_manager
        self._provider = provider
        self._store = store
        self._tenant: Any | None = None
        self._source_name: str | None = None
        self._archive_id: str | None = None
        self._object_key: str | None = None
        self._fixtures = self._make_fixtures()

    @staticmethod
    def _make_fixtures() -> dict[str, bytes]:
        """Build valid representative SQLite, text, binary, and empty fixtures."""
        with tempfile.TemporaryDirectory(prefix="yinshi-drill-sqlite-") as directory:
            database_path = Path(directory) / "drill.db"
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE canary (value TEXT NOT NULL)")
                database.execute("INSERT INTO canary (value) VALUES (?)", ("restored",))
                database.commit()
            sqlite_payload = database_path.read_bytes()
        return {
            _SQLITE_PATH: sqlite_payload,
            _TEXT_PATH: b"nested managed recovery canary\n",
            _BINARY_PATH: bytes(range(256)) * 4,
            _EMPTY_PATH: b"",
        }

    async def recover_retained_cleanup(self) -> None:
        """Fail closed before new work when an earlier drill still exists."""
        if list_retained_managed_recovery_tenants():
            raise RuntimeError("managed recovery retained cleanup failed")

    async def provision(self) -> None:
        """Create one internal tenant and wait for its managed runtime."""
        identity = uuid.uuid4().hex
        self._tenant = await asyncio.to_thread(
            resolve_or_create_user,
            "managed_recovery_drill",
            identity,
            f"managed-recovery-{identity}@invalid.local",
        )
        await self._runtime_manager.provision(self._required_user_id())
        runtime = await self._wait_runtime("ready")
        self._source_name = runtime.sprite_name

    async def write_fixtures(self) -> None:
        """Write representative bounded state and verify exact source bytes."""
        sprite_name = self._required_source_name()
        for path, content in self._fixtures.items():
            await self._provider.write_file(
                sprite_name,
                path=path,
                content=content,
                mode="0600",
                mkdir=True,
            )
        await self._verify_fixture_bytes(sprite_name)

    async def backup_with_lost_completion(self) -> None:
        """Create one encrypted backup while losing one successful completion response."""
        user_id = self._required_user_id()
        reservation = self._backup_manager.reserve_create()
        self._archive_id = reservation.archive_id
        self._object_key = reservation.object_key
        self._store.arm_lost_completion_response(
            object_key=reservation.object_key,
            archive_id=reservation.archive_id,
        )
        operation = self._backup_manager.enqueue_create(
            user_id,
            reservation=reservation,
        )
        self._backup_manager.wake()
        await self._wait_operation_complete(operation.job_id)
        archive = next(
            (
                value
                for value in list_managed_backup_archives(user_id)
                if value.id == operation.archive_id
            ),
            None,
        )
        if archive is None or archive.status != "ready" or archive.object_version is None:
            raise RuntimeError("managed recovery backup did not become ready")
        self._object_key = archive.object_key
        inventory = await self._store.inspect_object(object_key=archive.object_key)
        if inventory.version_count != 1 or inventory.multipart_upload_ids:
            raise RuntimeError("managed recovery backup object inventory is invalid")

    async def delete_source(self) -> None:
        """Delete the exact source Sprite after a durable ready archive exists."""
        source_name = self._required_source_name()
        await self._provider.delete_sprite(source_name)
        if await self._provider.get_sprite(source_name) is not None:
            raise RuntimeError("managed recovery source Sprite still exists")

    async def restore(self) -> None:
        """Claim explicit source loss and wait for replacement publication."""
        operation = self._backup_manager.enqueue_source_loss_restore(
            self._required_user_id(),
            self._required_archive_id(),
        )
        self._backup_manager.wake()
        await self._wait_operation_complete(operation.job_id)
        runtime = await self._wait_runtime("ready")
        if runtime.sprite_name == self._required_source_name():
            raise RuntimeError("managed recovery replacement did not change authority")

    async def verify(self) -> tuple[int, int, bool, bool]:
        """Verify restored bytes, SQLite integrity, and sole replacement authority."""
        runtime = await self._wait_runtime("ready")
        await self._verify_fixture_bytes(runtime.sprite_name)
        database_payload = await self._provider.read_file(
            runtime.sprite_name,
            path=_SQLITE_PATH,
            max_bytes=_SQLITE_FIXTURE_MAX_BYTES,
        )
        data_verified = await asyncio.to_thread(self._verify_sqlite, database_payload)
        source_absent = await self._provider.get_sprite(self._required_source_name()) is None
        inventory = await self._store.inspect_object(object_key=self._required_object_key())
        authority_verified = source_absent and runtime.sprite_name != self._required_source_name()
        return (
            inventory.version_count,
            len(inventory.multipart_upload_ids),
            data_verified,
            authority_verified,
        )

    async def cleanup(self) -> bool:
        """Delete archive, replacement, registry state, and internal tenant data."""
        if self._tenant is None:
            return True
        user_id = self._tenant.user_id
        try:
            if self._object_key is None and self._archive_id is not None:
                archives = list_managed_backup_archives(user_id)
                archive = next((value for value in archives if value.id == self._archive_id), None)
                if archive is not None:
                    self._object_key = archive.object_key
            if self._object_key is not None:
                await self._store.purge_object(object_key=self._object_key)
                inventory = await self._store.inspect_object(object_key=self._object_key)
                if (
                    inventory.version_count
                    or inventory.delete_marker_count
                    or inventory.multipart_upload_ids
                ):
                    return False
            runtime = get_managed_runtime_status(user_id)
            if runtime is not None:
                claim = claim_managed_runtime_deletion(user_id, datetime.now(UTC))
                if claim is not None:
                    await self._provider.delete_sprite(claim.runtime.sprite_name)
                    if await self._provider.get_sprite(claim.runtime.sprite_name) is not None:
                        raise RuntimeError("managed recovery replacement still exists")
                    if not finalize_managed_runtime_deletion(user_id, claim.runtime.generation):
                        raise RuntimeError("managed recovery runtime cleanup was not stored")
            owned_identities = tuple(
                identity
                for identity in list_managed_sprite_identities()
                if identity.user_id == user_id
            )
            for identity in owned_identities:
                if await self._provider.get_sprite(identity.sprite_name) is not None:
                    return False
                remove_managed_sprite_identity(identity.sprite_name)
            if any(identity.user_id == user_id for identity in list_managed_sprite_identities()):
                return False
            with get_control_db() as database:
                database.execute("DELETE FROM users WHERE id = ?", (user_id,))
                database.commit()
            shutil.rmtree(self._tenant.data_dir, ignore_errors=True)
            return get_managed_runtime_status(user_id) is None
        except Exception:
            return False

    async def _wait_runtime(self, status: str) -> Any:
        deadline = asyncio.get_running_loop().time() + _TIMEOUT_SECONDS
        while True:
            runtime = get_managed_runtime_status(self._required_user_id())
            if runtime is not None and runtime.lifecycle_status == status:
                return runtime
            if runtime is not None and runtime.lifecycle_status == "failed":
                raise RuntimeError("managed recovery runtime failed")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("managed recovery runtime timed out")
            await asyncio.sleep(_POLL_SECONDS)

    async def _wait_operation_complete(self, job_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + _TIMEOUT_SECONDS
        while True:
            operation = get_managed_backup_operation(self._required_user_id(), job_id)
            if operation is None:
                return
            if operation.status == "failed":
                raise RuntimeError("managed recovery operation failed")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("managed recovery operation timed out")
            await asyncio.sleep(_POLL_SECONDS)

    async def _verify_fixture_bytes(self, sprite_name: str) -> None:
        for path, expected in self._fixtures.items():
            actual = await self._provider.read_file(
                sprite_name,
                path=path,
                max_bytes=max(len(expected), 1) + 1024,
            )
            if not hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest():
                raise RuntimeError("managed recovery fixture digest did not match")

    @staticmethod
    def _verify_sqlite(payload: bytes) -> bool:
        with tempfile.TemporaryDirectory(prefix="yinshi-drill-verify-") as directory:
            path = Path(directory) / "drill.db"
            path.write_bytes(payload)
            with sqlite3.connect(path) as database:
                integrity = database.execute("PRAGMA integrity_check").fetchone()
                canary = database.execute("SELECT value FROM canary").fetchone()
        return bool(integrity == ("ok",) and canary == ("restored",))

    def _required_user_id(self) -> str:
        if self._tenant is None:
            raise RuntimeError("managed recovery tenant is unavailable")
        return str(self._tenant.user_id)

    def _required_source_name(self) -> str:
        if self._source_name is None:
            raise RuntimeError("managed recovery source is unavailable")
        return self._source_name

    def _required_archive_id(self) -> str:
        if self._archive_id is None:
            raise RuntimeError("managed recovery archive is unavailable")
        return self._archive_id

    def _required_object_key(self) -> str:
        if self._object_key is None:
            raise RuntimeError("managed recovery object is unavailable")
        return self._object_key
