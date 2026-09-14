"""Authenticated broker lifecycle orchestration."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from yinshi.services.broker_journal import BrokerJournal, JournalCommitAbsent
from yinshi.services.broker_protocol import (
    BrokerProtocolError,
    BrokerRequest,
    JsonValue,
    create_signed_response,
    parse_signed_request,
)
from yinshi.services.broker_replica_journal import (
    ReplicaAuthority,
    replica_authority_from_request,
)
from yinshi.services.broker_replica_journal_v2 import (
    ReplicaDrainContinuation,
    replica_drain_continuation_from_request,
)
from yinshi.services.broker_replica_lifecycle_v2 import (
    BrokerReplicaLifecycleCoordinatorV2,
)

_SUPPORTED_REQUEST_TYPE = "executor.launch"
_REPLICA_REQUEST_TYPE = "replica.lifecycle"
_REPLICA_DRAIN_REQUEST_TYPE = "replica.drain"
_FORBIDDEN_LAUNCH_FIELDS = frozenset(
    {
        "capabilities",
        "cgroup",
        "command",
        "commands",
        "environment",
        "executable",
        "gid",
        "home",
        "mount",
        "mounts",
        "path",
        "paths",
        "properties",
        "replica",
        "socket",
        "systemd_properties",
        "uid",
        "unit",
    }
)
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_RETRY_WAIT_SECONDS_DEFAULT = 5.0
_RETRY_WAIT_SECONDS_MAX = 30.0
_LAUNCH_TIMEOUT_SECONDS_DEFAULT = 30.0
_LAUNCH_TIMEOUT_SECONDS_MAX = 300.0
_RETRY_POLL_SECONDS = 0.02
_OWNER_TOKEN_BYTES = 32
_MAX_ABANDONED_EFFECTS = 64


class LauncherTransportError(RuntimeError):
    """Report an unconfirmed launcher transport outcome."""


class LauncherTimeoutError(RuntimeError):
    """Report an unconfirmed launcher deadline without a transport verdict."""


class LauncherVerificationError(RuntimeError):
    """Report an authenticated launcher response that cannot be verified."""


@dataclass(frozen=True, slots=True)
class LauncherResult:
    status: str
    error: str | None


class LauncherClient(Protocol):
    async def prepare(self, operation_id: str) -> LauncherResult: ...

    async def launch(self, operation_id: str) -> LauncherResult: ...

    async def reclaim(self, operation_id: str) -> LauncherResult: ...


class BrokerRuntimeManager(Protocol):
    def setup(self, operation_id: str) -> object: ...


def _contains_forbidden_control(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_contains_forbidden_control(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_LAUNCH_FIELDS or _contains_forbidden_control(item):
                return True
    return False


def _confirmed_launcher_result(value: object, *, stage: str) -> LauncherResult | None:
    if type(value) is not LauncherResult:
        return None
    expected = "prepared" if stage == "prepare" else "launched"
    if value.status == expected and value.error is None:
        return value
    if (
        value.status == "rejected"
        and isinstance(value.error, str)
        and _ERROR_CODE_PATTERN.fullmatch(value.error) is not None
    ):
        return value
    return None


class BrokerService:
    """Enforce authentication, durable stages, and one lifecycle owner."""

    def __init__(
        self,
        *,
        broker_incarnation: str,
        database_incarnation: str,
        application_uid: int,
        application_public_key: Ed25519PublicKey,
        broker_private_key: Ed25519PrivateKey,
        journal: BrokerJournal,
        launcher: LauncherClient,
        runtime: BrokerRuntimeManager,
        retry_wait_seconds: float = _RETRY_WAIT_SECONDS_DEFAULT,
        launch_timeout_seconds: float = _LAUNCH_TIMEOUT_SECONDS_DEFAULT,
    ) -> None:
        if type(application_uid) is not int or application_uid < 0:
            raise ValueError("application UID must be a non-negative integer")
        if (
            type(retry_wait_seconds) not in {int, float}
            or not 0 < retry_wait_seconds <= _RETRY_WAIT_SECONDS_MAX
        ):
            raise ValueError("retry wait seconds must be a positive bounded number")
        if (
            type(launch_timeout_seconds) not in {int, float}
            or not 0 < launch_timeout_seconds <= _LAUNCH_TIMEOUT_SECONDS_MAX
        ):
            raise ValueError("launch timeout seconds must be a positive bounded number")
        self._broker_incarnation = broker_incarnation
        self._database_incarnation = database_incarnation
        self._application_uid = application_uid
        self._application_public_key = application_public_key
        self._broker_private_key = broker_private_key
        self._journal = journal
        self._launcher = launcher
        self._runtime = runtime
        self._retry_wait_seconds = float(retry_wait_seconds)
        self._launch_timeout_seconds = float(launch_timeout_seconds)
        self._owner_token = secrets.token_urlsafe(_OWNER_TOKEN_BYTES)
        self._abandoned_effects: set[asyncio.Task[LauncherResult]] = set()

    @property
    def application_uid(self) -> int:
        return self._application_uid

    @property
    def broker_public_key(self) -> Ed25519PublicKey:
        return self._broker_private_key.public_key()

    def _authenticate(self, frame: bytes, *, peer_uid: int) -> BrokerRequest:
        if type(peer_uid) is not int or peer_uid != self._application_uid:
            raise BrokerProtocolError("broker peer UID is not authorized")
        request = parse_signed_request(frame, public_key=self._application_public_key)
        if request.broker_incarnation != self._broker_incarnation:
            raise BrokerProtocolError("broker incarnation is stale")
        if request.database_incarnation != self._database_incarnation:
            raise BrokerProtocolError("database incarnation is stale")
        if request.request_type != _SUPPORTED_REQUEST_TYPE:
            raise BrokerProtocolError("broker request type is not supported")
        if _contains_forbidden_control(request.payload):
            raise BrokerProtocolError("request contains forbidden launch control")
        return request

    def _confirmed_response(
        self, request: BrokerRequest, result: LauncherResult, *, stage: str
    ) -> bytes:
        return create_signed_response(
            request,
            private_key=self._broker_private_key,
            status="ok" if result.status == "launched" else "error",
            error=None if result.status == "launched" else result.error,
            result={"launcher_status": result.status, "lifecycle_stage": stage},
        )

    def _unresolved_response(self, request: BrokerRequest, *, stage: str) -> bytes:
        return create_signed_response(
            request,
            private_key=self._broker_private_key,
            status="error",
            error="launcher_outcome_unresolved",
            result={"launcher_status": "unresolved", "lifecycle_stage": stage},
        )

    def _record_unresolved(self, request: BrokerRequest, *, stage: str, reason: str) -> bytes:
        response = self._unresolved_response(request, stage=stage)
        self._journal.record_stage_unresolved(
            request,
            owner_token=self._owner_token,
            stage=stage,
            reason=reason,
            response_frame=response,
        )
        return response

    def _consume_abandoned(self, task: asyncio.Task[LauncherResult]) -> None:
        self._abandoned_effects.discard(task)
        if not task.cancelled():
            task.exception()

    def _abandon(self, task: asyncio.Task[LauncherResult]) -> None:
        self._abandoned_effects.add(task)
        task.add_done_callback(self._consume_abandoned)

    async def _root_effect(
        self,
        request: BrokerRequest,
        *,
        stage: str,
        effect: object,
    ) -> LauncherResult | bytes:
        if len(self._abandoned_effects) >= _MAX_ABANDONED_EFFECTS:
            return self._record_unresolved(request, stage=stage, reason="timeout_unknown")
        callable_effect = effect
        task = asyncio.create_task(callable_effect(request.operation_id))  # type: ignore[operator]
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._launch_timeout_seconds)
        except asyncio.CancelledError:
            self._abandon(task)
            return self._record_unresolved(request, stage=stage, reason="cancellation_unknown")
        if task not in done:
            self._abandon(task)
            return self._record_unresolved(request, stage=stage, reason="timeout_unknown")
        try:
            raw_result = task.result()
        except asyncio.CancelledError:
            return self._record_unresolved(request, stage=stage, reason="cancellation_unknown")
        except LauncherTimeoutError:
            return self._record_unresolved(request, stage=stage, reason="timeout_unknown")
        except LauncherVerificationError:
            return self._record_unresolved(request, stage=stage, reason="verification_unknown")
        except (LauncherTransportError, OSError):
            return self._record_unresolved(request, stage=stage, reason="transport_unknown")
        except Exception:  # noqa: BLE001
            return self._record_unresolved(request, stage=stage, reason="verification_unknown")
        result = _confirmed_launcher_result(raw_result, stage=stage)
        if result is None:
            return self._record_unresolved(request, stage=stage, reason="verification_unknown")
        return result

    def _persist_outcome(
        self,
        request: BrokerRequest,
        *,
        stage: str,
        stage_status: str,
        stage_error: str | None = None,
        response_frame: bytes | None = None,
    ) -> bytes | None:
        try:
            self._journal.record_stage_outcome(
                request,
                owner_token=self._owner_token,
                stage=stage,
                stage_status=stage_status,
                stage_error=stage_error,
                response_frame=response_frame,
            )
        except JournalCommitAbsent:
            return self._record_unresolved(request, stage=stage, reason="sqlite_commit_unknown")
        return response_frame

    async def _finalize_lifecycle(self, request: BrokerRequest) -> bytes:
        prepare = await self._root_effect(request, stage="prepare", effect=self._launcher.prepare)
        if isinstance(prepare, bytes):
            return prepare
        if prepare.status == "rejected":
            response = self._confirmed_response(request, prepare, stage="prepare")
            terminal_response = self._persist_outcome(
                request,
                stage="prepare",
                stage_status="rejected",
                stage_error=prepare.error,
                response_frame=response,
            )
            if terminal_response is None:
                raise RuntimeError("terminal prepare outcome has no response")
            return terminal_response
        terminal_response = self._persist_outcome(
            request,
            stage="prepare",
            stage_status="prepared",
        )
        if terminal_response is not None:
            return terminal_response

        self._journal.begin_stage(request, owner_token=self._owner_token, stage="runtime_setup")
        try:
            self._runtime.setup(request.operation_id)
        except Exception:  # noqa: BLE001
            return self._record_unresolved(
                request, stage="runtime_setup", reason="verification_unknown"
            )
        terminal_response = self._persist_outcome(
            request,
            stage="runtime_setup",
            stage_status="runtime_ready",
        )
        if terminal_response is not None:
            return terminal_response

        self._journal.begin_stage(request, owner_token=self._owner_token, stage="launch")
        launch = await self._root_effect(request, stage="launch", effect=self._launcher.launch)
        if isinstance(launch, bytes):
            return launch
        response = self._confirmed_response(request, launch, stage="launch")
        terminal_response = self._persist_outcome(
            request,
            stage="launch",
            stage_status=launch.status,
            stage_error=launch.error,
            response_frame=response,
        )
        if terminal_response is None:
            raise RuntimeError("terminal launch outcome has no response")
        return terminal_response

    async def _run_as_owner(self, request: BrokerRequest) -> bytes:
        self._journal.begin_stage(request, owner_token=self._owner_token, stage="prepare")
        finalizer = asyncio.create_task(self._finalize_lifecycle(request))
        request_was_cancelled = False
        while True:
            try:
                response = await asyncio.shield(finalizer)
                break
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    request_was_cancelled = True
                    current.uncancel()
                if finalizer.cancelled():
                    response = self._record_unresolved(
                        request, stage="prepare", reason="cancellation_unknown"
                    )
                    break
        if request_was_cancelled:
            raise asyncio.CancelledError
        return response

    async def _wait_for_owner(self, request: BrokerRequest) -> bytes:
        deadline = time.monotonic() + self._retry_wait_seconds
        while time.monotonic() < deadline:
            state = self._journal.status(request)
            if state.state in {"terminal", "unresolved"}:
                if state.response_frame is None:
                    raise RuntimeError("terminal broker journal entry has no response")
                return state.response_frame
            await asyncio.sleep(_RETRY_POLL_SECONDS)
        raise LauncherTransportError("broker lifecycle owner is still active")

    def recover_startup(self) -> None:
        """Persist unresolved results for lifecycle ownership lost at restart."""
        for incomplete in self._journal.incomplete_lifecycles():
            request = self._authenticate(
                incomplete.request_frame,
                peer_uid=self._application_uid,
            )
            if not incomplete.stage_started:
                self._journal.begin_stage(
                    request,
                    owner_token=incomplete.owner_token,
                    stage=incomplete.stage,
                )
            response = self._unresolved_response(request, stage=incomplete.stage)
            self._journal.record_stage_unresolved(
                request,
                owner_token=incomplete.owner_token,
                stage=incomplete.stage,
                reason="broker_restart_unknown",
                response_frame=response,
            )

    async def handle(self, frame: bytes, *, peer_uid: int) -> bytes:
        request = self._authenticate(frame, peer_uid=peer_uid)
        decision = self._journal.accept(request, frame)
        if decision.state in {"terminal", "unresolved"}:
            if decision.response_frame is None:
                raise RuntimeError("terminal broker journal entry has no response")
            return decision.response_frame
        if decision.state != "accepted":
            raise RuntimeError("broker journal returned an invalid transition")
        ownership = self._journal.claim_lifecycle(
            request,
            owner_token=self._owner_token,
            broker_incarnation=self._broker_incarnation,
        )
        if ownership.state == "claimed":
            return await self._run_as_owner(request)
        if ownership.state in {"terminal", "unresolved"}:
            if ownership.response_frame is None:
                raise RuntimeError("terminal broker journal entry has no response")
            return ownership.response_frame
        if ownership.state == "in_flight":
            return await self._wait_for_owner(request)
        raise RuntimeError("broker journal returned an invalid transition")


class BrokerControlService:
    """Authenticate and route one bounded broker control frame by request type."""

    def __init__(
        self,
        *,
        broker_incarnation: str,
        database_incarnation: str,
        application_uid: int,
        application_public_key: Ed25519PublicKey,
        broker_private_key: Ed25519PrivateKey,
        launch_service: BrokerService,
        replica_coordinator: BrokerReplicaLifecycleCoordinatorV2 | None,
        replica_lifecycle_enabled: bool,
        launch_request_timeout_seconds: float = 120.0,
        replica_lifecycle_timeout_seconds: float = 360.0,
    ) -> None:
        if (
            not isinstance(broker_incarnation, str)
            or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
            or not isinstance(database_incarnation, str)
            or _TOKEN_PATTERN.fullmatch(database_incarnation) is None
        ):
            raise ValueError("broker control incarnation is invalid")
        if type(application_uid) is not int or application_uid < 0:
            raise ValueError("application UID must be a non-negative integer")
        if not isinstance(application_public_key, Ed25519PublicKey):
            raise TypeError("application public key is invalid")
        if not isinstance(broker_private_key, Ed25519PrivateKey):
            raise TypeError("broker private key is invalid")
        if not callable(getattr(launch_service, "handle", None)):
            raise TypeError("launch service is invalid")
        if replica_coordinator is not None and not (
            callable(getattr(replica_coordinator, "start", None))
            and callable(getattr(replica_coordinator, "continue_drain", None))
        ):
            raise TypeError("replica coordinator is invalid")
        if type(replica_lifecycle_enabled) is not bool:
            raise TypeError("replica lifecycle gate is invalid")
        if (
            type(launch_request_timeout_seconds) not in {int, float}
            or not 0 < launch_request_timeout_seconds <= 3_600
            or type(replica_lifecycle_timeout_seconds) not in {int, float}
            or not 0 < replica_lifecycle_timeout_seconds <= 3_600
        ):
            raise ValueError("broker control request timeout is invalid")
        if replica_lifecycle_enabled and replica_coordinator is None:
            raise ValueError("enabled replica lifecycle requires a coordinator")
        self._broker_incarnation = broker_incarnation
        self._database_incarnation = database_incarnation
        self._application_uid = application_uid
        self._application_public_key = application_public_key
        self._broker_private_key = broker_private_key
        self._launch_service = launch_service
        self._replica_coordinator = replica_coordinator
        self._replica_lifecycle_enabled = replica_lifecycle_enabled
        self._launch_request_timeout_seconds = float(launch_request_timeout_seconds)
        self._replica_lifecycle_timeout_seconds = float(replica_lifecycle_timeout_seconds)

    @property
    def application_uid(self) -> int:
        return self._application_uid

    def _authenticate(self, frame: bytes, *, peer_uid: int) -> BrokerRequest:
        if type(peer_uid) is not int or peer_uid != self._application_uid:
            raise BrokerProtocolError("broker peer UID is not authorized")
        request = parse_signed_request(frame, public_key=self._application_public_key)
        if request.broker_incarnation != self._broker_incarnation:
            raise BrokerProtocolError("broker incarnation is stale")
        if request.database_incarnation != self._database_incarnation:
            raise BrokerProtocolError("database incarnation is stale")
        return request

    def _disabled_replica_response(self, request: BrokerRequest) -> bytes:
        return create_signed_response(
            request,
            private_key=self._broker_private_key,
            status="error",
            error="replica_lifecycle_disabled",
            result={
                "stage": "ingest",
                "state": "rejected",
                "code": "replica_lifecycle_disabled",
            },
        )

    async def handle(self, frame: bytes, *, peer_uid: int) -> bytes:
        """Route authenticated launch and replica frames without schema overlap."""
        request = self._authenticate(frame, peer_uid=peer_uid)
        if request.request_type == _SUPPORTED_REQUEST_TYPE:
            return await asyncio.wait_for(
                self._launch_service.handle(frame, peer_uid=peer_uid),
                timeout=self._launch_request_timeout_seconds,
            )
        if request.request_type not in (
            _REPLICA_REQUEST_TYPE,
            _REPLICA_DRAIN_REQUEST_TYPE,
        ):
            raise BrokerProtocolError("broker request type is not supported")
        if not self._replica_lifecycle_enabled:
            return self._disabled_replica_response(request)
        coordinator = self._replica_coordinator
        if coordinator is None:
            raise RuntimeError("enabled replica lifecycle has no coordinator")
        if request.request_type == _REPLICA_REQUEST_TYPE:
            try:
                authority: ReplicaAuthority = replica_authority_from_request(request)
            except (TypeError, ValueError) as error:
                raise BrokerProtocolError("replica lifecycle payload is invalid") from error
            return await asyncio.wait_for(
                coordinator.start(request, frame, authority),
                timeout=self._replica_lifecycle_timeout_seconds,
            )
        try:
            continuation: ReplicaDrainContinuation = replica_drain_continuation_from_request(
                request
            )
        except (TypeError, ValueError) as error:
            raise BrokerProtocolError("replica drain payload is invalid") from error
        return await asyncio.wait_for(
            coordinator.continue_drain(request, frame, continuation),
            timeout=self._replica_lifecycle_timeout_seconds,
        )
