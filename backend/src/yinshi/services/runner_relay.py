"""Bounded opaque relay authorization and in-memory connection broker."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from yinshi.db import get_control_db
from yinshi.services.managed_backups import ManagedBackupOperation
from yinshi.services.runner_capabilities import VerifiedRunnerCapability

_RUNNER_PREFIX_LENGTH = 16
_RELAY_FRAME_BYTES_MAX = 65_535
_RELAY_QUEUE_FRAMES_MAX = 16
_RUNNER_SESSIONS_MAX = 32
_RETIRED_TRANSFERS_MAX = 128
_RETIRED_TRANSFER_TTL_SECONDS = 300.0


class RunnerRelayAuthorizationError(ValueError):
    """A relay grant is missing, stale, mismatched, or already consumed."""


@dataclass(frozen=True, slots=True)
class RunnerTransferGrant:
    """Metadata needed to route bounded ciphertext without retaining content."""

    transfer_id: str
    runner_id: str
    expires_at: int
    max_session_bytes: int


class RelayWebSocket(Protocol):
    """Small WebSocket surface used by the relay broker and test fakes."""

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


def _require_transfer_id(transfer_id: str) -> str:
    """Return one canonical UUID transfer ID."""
    if not isinstance(transfer_id, str) or not transfer_id:
        raise ValueError("transfer_id must not be empty")
    try:
        normalized_id = str(uuid.UUID(transfer_id))
    except ValueError as exc:
        raise ValueError("transfer_id must be a UUID") from exc
    if normalized_id != transfer_id:
        raise ValueError("transfer_id must be canonical")
    return normalized_id


def _capability_hash(capability: str) -> str:
    """Hash a bounded capability before control-plane persistence or comparison."""
    if not isinstance(capability, str) or not capability:
        raise ValueError("capability must not be empty")
    if len(capability) > 8_192:
        raise ValueError("capability is too large")
    return hashlib.sha256(capability.encode("ascii")).hexdigest()


def store_runner_transfer_grant(
    capability: str,
    claims: VerifiedRunnerCapability,
) -> None:
    """Persist only routing limits and a capability hash for one issued grant."""
    if not isinstance(claims, VerifiedRunnerCapability):
        raise TypeError("claims must be VerifiedRunnerCapability")
    capability_hash = _capability_hash(capability)
    with get_control_db() as database:
        database.execute(
            "DELETE FROM runner_transfer_grants WHERE expires_at <= ?",
            (claims.issued_at,),
        )
        database.execute(
            """
            INSERT INTO runner_transfer_grants (
                transfer_id, user_id, runner_id, capability_hash,
                expires_at, max_session_bytes, claimed_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                claims.transfer_id,
                claims.user_id,
                claims.runner_id,
                capability_hash,
                claims.expires_at,
                claims.max_session_bytes,
            ),
        )
        database.commit()


