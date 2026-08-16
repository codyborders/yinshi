"""Lifecycle owner for durable managed backup work."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from yinshi.services.managed_backup_crypto import (
    seal_managed_backup_job,
    unwrap_managed_archive_key,
    wrap_managed_archive_key,
)
from yinshi.services.managed_backup_store import (
    PendingManagedBackupUploads,
    StoredManagedBackup,
)
from yinshi.services.managed_backups import (
    ManagedBackupArchive,
    ManagedBackupCreationClaim,
    ManagedBackupOperation,
    claim_due_managed_backup_operation,
    clear_managed_backup_candidate,
    complete_managed_backup_creation,
    complete_managed_backup_deletion,
    get_managed_backup_archive,
    list_managed_backup_retention_candidates,
    record_managed_backup_candidate,
    record_managed_backup_upload,
    record_managed_backup_upload_intent,
    renew_managed_backup_operation_lease,
    start_managed_backup_creation,
    start_managed_source_loss_restore,
)
from yinshi.services.managed_operation_failures import fail_managed_backup_operation
from yinshi.services.managed_runners import (
    ManagedRuntimeStatus,
    activate_managed_restore_candidate,
    get_managed_runtime_status,
    managed_sprite_name,
)
from yinshi.services.runners import (
    get_managed_runner_for_user,
    revoke_managed_restore_runner_for_job,
)
from yinshi.services.sprites import SpritesProviderError

_MAINTENANCE_ROOT = "/var/lib/yinshi/maintenance"
_RESULT_BYTES_MAX = 4096

logger = logging.getLogger(__name__)


def _prepare_staging_root(staging_root: Path | None) -> Path | None:
    """Create and validate one owner-only local ciphertext staging directory."""
    if staging_root is None:
        return None
    try:
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = staging_root.lstat()
    except OSError:
        raise ValueError("managed backup staging root is invalid") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("managed backup staging root is invalid")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValueError("managed backup staging root is invalid")
    try:
        staging_root.chmod(0o700)
    except OSError:
        raise ValueError("managed backup staging root is invalid") from None
    return staging_root


class ManagedBackupReservation(NamedTuple):
    """Non-runnable exact backup identity reserved by one manager call."""

    archive_id: str
    job_id: str
    object_key: str


class ManagedBackupManager:
    """Own managed backup startup and shutdown outside request handlers."""

    def __init__(
        self,
        *,
        reconcile_once: Callable[[], Awaitable[bool]] | None = None,
        interval_seconds: float = 5.0,
        start_creation: Callable[..., Any] = start_managed_backup_creation,
        start_restore: Callable[..., Any] | None = None,
        start_source_loss_restore: Callable[..., Any] | None = start_managed_source_loss_restore,
        start_deletion: Callable[..., Any] | None = None,
        get_runtime: Callable[[str], ManagedRuntimeStatus | None] = get_managed_runtime_status,
        get_runner: Callable[[str], dict[str, Any] | None] = get_managed_runner_for_user,
        wrapping_key: bytes | None = None,
        key_id: str = "backup-v1",
        object_prefix: str = "yinshi-managed-v1",
        now: Callable[[], datetime] | None = None,
        new_id: Callable[[], str] | None = None,
        provider: Any | None = None,
        store: Any | None = None,
        relay: Any | None = None,
        worker_id: str = "managed-backup-worker",
        claim_operation: Callable[
            ..., ManagedBackupOperation | None
        ] = claim_due_managed_backup_operation,
        renew_lease: Callable[..., bool] = renew_managed_backup_operation_lease,
        fail_operation: Callable[..., bool] = fail_managed_backup_operation,
        lease_renew_interval_seconds: float = 60.0,
        get_archive: Callable[[str, str], ManagedBackupArchive | None] = get_managed_backup_archive,
        unwrap_key: Callable[..., bytes] = unwrap_managed_archive_key,
        record_upload_intent: Callable[..., bool] = record_managed_backup_upload_intent,
        record_upload: Callable[..., bool] = record_managed_backup_upload,
        complete_creation: Callable[..., bool] = complete_managed_backup_creation,
        complete_deletion: Callable[..., bool] = complete_managed_backup_deletion,
        runtime_service: Any | None = None,
        restore_name_prefix: str = "managed-restore",
        restore_name_key: str | None = None,
        record_candidate: Callable[..., bool] = record_managed_backup_candidate,
        clear_candidate: Callable[..., bool] = clear_managed_backup_candidate,
        activate_candidate: Callable[..., bool] = activate_managed_restore_candidate,
        complete_restore: Callable[..., bool] | None = None,
        revoke_restore_runner: Callable[[str, str], bool] = (revoke_managed_restore_runner_for_job),
        coordinate_restore: (
            Callable[[ManagedBackupOperation, ManagedBackupArchive], Awaitable[None]] | None
        ) = None,
        new_lease_token: Callable[[], str] | None = None,
        staging_root: Path | None = None,
        retention_days: int = 30,
        retention_batch_size: int = 20,
        list_retention: Callable[..., tuple[ManagedBackupArchive, ...]] = (
            list_managed_backup_retention_candidates
        ),
        enqueue_retention: Callable[[str, str], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        create_result_timeout_seconds: float = 240.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if create_result_timeout_seconds <= 0:
            raise ValueError("create_result_timeout_seconds must be positive")
        if wrapping_key is not None and len(wrapping_key) != 32:
            raise ValueError("wrapping_key must contain exactly 32 bytes")
        if not key_id or not object_prefix:
            raise ValueError("managed backup key and object prefix are required")
        self._reconcile_once = reconcile_once or self.run_once
        self._interval_seconds = interval_seconds
        self._start_creation = start_creation
        self._start_restore = start_restore
        self._start_source_loss_restore = start_source_loss_restore
        self._start_deletion = start_deletion
        self._get_runtime = get_runtime
        self._get_runner = get_runner
        self._wrapping_key = wrapping_key
        self._key_id = key_id
        self._object_prefix = object_prefix.rstrip("/")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._provider = provider
        self._sleep = sleep
        self._monotonic = monotonic
        self._create_result_timeout_seconds = create_result_timeout_seconds
        self._store = store
        self._relay = relay
        self._worker_id = worker_id
        self._claim_operation = claim_operation
        if lease_renew_interval_seconds <= 0:
            raise ValueError("lease_renew_interval_seconds must be positive")
        self._renew_lease = renew_lease
        self._fail_operation = fail_operation
        self._lease_renew_interval_seconds = lease_renew_interval_seconds
        self._get_archive = get_archive
        self._unwrap_key = unwrap_key
        self._record_upload_intent = record_upload_intent
        self._record_upload = record_upload
        self._complete_creation = complete_creation
        self._complete_deletion = complete_deletion
        self._runtime_service = runtime_service
        self._restore_name_prefix = restore_name_prefix
        self._restore_name_key = restore_name_key
        self._record_candidate = record_candidate
        self._clear_candidate = clear_candidate
        self._activate_candidate = activate_candidate
        self._complete_restore = complete_restore
        self._revoke_restore_runner = revoke_restore_runner
        self._coordinate_restore_job = coordinate_restore or self._coordinate_restore
        self._new_lease_token = new_lease_token or (lambda: uuid.uuid4().hex)
        if not 1 <= retention_days <= 3650 or not 1 <= retention_batch_size <= 100:
            raise ValueError("managed backup retention bounds are invalid")
        self._staging_root = _prepare_staging_root(staging_root)
        self._retention_days = retention_days
        self._retention_batch_size = retention_batch_size
        self._list_retention = list_retention
        self._enqueue_retention = enqueue_retention or self.enqueue_delete
        self._next_retention_at: datetime | None = None
        self._wake_event = asyncio.Event()
        self._wake_generation = 0
        self._handled_wake_generation = 0
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def reserve_create(self) -> ManagedBackupReservation:
        """Reserve exact identifiers before publishing a runnable operation."""
        archive_id = self._new_id()
        return ManagedBackupReservation(
            archive_id=archive_id,
            job_id=self._new_id(),
            object_key=f"{self._object_prefix}/{archive_id}.enc",
        )

    def enqueue_create(
        self,
        user_id: str,
        *,
        reservation: ManagedBackupReservation | None = None,
    ) -> ManagedBackupOperation:
        """Persist one server-authorized encrypted backup request."""
        if self._wrapping_key is None:
            raise ValueError("managed backups are unavailable")
        runtime = self._get_runtime(user_id)
        runner = self._get_runner(user_id)
        if (
            runtime is None
            or runtime.lifecycle_status != "ready"
            or runner is None
            or runner.get("id") != runtime.runner_id
            or runner.get("noise_key_confirmed") is not True
        ):
            raise ValueError("managed runtime is not ready for backup")
        reserved = reservation or self.reserve_create()
        archive_id = reserved.archive_id
        job_id = reserved.job_id
        archive_key = os.urandom(32)
        owner_digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        wrapped_key = wrap_managed_archive_key(
            archive_key,
            user_id=user_id,
            archive_id=archive_id,
            key_id=self._key_id,
            wrapping_key=self._wrapping_key,
        )
        claim = self._start_creation(
            user_id,
            runtime_generation=runtime.generation,
            archive_id=archive_id,
            job_id=job_id,
            object_key=reserved.object_key,
            wrapped_key=wrapped_key,
            key_id=self._key_id,
            owner_digest=owner_digest,
            now=self._now(),
        )
        if isinstance(claim, ManagedBackupCreationClaim):
            return claim.operation
        if isinstance(claim, ManagedBackupOperation):
            return claim
        return ManagedBackupOperation(
            user_id=user_id,
            job_id=job_id,
            archive_id=archive_id,
            operation="create",
            status="running",
            runtime_generation=runtime.generation,
            started_at=self._now().isoformat(),
            updated_at=self._now().isoformat(),
            last_error=None,
        )

    def enqueue_restore(self, user_id: str, archive_id: str) -> ManagedBackupOperation:
        """Persist one tenant-owned replacement restore request."""
        runtime = self._get_runtime(user_id)
        archive = self._get_archive(user_id, archive_id)
        if archive is None:
            raise LookupError("managed backup archive was not found")
        if (
            runtime is None
            or runtime.lifecycle_status != "ready"
            or archive.status != "ready"
            or archive.object_version is None
            or self._start_restore is None
        ):
            raise ValueError("managed backup archive is not restorable")
        job_id = self._new_id()
        claim = self._start_restore(
            user_id,
            archive_id=archive_id,
            runtime_generation=runtime.generation,
            job_id=job_id,
            now=self._now(),
        )
        if isinstance(claim, ManagedBackupCreationClaim):
            return claim.operation
        if isinstance(claim, ManagedBackupOperation):
            return claim
        return ManagedBackupOperation(
            user_id=user_id,
            job_id=job_id,
            archive_id=archive_id,
            operation="restore",
            status="running",
            runtime_generation=runtime.generation,
            started_at=self._now().isoformat(),
            updated_at=self._now().isoformat(),
            last_error=None,
        )

    def enqueue_source_loss_restore(
        self,
        user_id: str,
        archive_id: str,
    ) -> ManagedBackupOperation:
        """Persist one explicitly destructive source-loss restore request."""
        runtime = self._get_runtime(user_id)
        archive = self._get_archive(user_id, archive_id)
        if archive is None:
            raise LookupError("managed backup archive was not found")
        if (
            runtime is None
            or runtime.lifecycle_status != "ready"
            or archive.status != "ready"
            or archive.object_version is None
            or self._start_source_loss_restore is None
        ):
            raise ValueError("managed backup archive is not restorable after source loss")
        job_id = self._new_id()
        claim = self._start_source_loss_restore(
            user_id,
            archive_id=archive_id,
            runtime_generation=runtime.generation,
            job_id=job_id,
            now=self._now(),
        )
        if isinstance(claim, ManagedBackupCreationClaim):
            return claim.operation
        if isinstance(claim, ManagedBackupOperation):
            return claim
        raise RuntimeError("managed source-loss restore claim is invalid")

    def enqueue_delete(self, user_id: str, archive_id: str) -> ManagedBackupOperation:
        """Persist one tenant-owned exact-version deletion request."""
        archive = self._get_archive(user_id, archive_id)
        if archive is None:
            raise LookupError("managed backup archive was not found")
        if (
            archive.status != "ready"
            or archive.object_version is None
            or self._start_deletion is None
        ):
            raise ValueError("managed backup archive is not deletable")
        runtime = self._get_runtime(user_id)
        deletion_generation = (
            runtime.generation if runtime is not None else archive.runtime_generation
        )
        job_id = self._new_id()
        claim = self._start_deletion(
            user_id,
            archive_id=archive_id,
            runtime_generation=deletion_generation,
            job_id=job_id,
            now=self._now(),
        )
        if isinstance(claim, ManagedBackupCreationClaim):
            return claim.operation
        if isinstance(claim, ManagedBackupOperation):
            return claim
        return ManagedBackupOperation(
            user_id=user_id,
            job_id=job_id,
            archive_id=archive_id,
            operation="delete",
            status="running",
            runtime_generation=deletion_generation,
            started_at=self._now().isoformat(),
            updated_at=self._now().isoformat(),
            last_error=None,
        )

    def schedule_retention(self) -> int:
        """Queue bounded old archives no more than once per minute."""
        now = self._now()
        if self._next_retention_at is not None and now < self._next_retention_at:
            return 0
        self._next_retention_at = now + timedelta(minutes=1)
        cutoff = (now - timedelta(days=self._retention_days)).isoformat()
        candidates = self._list_retention(
            cutoff=cutoff,
            limit=self._retention_batch_size,
        )
        bounded_candidates = candidates[: self._retention_batch_size]
        for archive in bounded_candidates:
            self._enqueue_retention(archive.user_id, archive.id)
        return len(bounded_candidates)

    async def run_once(self) -> bool:
        """Claim and complete one exact managed backup creation."""
        if self._provider is None or self._store is None or self._relay is None:
            return False
        now = self._now()
        operation = self._claim_operation(
            worker_id=self._worker_id,
            lease_token=self._new_lease_token(),
            now=now,
            lease_expires_at=now + timedelta(minutes=15),
        )
        if operation is None:
            return False
        work_task = asyncio.create_task(
            self._coordinate_operation(operation),
            name="managed-backup-operation",
        )
        lease_task = asyncio.create_task(
            self._renew_operation_lease(operation),
            name="managed-backup-lease-renewal",
        )
        try:
            done, _pending = await asyncio.wait(
                (work_task, lease_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_task in done:
                work_task.cancel()
                await asyncio.gather(work_task, return_exceptions=True)
                lease_task.result()
                raise RuntimeError("managed backup lease task ended unexpectedly")
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            return work_task.result()
        finally:
            work_task.cancel()
            lease_task.cancel()
            await asyncio.gather(work_task, lease_task, return_exceptions=True)

    async def _coordinate_operation(self, operation: ManagedBackupOperation) -> bool:
        """Dispatch one leased operation while the caller supervises ownership."""
        archive = self._get_archive(operation.user_id, operation.archive_id)
        if operation.operation == "delete":
            try:
                if archive is not None:
                    await self._coordinate_delete(operation, archive)
            except Exception:
                with suppress(Exception):
                    self._persist_operation_failure(operation, "deletion_failed")
                raise
            return True
        if operation.operation == "restore":
            try:
                if archive is not None and self._coordinate_restore_job is not None:
                    await self._coordinate_restore_job(operation, archive)
            except Exception:
                with suppress(Exception):
                    self._persist_operation_failure(operation, "restore_failed")
                raise
            return True
        if operation.operation != "create":
            return True
        runtime = self._get_runtime(operation.user_id)
        runner = self._get_runner(operation.user_id)
        assert self._provider is not None
        assert self._store is not None
        assert self._relay is not None
        if archive is None or runtime is None or runner is None:
            return True
        await self._coordinate_create(operation, archive, runtime, runner)
        return True

    def _persist_operation_failure(
        self,
        operation: ManagedBackupOperation,
        failure_class: str,
    ) -> None:
        """Persist a sanitized semantic class at the dispatch boundary."""
        self._fail_operation(
            job_id=operation.job_id,
            runtime_generation=operation.runtime_generation,
            lease_owner=operation.lease_owner,
            lease_token=operation.lease_token,
            failure_class=failure_class,
            error_code=f"{operation.operation}_coordination_failed",
            now=self._now(),
        )

    async def _renew_operation_lease(self, operation: ManagedBackupOperation) -> None:
        """Keep exact job ownership alive while external work remains active."""
        if operation.lease_token is None or operation.lease_owner is None:
            raise RuntimeError("managed backup operation lease is incomplete")
        while True:
            await asyncio.sleep(self._lease_renew_interval_seconds)
            now = self._now()
            renewed = await asyncio.to_thread(
                self._renew_lease,
                job_id=operation.job_id,
                worker_id=operation.lease_owner,
                lease_token=operation.lease_token,
                runtime_generation=operation.runtime_generation,
                now=now,
                lease_expires_at=now + timedelta(minutes=15),
            )
            if not renewed:
                raise RuntimeError("managed backup operation lease was lost")

    async def _coordinate_restore(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
    ) -> None:
        """Restore one exact archive into a private replacement before cutover."""
        assert self._provider is not None
        assert self._store is not None
        assert self._relay is not None
        if (
            self._runtime_service is None
            or self._restore_name_key is None
            or self._wrapping_key is None
            or operation.lease_token is None
            or operation.source_runner_id is None
            or operation.source_sprite_id is None
            or archive.object_version is None
            or archive.size_bytes is None
            or archive.sha256 is None
        ):
            return
        if operation.phase == "activated":
            if operation.candidate_sprite_id is None:
                raise RuntimeError("activated restore candidate is missing")
            await self._finish_activated_restore(
                operation,
                operation.candidate_sprite_id,
            )
            return
        candidate_name = operation.candidate_sprite_id or managed_sprite_name(
            f"{operation.user_id}:{operation.job_id}",
            prefix=self._restore_name_prefix,
            secret_key=self._restore_name_key,
        )
        try:
            candidate = await self._runtime_service.provision_restore_candidate(
                operation.user_id,
                job_id=operation.job_id,
                candidate_sprite_name=candidate_name,
                candidate_runner_id=operation.candidate_runner_id,
            )
        except BaseException:
            await self._recover_failed_restore(operation, candidate_name)
            raise
        if operation.candidate_sprite_id is None and not self._record_candidate(
            job_id=operation.job_id,
            lease_token=operation.lease_token,
            runtime_generation=operation.runtime_generation,
            candidate_runner_id=candidate.runner_id,
            candidate_sprite_id=candidate_name,
            now=self._now(),
        ):
            await self._recover_failed_restore(operation, candidate_name)
            return
        try:
            activated = await self._prepare_restore_candidate(
                operation,
                archive,
                candidate_name,
                candidate,
            )
        except BaseException:
            await self._recover_failed_restore(operation, candidate_name)
            raise
        if not activated:
            await self._recover_failed_restore(operation, candidate_name)
            return
        await self._finish_activated_restore(operation, candidate_name)

    async def _prepare_restore_candidate(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
        candidate_name: str,
        candidate: Any,
    ) -> bool:
        """Run replacement restore and report whether cutover became irreversible."""
        assert self._provider is not None
        assert self._store is not None
        assert self._relay is not None
        assert self._wrapping_key is not None
        assert operation.lease_token is not None
        assert operation.source_runner_id is not None
        assert operation.source_sprite_id is not None
        assert archive.object_version is not None
        assert archive.size_bytes is not None
        assert archive.sha256 is not None
        with tempfile.TemporaryDirectory(
            prefix="yinshi-managed-restore-", dir=self._staging_root
        ) as directory:
            local_path = Path(directory) / "archive.enc"
            await self._store.get_file(
                object_key=archive.object_key,
                object_version=archive.object_version,
                target_path=local_path,
                expected_size=archive.size_bytes,
                expected_sha256=archive.sha256,
            )
            root = f"{_MAINTENANCE_ROOT}/{operation.job_id}"
            await self._provider.upload_file(
                candidate_name,
                path=f"{root}.archive.enc",
                source_path=local_path,
                expected_size=archive.size_bytes,
                expected_sha256=archive.sha256,
                mode="0600",
            )
        archive_key = self._unwrap_key(
            envelope=archive.wrapped_key,
            user_id=operation.user_id,
            archive_id=archive.id,
            keyring={archive.key_id: self._wrapping_key},
        )
        sealed_job = seal_managed_backup_job(
            {
                "archive_context": {
                    "archive_id": archive.id,
                    "created_at": archive.created_at,
                    "owner_digest": archive.owner_digest,
                    "runtime_generation": archive.runtime_generation,
                },
                "archive_key": base64.urlsafe_b64encode(archive_key).rstrip(b"=").decode("ascii"),
                "job_id": operation.job_id,
                "operation": "restore",
                "version": 1,
            },
            runner_public_key=candidate.runner_public_key,
            job_id=operation.job_id,
        )
        await self._provider.write_file(
            candidate_name,
            path=f"{root}.job",
            content=sealed_job,
            mode="0600",
            mkdir=True,
        )
        await self._provider.configure_service(
            candidate_name,
            service_name="yinshi-maintenance",
            command="/opt/yinshi/current/venv/bin/python",
            args=("-m", "yinshi.managed_backup_guest", "restore", "--job-id", operation.job_id),
            environment={},
            directory="/opt/yinshi/current/backend",
            needs=(),
            http_port=None,
            monitor_duration=None,
        )
        await self._provider.start_service(
            candidate_name,
            service_name="yinshi-maintenance",
            monitor_duration=300,
        )
        self._parse_restore_result(
            await self._provider.read_file(
                candidate_name,
                path=f"{root}.result",
                max_bytes=_RESULT_BYTES_MAX,
            ),
            operation.job_id,
        )
        for service in ("yinshi-sidecar", "yinshi-runner"):
            await self._provider.start_service(
                candidate_name,
                service_name=service,
                monitor_duration=30,
            )
        assert self._runtime_service is not None
        await self._runtime_service.verify_restore_candidate(
            operation.user_id,
            job_id=operation.job_id,
            candidate_runner_id=candidate.runner_id,
            candidate_sprite_name=candidate_name,
            expected_public_key=candidate.runner_public_key,
        )
        if not operation.source_lost:
            await self._relay.quiesce_runner(
                operation.source_runner_id,
                job_id=operation.job_id,
                timeout_seconds=30,
            )
        artifact_version = self._runtime_service.artifact_version
        if not self._activate_candidate(
            operation.user_id,
            source_generation=operation.runtime_generation,
            candidate_runner_id=candidate.runner_id,
            candidate_sprite_id=candidate_name,
            artifact_version=artifact_version,
            now=self._now(),
            job_id=operation.job_id,
            lease_token=operation.lease_token,
        ):
            return False
        return True

    async def _finish_activated_restore(
        self,
        operation: ManagedBackupOperation,
        candidate_name: str,
    ) -> None:
        """Finish post-cutover cleanup without reverting active authority."""
        assert self._provider is not None
        assert operation.source_sprite_id is not None
        if not operation.source_lost:
            await self._provider.delete_sprite(operation.source_sprite_id)
        root = f"{_MAINTENANCE_ROOT}/{operation.job_id}"
        for suffix in (".job", ".result", ".archive.enc", ".release"):
            with suppress(Exception):
                await self._provider.delete_file(candidate_name, path=f"{root}{suffix}")
        if self._complete_restore is not None:
            self._complete_restore(
                job_id=operation.job_id,
                lease_token=operation.lease_token,
                runtime_generation=operation.runtime_generation,
                now=self._now(),
            )

    async def _recover_failed_restore(
        self,
        operation: ManagedBackupOperation,
        candidate_name: str,
    ) -> None:
        """Delete failed replacement and restore source availability before cutover."""
        assert self._provider is not None
        assert self._relay is not None
        with suppress(Exception):
            self._revoke_restore_runner(operation.user_id, operation.job_id)
        candidate_deleted = False
        try:
            await self._provider.delete_sprite(candidate_name)
            candidate_deleted = True
        except Exception:
            pass
        if (
            candidate_deleted
            and operation.lease_token is not None
            and operation.candidate_runner_id is not None
            and operation.candidate_sprite_id is not None
        ):
            with suppress(Exception):
                self._clear_candidate(
                    job_id=operation.job_id,
                    lease_token=operation.lease_token,
                    runtime_generation=operation.runtime_generation,
                    candidate_runner_id=operation.candidate_runner_id,
                    candidate_sprite_id=operation.candidate_sprite_id,
                    now=self._now(),
                )
        if operation.source_sprite_id is not None and not operation.source_lost:
            for service in ("yinshi-sidecar", "yinshi-runner"):
                with suppress(Exception):
                    await self._provider.start_service(
                        operation.source_sprite_id,
                        service_name=service,
                        monitor_duration=30,
                    )
        if operation.source_runner_id is not None and not operation.source_lost:
            with suppress(Exception):
                await self._relay.release_maintenance(
                    operation.source_runner_id,
                    job_id=operation.job_id,
                )

    @staticmethod
    def _parse_restore_result(payload: bytes, job_id: str) -> None:
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("managed restore guest result is invalid") from None
        current_result = {
            "cleanup_pending": False,
            "job_id": job_id,
            "status": "restored",
        }
        if result != current_result:
            raise ValueError("managed restore guest result is invalid")

    async def _coordinate_delete(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
    ) -> None:
        """Delete one exact version before cryptographic catalog erasure."""
        assert self._store is not None
        if archive.status != "deleting" or archive.object_version is None:
            return
        await self._store.delete_file(
            object_key=archive.object_key,
            object_version=archive.object_version,
        )
        self._complete_deletion(
            operation.user_id,
            job_id=operation.job_id,
            lease_token=operation.lease_token,
            runtime_generation=operation.runtime_generation,
            now=self._now(),
        )

    async def _coordinate_create(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
        runtime: ManagedRuntimeStatus,
        runner: dict[str, Any],
    ) -> None:
        assert self._provider is not None
        assert self._store is not None
        assert self._relay is not None
        if runtime.generation != operation.runtime_generation or runtime.runner_id != runner.get(
            "id"
        ):
            return
        if archive.status == "uploaded":
            await self._recover_uploaded_create(operation, archive, runtime)
            return
        if operation.phase == "object_uploading":
            if archive.size_bytes is None or archive.sha256 is None:
                raise RuntimeError("managed backup upload metadata is incomplete")
            try:
                stored = await self._store.reconcile_upload(
                    object_key=archive.object_key,
                    archive_id=archive.id,
                    expected_size=archive.size_bytes,
                    expected_sha256=archive.sha256,
                )
            except asyncio.CancelledError:
                await self._restore_create_runtime_if_owned(operation, runtime)
                raise
            except Exception:
                await self._restore_create_runtime(operation, runtime)
                raise
            if isinstance(stored, PendingManagedBackupUploads):
                if operation.lease_owner is None or operation.lease_token is None:
                    raise RuntimeError("managed backup operation lease is incomplete")
                renewed_at = self._now()
                renewed = await asyncio.to_thread(
                    self._renew_lease,
                    job_id=operation.job_id,
                    worker_id=operation.lease_owner,
                    lease_token=operation.lease_token,
                    runtime_generation=operation.runtime_generation,
                    now=renewed_at,
                    lease_expires_at=renewed_at + timedelta(minutes=15),
                )
                if not renewed:
                    raise RuntimeError("managed backup operation lease was lost")
                try:
                    await self._store.abort_uploads(
                        object_key=archive.object_key,
                        upload_ids=stored.upload_ids,
                    )
                    stored = await self._store.reconcile_upload(
                        object_key=archive.object_key,
                        archive_id=archive.id,
                        expected_size=archive.size_bytes,
                        expected_sha256=archive.sha256,
                    )
                    if isinstance(stored, PendingManagedBackupUploads):
                        raise RuntimeError("managed backup upload cleanup did not converge")
                except asyncio.CancelledError:
                    await self._restore_create_runtime_if_owned(operation, runtime)
                    raise
                except Exception:
                    await self._restore_create_runtime(operation, runtime)
                    raise
            if stored is None:
                try:
                    stored = await self._upload_preserved_create(
                        operation,
                        archive,
                        runtime,
                    )
                except asyncio.CancelledError:
                    await self._restore_create_runtime_if_owned(operation, runtime)
                    raise
                except Exception:
                    await self._restore_create_runtime(operation, runtime)
                    raise
            if not self._record_upload(
                operation.user_id,
                job_id=operation.job_id,
                lease_token=operation.lease_token,
                runtime_generation=operation.runtime_generation,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                object_version=stored.version,
                now=self._now(),
            ):
                return
            await self._recover_recorded_create(operation, stored, runtime)
            return
        assert self._wrapping_key is not None
        archive_key = self._unwrap_key(
            envelope=archive.wrapped_key,
            user_id=operation.user_id,
            archive_id=archive.id,
            keyring={self._key_id: self._wrapping_key},
        )
        payload = {
            "archive_context": {
                "archive_id": archive.id,
                "created_at": archive.created_at,
                "owner_digest": archive.owner_digest,
                "runtime_generation": operation.runtime_generation,
            },
            "archive_key": base64.urlsafe_b64encode(archive_key).rstrip(b"=").decode("ascii"),
            "job_id": operation.job_id,
            "operation": "create",
            "version": 1,
        }
        sealed_job = seal_managed_backup_job(
            payload,
            runner_public_key=str(runner["noise_public_key"]),
            job_id=operation.job_id,
        )
        root = f"{_MAINTENANCE_ROOT}/{operation.job_id}"
        maintenance_started = False
        try:
            await self._relay.quiesce_runner(
                runtime.runner_id, job_id=operation.job_id, timeout_seconds=30
            )
            maintenance_started = True
            for service in ("yinshi-runner", "yinshi-sidecar"):
                await self._provider.stop_service(
                    runtime.sprite_name,
                    service_name=service,
                    timeout_seconds=30,
                )
            await self._provider.write_file(
                runtime.sprite_name,
                path=f"{root}.job",
                content=sealed_job,
                mode="0600",
                mkdir=True,
            )
            await self._provider.configure_service(
                runtime.sprite_name,
                service_name="yinshi-maintenance",
                command="/opt/yinshi/current/venv/bin/python",
                args=(
                    "-m",
                    "yinshi.managed_backup_guest",
                    "create",
                    "--job-id",
                    operation.job_id,
                    "--hold-seconds",
                    "300",
                ),
                environment={},
                directory="/opt/yinshi/current/backend",
                needs=(),
                http_port=None,
                monitor_duration=None,
            )
            await self._provider.start_service(
                runtime.sprite_name,
                service_name="yinshi-maintenance",
                monitor_duration=5,
            )
            result = await self._wait_for_create_result(
                runtime.sprite_name,
                root,
                operation.job_id,
            )
        except BaseException:
            if maintenance_started:
                await self._recover_failed_create(operation, runtime, root)
            raise
        try:
            with tempfile.TemporaryDirectory(
                prefix="yinshi-managed-backup-", dir=self._staging_root
            ) as directory:
                local_path = Path(directory) / "archive.enc"
                await self._provider.download_file(
                    runtime.sprite_name,
                    path=f"{root}.archive.enc",
                    target_path=local_path,
                    expected_size=result["size_bytes"],
                    expected_sha256=result["sha256"],
                )
                if not self._record_upload_intent(
                    job_id=operation.job_id,
                    lease_token=operation.lease_token,
                    runtime_generation=operation.runtime_generation,
                    size_bytes=result["size_bytes"],
                    sha256=result["sha256"],
                    now=self._now(),
                ):
                    await self._recover_failed_create(operation, runtime, root)
                    return
                stored = await self._store.put_file(
                    local_path,
                    object_key=archive.object_key,
                    expected_size=result["size_bytes"],
                    expected_sha256=result["sha256"],
                    archive_id=archive.id,
                )
        except BaseException:
            await self._restore_create_runtime(operation, runtime)
            raise
        if not self._record_upload(
            operation.user_id,
            job_id=operation.job_id,
            lease_token=operation.lease_token,
            runtime_generation=operation.runtime_generation,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            object_version=stored.version,
            now=self._now(),
        ):
            await self._recover_failed_create(operation, runtime, root)
            return
        await self._provider.write_file(
            runtime.sprite_name,
            path=f"{root}.release",
            content=b"release\n",
            mode="0600",
            mkdir=True,
        )
        await self._provider.delete_service(
            runtime.sprite_name,
            service_name="yinshi-maintenance",
        )
        for service in ("yinshi-sidecar", "yinshi-runner"):
            await self._provider.start_service(
                runtime.sprite_name, service_name=service, monitor_duration=30
            )
        await self._relay.release_maintenance(runtime.runner_id, job_id=operation.job_id)
        self._complete_creation(
            operation.user_id,
            job_id=operation.job_id,
            lease_token=operation.lease_token,
            runtime_generation=operation.runtime_generation,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            object_version=stored.version,
            now=self._now(),
        )
        for suffix in (".job", ".result", ".archive.enc", ".release"):
            await self._provider.delete_file(runtime.sprite_name, path=f"{root}{suffix}")

    async def _wait_for_create_result(
        self,
        sprite_name: str,
        root: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Wait a bounded interval for one valid maintenance result file."""
        assert self._provider is not None
        deadline = self._monotonic() + self._create_result_timeout_seconds
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("managed backup result timed out") from None
            try:
                async with asyncio.timeout(remaining):
                    payload = await self._provider.read_file(
                        sprite_name,
                        path=f"{root}.result",
                        max_bytes=_RESULT_BYTES_MAX,
                    )
                return self._parse_create_result(payload, job_id)
            except TimeoutError:
                raise TimeoutError("managed backup result timed out") from None
            except SpritesProviderError:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("managed backup result timed out") from None
                try:
                    async with asyncio.timeout(remaining):
                        await self._sleep(min(1.0, remaining))
                except TimeoutError:
                    raise TimeoutError("managed backup result timed out") from None

    async def _upload_preserved_create(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
        runtime: ManagedRuntimeStatus,
    ) -> StoredManagedBackup:
        """Conditionally re-upload trusted guest output after confirmed absence."""
        assert self._provider is not None
        assert self._store is not None
        assert archive.size_bytes is not None
        assert archive.sha256 is not None
        root = f"{_MAINTENANCE_ROOT}/{operation.job_id}"
        with tempfile.TemporaryDirectory(
            prefix="yinshi-managed-backup-retry-",
            dir=self._staging_root,
        ) as directory:
            local_path = Path(directory) / "archive.enc"
            await self._provider.download_file(
                runtime.sprite_name,
                path=f"{root}.archive.enc",
                target_path=local_path,
                expected_size=archive.size_bytes,
                expected_sha256=archive.sha256,
            )
            stored = await self._store.put_file(
                local_path,
                object_key=archive.object_key,
                expected_size=archive.size_bytes,
                expected_sha256=archive.sha256,
                archive_id=archive.id,
            )
        if not isinstance(stored, StoredManagedBackup):
            raise RuntimeError("backup upload returned invalid object metadata")
        return stored

    async def _recover_failed_create(
        self,
        operation: ManagedBackupOperation,
        runtime: ManagedRuntimeStatus,
        root: str,
    ) -> None:
        """Restore source availability after a failed pre-upload create step."""
        assert self._provider is not None
        await self._restore_create_runtime(operation, runtime)
        for suffix in (".job", ".result", ".archive.enc", ".release"):
            with suppress(Exception):
                await self._provider.delete_file(
                    runtime.sprite_name,
                    path=f"{root}{suffix}",
                )

    async def _restore_create_runtime_if_owned(
        self,
        operation: ManagedBackupOperation,
        runtime: ManagedRuntimeStatus,
    ) -> None:
        """Restore source access only after extending the exact current lease."""
        if operation.lease_owner is None or operation.lease_token is None:
            return
        renewed_at = self._now()
        try:
            renewed = await asyncio.to_thread(
                self._renew_lease,
                job_id=operation.job_id,
                worker_id=operation.lease_owner,
                lease_token=operation.lease_token,
                runtime_generation=operation.runtime_generation,
                now=renewed_at,
                lease_expires_at=renewed_at + timedelta(minutes=15),
            )
        except Exception:
            return
        if renewed:
            await self._restore_create_runtime(operation, runtime)

    async def _restore_create_runtime(
        self,
        operation: ManagedBackupOperation,
        runtime: ManagedRuntimeStatus,
    ) -> None:
        """Restore source access without changing durable guest backup output."""
        assert self._provider is not None
        assert self._relay is not None
        with suppress(Exception):
            await self._provider.stop_service(
                runtime.sprite_name,
                service_name="yinshi-maintenance",
                timeout_seconds=30,
            )
        with suppress(Exception):
            await self._provider.delete_service(
                runtime.sprite_name,
                service_name="yinshi-maintenance",
            )
        for service in ("yinshi-sidecar", "yinshi-runner"):
            with suppress(Exception):
                await self._provider.start_service(
                    runtime.sprite_name,
                    service_name=service,
                    monitor_duration=30,
                )
        with suppress(Exception):
            await self._relay.release_maintenance(
                runtime.runner_id,
                job_id=operation.job_id,
            )

    async def _recover_uploaded_create(
        self,
        operation: ManagedBackupOperation,
        archive: ManagedBackupArchive,
        runtime: ManagedRuntimeStatus,
    ) -> None:
        """Resume the exact source and publish one previously recorded object."""
        if archive.object_version is None or archive.size_bytes is None or archive.sha256 is None:
            return
        await self._recover_recorded_create(
            operation,
            StoredManagedBackup(
                version=archive.object_version,
                size_bytes=archive.size_bytes,
                sha256=archive.sha256,
            ),
            runtime,
        )

    async def _recover_recorded_create(
        self,
        operation: ManagedBackupOperation,
        stored: StoredManagedBackup,
        runtime: ManagedRuntimeStatus,
    ) -> None:
        """Resume one source runtime after exact object metadata is durable."""
        assert self._provider is not None
        assert self._relay is not None
        if operation.lease_token is None:
            return
        root = f"{_MAINTENANCE_ROOT}/{operation.job_id}"
        await self._provider.write_file(
            runtime.sprite_name,
            path=f"{root}.release",
            content=b"release\n",
            mode="0600",
            mkdir=True,
        )
        await self._provider.delete_service(
            runtime.sprite_name,
            service_name="yinshi-maintenance",
        )
        for service in ("yinshi-sidecar", "yinshi-runner"):
            await self._provider.start_service(
                runtime.sprite_name,
                service_name=service,
                monitor_duration=30,
            )
        await self._relay.release_maintenance(
            runtime.runner_id,
            job_id=operation.job_id,
        )
        completed = self._complete_creation(
            operation.user_id,
            job_id=operation.job_id,
            lease_token=operation.lease_token,
            runtime_generation=operation.runtime_generation,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            object_version=stored.version,
            now=self._now(),
        )
        if not completed:
            return
        for suffix in (".job", ".result", ".archive.enc", ".release"):
            await self._provider.delete_file(runtime.sprite_name, path=f"{root}{suffix}")

    @staticmethod
    def _parse_create_result(payload: bytes, job_id: str) -> dict[str, Any]:
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("managed backup guest result is invalid") from None
        if not isinstance(result, dict) or set(result) != {
            "job_id",
            "sha256",
            "size_bytes",
            "status",
        }:
            raise ValueError("managed backup guest result is invalid")
        size = result["size_bytes"]
        digest = result["sha256"]
        if result["job_id"] != job_id or result["status"] != "ready":
            raise ValueError("managed backup guest result is invalid")
        if type(size) is not int or size <= 0 or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("managed backup guest result is invalid")
        if any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("managed backup guest result is invalid")
        return result

    async def start(self) -> None:
        """Start background maintenance ownership."""
        if self._task is not None:
            raise RuntimeError("managed backup manager is already started")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="managed-backup-manager")

    def wake(self) -> None:
        """Request prompt reconciliation."""
        if self._task is None:
            raise RuntimeError("managed backup manager is not started")
        self._wake_generation += 1
        self._wake_event.set()

    async def aclose(self) -> None:
        """Stop background maintenance ownership."""
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            reconcile_generation = self._wake_generation
            try:
                self.schedule_retention()
                did_work = await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Managed backup reconciliation failed")
                did_work = False
            self._handled_wake_generation = reconcile_generation
            if did_work:
                continue
            self._wake_event.clear()
            if self._wake_generation != self._handled_wake_generation:
                continue
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue

    async def _idle_reconcile(self) -> bool:
        return False