def claim_runner_transfer_grant(
    transfer_id: str,
    capability: str,
    *,
    current_time: int | None = None,
) -> RunnerTransferGrant:
    """Atomically consume one exact unexpired capability hash for relay use."""
    normalized_transfer_id = _require_transfer_id(transfer_id)
    capability_hash = _capability_hash(capability)
    now = int(time.time()) if current_time is None else current_time
    if type(now) is not int or now < 0:
        raise ValueError("current_time must be a non-negative integer")

    with get_control_db() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT grant.*, runner.revoked_at, runner.runner_token_hash
            FROM runner_transfer_grants AS grant
            JOIN user_runners AS runner ON runner.id = grant.runner_id
            WHERE grant.transfer_id = ?
            """,
            (normalized_transfer_id,),
        ).fetchone()
        if row is None:
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay grant was not found")
        if row["expires_at"] <= now:
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay grant has expired")
        if row["claimed_at"] is not None:
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay grant was already claimed")
        if row["revoked_at"] is not None or row["runner_token_hash"] is None:
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay grant was revoked")
        if not secrets.compare_digest(row["capability_hash"], capability_hash):
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay capability does not match")
        result = database.execute(
            """
            UPDATE runner_transfer_grants
            SET claimed_at = ?
            WHERE transfer_id = ? AND claimed_at IS NULL
            """,
            (now, normalized_transfer_id),
        )
        if result.rowcount != 1:
            database.rollback()
            raise RunnerRelayAuthorizationError("Runner relay grant was already claimed")
        database.commit()
    return RunnerTransferGrant(
        transfer_id=normalized_transfer_id,
        runner_id=row["runner_id"],
        expires_at=row["expires_at"],
        max_session_bytes=row["max_session_bytes"],
    )


@dataclass(slots=True)
class _RunnerConnection:
    websocket: RelayWebSocket
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sessions: set[str] = field(default_factory=set)
    retired_transfers: OrderedDict[str, float] = field(default_factory=OrderedDict)


@dataclass(frozen=True, slots=True)
class _RunnerMaintenance:
    job_id: str
    acknowledgement: asyncio.Future[None] | None


@dataclass(slots=True)
class _ClientConnection:
    websocket: RelayWebSocket
    grant: RunnerTransferGrant
    outbound_queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_RELAY_QUEUE_FRAMES_MAX)
    )
    ciphertext_bytes: int = 0


def _get_running_maintenance_operation(
    runner_id: str,
) -> ManagedBackupOperation | None:
    """Load one durable source-runner fence without coupling relay import startup."""
    from yinshi.services.managed_backups import (
        get_running_managed_backup_operation_for_runner,
    )

    return get_running_managed_backup_operation_for_runner(runner_id)


class RunnerRelayBroker:
    """Route framed ciphertext between one outbound runner and bounded clients."""

    def __init__(
        self,
        *,
        get_running_operation: Callable[[str], ManagedBackupOperation | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if get_running_operation is not None and not callable(get_running_operation):
            raise TypeError("get_running_operation must be callable or None")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._get_running_operation = get_running_operation
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._runners: dict[str, _RunnerConnection] = {}
        self._maintenance: dict[str, _RunnerMaintenance] = {}
        self._clients: dict[str, _ClientConnection] = {}

    def is_runner_connected(self, runner_id: str) -> bool:
        """Return whether this event loop currently owns a runner socket."""
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id must not be empty")
        return runner_id in self._runners

    async def register_runner(self, runner_id: str, websocket: RelayWebSocket) -> None:
        """Register one current outbound runner connection, replacing stale state."""
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id must not be empty")
        durable_operation = (
            None if self._get_running_operation is None else self._get_running_operation(runner_id)
        )
        durable_job_id = None if durable_operation is None else durable_operation.job_id
        clients_to_close: list[_ClientConnection] = []
        async with self._lock:
            previous = self._runners.get(runner_id)
            if previous is not None:
                for transfer_id in tuple(previous.sessions):
                    client = self._clients.pop(transfer_id, None)
                    if client is not None:
                        self._close_client_queue(client)
                        clients_to_close.append(client)
            self._runners[runner_id] = _RunnerConnection(websocket=websocket)
            maintenance = self._maintenance.get(runner_id)
            maintenance_job_id = None if maintenance is None else maintenance.job_id
            if durable_job_id is not None and maintenance is None:
                acknowledgement = asyncio.get_running_loop().create_future()
                self._maintenance[runner_id] = _RunnerMaintenance(
                    durable_job_id,
                    acknowledgement,
                )
                maintenance_job_id = durable_job_id
        if previous is not None:
            await previous.websocket.close(code=4001, reason="Runner connection replaced")
        for client in clients_to_close:
            await client.websocket.close(code=4002, reason="Runner connection replaced")
        if maintenance_job_id is not None:
            control_message = '{"job_id":"' + maintenance_job_id + '","type":"quiesce"}'
            async with self._runners[runner_id].send_lock:
                await websocket.send_text(control_message)

    async def unregister_runner(self, runner_id: str, websocket: RelayWebSocket) -> None:
        """Remove only the matching runner connection and close attached clients."""
        clients_to_close: list[_ClientConnection] = []
        async with self._lock:
            runner = self._runners.get(runner_id)
            if runner is None or runner.websocket is not websocket:
                return
            del self._runners[runner_id]
            for transfer_id in tuple(runner.sessions):
                client = self._clients.pop(transfer_id, None)
                if client is not None:
                    self._close_client_queue(client)
                    clients_to_close.append(client)
        for client in clients_to_close:
            await client.websocket.close(code=4002, reason="Runner disconnected")

    async def quiesce_runner(
        self,
        runner_id: str,
        *,
        job_id: str,
        timeout_seconds: float,
    ) -> None:
        """Request exact runner quiescence and await its matching acknowledgement."""
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id must not be empty")
        normalized_job_id = _require_transfer_id(job_id)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300
        ):
            raise ValueError("timeout_seconds must be between 0 and 300")
        clients_to_close: list[_ClientConnection] = []
        async with self._lock:
            runner = self._runners.get(runner_id)
            if runner is None:
                raise RunnerRelayAuthorizationError("Runner is not connected")
            existing_maintenance = self._maintenance.get(runner_id)
            if existing_maintenance is not None:
                if existing_maintenance.job_id != normalized_job_id:
                    raise RunnerRelayAuthorizationError("Runner maintenance is already active")
                acknowledgement = existing_maintenance.acknowledgement
                if acknowledgement is None or acknowledgement.done():
                    return
                maintenance = existing_maintenance
            else:
                acknowledgement = asyncio.get_running_loop().create_future()
                maintenance = _RunnerMaintenance(normalized_job_id, acknowledgement)
                self._maintenance[runner_id] = maintenance
            for transfer_id in tuple(runner.sessions):
                client = self._clients.pop(transfer_id, None)
                runner.sessions.discard(transfer_id)
                self._retire_transfer(runner, transfer_id)
                if client is not None:
                    self._close_client_queue(client)
                    clients_to_close.append(client)
        for client in clients_to_close:
            await client.websocket.close(code=4006, reason="Runner entered maintenance")
        control_message = '{"job_id":"' + normalized_job_id + '","type":"quiesce"}'
        try:
            async with runner.send_lock:
                await runner.websocket.send_text(control_message)
            await asyncio.wait_for(
                asyncio.shield(acknowledgement),
                timeout=float(timeout_seconds),
            )
        except BaseException:
            async with self._lock:
                current = self._maintenance.get(runner_id)
                if current is maintenance:
                    del self._maintenance[runner_id]
            raise

    async def runner_quiesced(self, runner_id: str, job_id: str) -> None:
        """Accept one exact maintenance acknowledgement from the current runner."""
        normalized_job_id = _require_transfer_id(job_id)
        async with self._lock:
            maintenance = self._maintenance.get(runner_id)
            if maintenance is None or maintenance.job_id != normalized_job_id:
                raise RunnerRelayAuthorizationError("Runner maintenance acknowledgement mismatched")
            acknowledgement = maintenance.acknowledgement
            if acknowledgement is not None and not acknowledgement.done():
                acknowledgement.set_result(None)

    async def release_maintenance(self, runner_id: str, *, job_id: str) -> None:
        """Release only the matching maintenance fence on one current runner."""
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id must not be empty")
        normalized_job_id = _require_transfer_id(job_id)
        async with self._lock:
            maintenance = self._maintenance.get(runner_id)
            if maintenance is None or maintenance.job_id != normalized_job_id:
                raise RunnerRelayAuthorizationError("Runner maintenance release mismatched")
            acknowledgement = maintenance.acknowledgement
            if acknowledgement is not None and not acknowledgement.done():
                raise RunnerRelayAuthorizationError("Runner maintenance is not quiesced")
            del self._maintenance[runner_id]

    async def attach_client(
        self,
        grant: RunnerTransferGrant,
        websocket: RelayWebSocket,
    ) -> None:
        """Attach one claimed client only while its runner is connected and bounded."""
        if not isinstance(grant, RunnerTransferGrant):
            raise TypeError("grant must be RunnerTransferGrant")
        async with self._lock:
            runner = self._runners.get(grant.runner_id)
            if runner is None:
                raise RunnerRelayAuthorizationError("Runner is not connected")
            if grant.runner_id in self._maintenance:
                raise RunnerRelayAuthorizationError("Runner maintenance is active")
            if len(runner.sessions) >= _RUNNER_SESSIONS_MAX:
                raise RunnerRelayAuthorizationError("Runner session limit was reached")
            if grant.transfer_id in self._clients:
                raise RunnerRelayAuthorizationError("Runner relay client is already attached")
            runner.sessions.add(grant.transfer_id)
            self._clients[grant.transfer_id] = _ClientConnection(
                websocket=websocket,
                grant=grant,
            )
        control_message = '{"transfer_id":"' + grant.transfer_id + '","type":"open"}'
        try:
            async with runner.send_lock:
                await runner.websocket.send_text(control_message)
        except BaseException:
            await self.detach_client(grant.transfer_id, websocket)
            raise

    async def detach_client(self, transfer_id: str, websocket: RelayWebSocket) -> None:
        """Detach one matching client and notify its current runner."""
        normalized_transfer_id = _require_transfer_id(transfer_id)
        runner: _RunnerConnection | None = None
        async with self._lock:
            client = self._clients.get(normalized_transfer_id)
            if client is None or client.websocket is not websocket:
                return
            del self._clients[normalized_transfer_id]
            self._close_client_queue(client)
            runner = self._runners.get(client.grant.runner_id)
            if runner is not None:
                runner.sessions.discard(normalized_transfer_id)
                self._retire_transfer(runner, normalized_transfer_id)
        if runner is not None:
            control_message = '{"transfer_id":"' + normalized_transfer_id + '","type":"close"}'
            async with runner.send_lock:
                await runner.websocket.send_text(control_message)

    async def client_frame(self, transfer_id: str, ciphertext: bytes) -> None:
        """Forward one bounded client ciphertext frame with an opaque UUID prefix."""
        normalized_transfer_id = _require_transfer_id(transfer_id)
        if not isinstance(ciphertext, bytes):
            raise TypeError("ciphertext must be bytes")
        if not 1 <= len(ciphertext) <= _RELAY_FRAME_BYTES_MAX:
            raise ValueError("Relay ciphertext frame has an invalid length")
        async with self._lock:
            client = self._clients.get(normalized_transfer_id)
            if client is None:
                raise RunnerRelayAuthorizationError("Runner relay client is not attached")
            runner = self._runners.get(client.grant.runner_id)
            if runner is None:
                raise RunnerRelayAuthorizationError("Runner is not connected")
            self._record_bytes(client, len(ciphertext))
            framed_ciphertext = uuid.UUID(normalized_transfer_id).bytes + ciphertext
        async with runner.send_lock:
            await runner.websocket.send_bytes(framed_ciphertext)

    async def runner_closed_transfer(self, runner_id: str, transfer_id: str) -> None:
        """Close one transfer idempotently without disrupting other runner sessions."""
        normalized_transfer_id = _require_transfer_id(transfer_id)
        async with self._lock:
            runner = self._runners.get(runner_id)
            if runner is None:
                raise RunnerRelayAuthorizationError("Runner relay transfer is not attached")
            client = self._clients.get(normalized_transfer_id)
            if client is None:
                return
            if client.grant.runner_id != runner_id:
                raise RunnerRelayAuthorizationError("Runner relay transfer is not attached")
            del self._clients[normalized_transfer_id]
            runner.sessions.discard(normalized_transfer_id)
            self._retire_transfer(runner, normalized_transfer_id)
            self._close_client_queue(client)
        await client.websocket.close(code=4004, reason="Runner rejected transfer")

    async def runner_frame(self, runner_id: str, framed_ciphertext: bytes) -> None:
        """Queue one bounded runner ciphertext frame for its attached client."""
        if not isinstance(framed_ciphertext, bytes):
            raise TypeError("framed_ciphertext must be bytes")
        if (
            not _RUNNER_PREFIX_LENGTH
            < len(framed_ciphertext)
            <= (_RUNNER_PREFIX_LENGTH + _RELAY_FRAME_BYTES_MAX)
        ):
            raise ValueError("Runner relay frame has an invalid length")
        transfer_id = str(uuid.UUID(bytes=framed_ciphertext[:_RUNNER_PREFIX_LENGTH]))
        ciphertext = framed_ciphertext[_RUNNER_PREFIX_LENGTH:]
        close_reason: str | None = None
        async with self._lock:
            runner = self._runners.get(runner_id)
            if runner is None:
                raise RunnerRelayAuthorizationError("Runner relay frame has no attached client")
            client = self._clients.get(transfer_id)
            if client is None or client.grant.runner_id != runner_id:
                if self._is_retired_transfer(runner, transfer_id):
                    return
                raise RunnerRelayAuthorizationError("Runner relay frame has no attached client")
            if client.ciphertext_bytes + len(ciphertext) > client.grant.max_session_bytes:
                close_reason = "Runner relay session exceeded byte limit"
            elif client.outbound_queue.full():
                close_reason = "Runner relay client exceeded backpressure"
            else:
                self._record_bytes(client, len(ciphertext))
                client.outbound_queue.put_nowait(ciphertext)
            if close_reason is not None:
                del self._clients[transfer_id]
                runner.sessions.discard(transfer_id)
                self._retire_transfer(runner, transfer_id)
                self._close_client_queue(client)
        if close_reason is not None:
            control_message = '{"transfer_id":"' + transfer_id + '","type":"close"}'
            async with runner.send_lock:
                await runner.websocket.send_text(control_message)
            await client.websocket.close(code=4005, reason=close_reason)

    async def send_client_frames(self, transfer_id: str) -> None:
        """Drain runner ciphertext to one client until task cancellation."""
        normalized_transfer_id = _require_transfer_id(transfer_id)
        while True:
            async with self._lock:
                client = self._clients.get(normalized_transfer_id)
                if client is None:
                    return
                queue = client.outbound_queue
                websocket = client.websocket
            ciphertext = await queue.get()
            if not ciphertext:
                return
            await websocket.send_bytes(ciphertext)

    async def disconnect_runner(self, runner_id: str) -> None:
        """Remove and close a runner plus every client immediately after revocation."""
        clients_to_close: list[_ClientConnection] = []
        async with self._lock:
            runner = self._runners.pop(runner_id, None)
            self._maintenance.pop(runner_id, None)
            if runner is None:
                return
            for transfer_id in tuple(runner.sessions):
                client = self._clients.pop(transfer_id, None)
                if client is not None:
                    self._close_client_queue(client)
                    clients_to_close.append(client)
        await runner.websocket.close(code=4003, reason="Runner revoked")
        for client in clients_to_close:
            await client.websocket.close(code=4003, reason="Runner revoked")

    def _retire_transfer(
        self,
        runner: _RunnerConnection,
        transfer_id: str,
    ) -> None:
        """Retain one bounded connection-local marker for late runner traffic."""
        now = self._monotonic()
        self._prune_retired_transfers(runner, now=now)
        runner.retired_transfers[transfer_id] = now
        runner.retired_transfers.move_to_end(transfer_id)
        while len(runner.retired_transfers) > _RETIRED_TRANSFERS_MAX:
            runner.retired_transfers.popitem(last=False)

    def _is_retired_transfer(
        self,
        runner: _RunnerConnection,
        transfer_id: str,
    ) -> bool:
        """Return whether this runner recently owned one detached transfer."""
        self._prune_retired_transfers(runner, now=self._monotonic())
        return transfer_id in runner.retired_transfers

    @staticmethod
    def _prune_retired_transfers(
        runner: _RunnerConnection,
        *,
        now: float,
    ) -> None:
        """Discard expired markers without scanning beyond their ordered prefix."""
        while runner.retired_transfers:
            _, retired_at = next(iter(runner.retired_transfers.items()))
            if now - retired_at <= _RETIRED_TRANSFER_TTL_SECONDS:
                return
            runner.retired_transfers.popitem(last=False)

    @staticmethod
    def _close_client_queue(client: _ClientConnection) -> None:
        """Wake one sender task, dropping stale queued frames during teardown."""
        while True:
            try:
                client.outbound_queue.put_nowait(b"")
                return
            except asyncio.QueueFull:
                client.outbound_queue.get_nowait()

    @staticmethod
    def _record_bytes(client: _ClientConnection, count: int) -> None:
        """Apply one shared bidirectional ciphertext byte budget."""
        if count < 1:
            raise ValueError("Relay ciphertext byte count must be positive")
        if client.ciphertext_bytes + count > client.grant.max_session_bytes:
            raise RunnerRelayAuthorizationError("Runner relay session exceeded byte limit")
        client.ciphertext_bytes += count


runner_relay_broker = RunnerRelayBroker(get_running_operation=_get_running_maintenance_operation)
