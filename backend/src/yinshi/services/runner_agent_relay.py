"""Runner-side multiplexing for control messages and encrypted RPC frames."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from yinshi.services.runner_noise_session import (
    RunnerCapabilityReplayStore,
    RunnerNoiseSession,
)
from yinshi.services.runner_rpc import DispatcherFactory, EncryptedRunnerRpcSession

_UUID_BYTES_LENGTH = 16
_RELAY_FRAME_BYTES_MAX = 65_535
_ACTIVE_TRANSFERS_MAX = 32
_WELCOME_KEYS = {"runner_id", "type"}
_TRANSFER_CONTROL_KEYS = {"transfer_id", "type"}


class RunnerRelaySessionError(ValueError):
    """One known transfer failed without invalidating the shared runner socket."""

    def __init__(self, transfer_id: str) -> None:
        super().__init__("Runner relay transfer frame was rejected")
        self.transfer_id = transfer_id


def _canonical_uuid(value: object, name: str) -> str:
    """Return a canonical UUID string from one untrusted control value."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    try:
        normalized_value = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    if normalized_value != value:
        raise ValueError(f"{name} must be canonical")
    return normalized_value


class RunnerAgentRelayRuntime:
    """Own connection-scoped runner identity and per-transfer encrypted sessions."""

    def __init__(
        self,
        *,
        runner_static_private_key: bytes,
        capability_signing_public_key: bytes,
        replay_database_path: Path,
        dispatcher_factory: DispatcherFactory | None = None,
    ) -> None:
        if not isinstance(runner_static_private_key, bytes) or len(runner_static_private_key) != 32:
            raise ValueError("runner_static_private_key must contain exactly 32 bytes")
        if not isinstance(capability_signing_public_key, bytes):
            raise TypeError("capability_signing_public_key must be bytes")
        if len(capability_signing_public_key) != 32:
            raise ValueError("capability_signing_public_key must contain exactly 32 bytes")
        if not isinstance(replay_database_path, Path):
            raise TypeError("replay_database_path must be a pathlib.Path")
        if dispatcher_factory is not None and not callable(dispatcher_factory):
            raise TypeError("dispatcher_factory must be callable or None")

        self._runner_static_private_key = bytes(runner_static_private_key)
        self._capability_signing_public_key = bytes(capability_signing_public_key)
        self._replay_store = RunnerCapabilityReplayStore(replay_database_path)
        self._dispatcher_factory = dispatcher_factory
        self._runner_id: str | None = None
        self._sessions: dict[str, EncryptedRunnerRpcSession] = {}

    @property
    def active_transfer_ids(self) -> tuple[str, ...]:
        """Return random routing IDs only, excluding capability and payload data."""
        return tuple(sorted(self._sessions))

    def handle_control(self, message: str) -> None:
        """Apply one exact welcome, open, or close relay control message."""
        if not isinstance(message, str) or not message or len(message) > 1_024:
            raise ValueError("Runner relay control message has an invalid length")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ValueError("Runner relay control message is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Runner relay control message has an invalid shape")
        message_type = payload.get("type")
        if message_type == "welcome":
            self._handle_welcome(payload)
            return
        if message_type in {"open", "close"}:
            self._handle_transfer_control(payload, message_type=message_type)
            return
        raise ValueError("Runner relay control message type is unsupported")

    async def handle_binary(self, frame: bytes, *, current_time: int) -> bytes:
        """Route one UUID-prefixed ciphertext frame through its encrypted RPC session."""
        if not isinstance(frame, bytes):
            raise TypeError("Runner relay frame must be bytes")
        if not _UUID_BYTES_LENGTH < len(frame) <= _UUID_BYTES_LENGTH + _RELAY_FRAME_BYTES_MAX:
            raise ValueError("Runner relay frame has an invalid length")
        if type(current_time) is not int or current_time < 0:
            raise ValueError("current_time must be a non-negative integer")
        transfer_id = str(uuid.UUID(bytes=frame[:_UUID_BYTES_LENGTH]))
        session = self._sessions.get(transfer_id)
        if session is None:
            raise ValueError("Runner relay transfer is not open")
        try:
            response = await session.handle_frame(
                frame[_UUID_BYTES_LENGTH:],
                current_time=current_time,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            del self._sessions[transfer_id]
            raise RunnerRelaySessionError(transfer_id) from exc
        return uuid.UUID(transfer_id).bytes + response

    def _handle_welcome(self, payload: dict[str, object]) -> None:
        """Set runner identity exactly once from authenticated relay state."""
        if set(payload) != _WELCOME_KEYS:
            raise ValueError("Runner relay welcome message has an invalid shape")
        runner_id = payload.get("runner_id")
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("Runner relay welcome runner_id must not be empty")
        if self._runner_id is not None:
            raise ValueError("Runner relay welcome was already received")
        self._runner_id = runner_id

    def _handle_transfer_control(
        self,
        payload: dict[str, object],
        *,
        message_type: str,
    ) -> None:
        """Open or close one bounded transfer after relay welcome."""
        if set(payload) != _TRANSFER_CONTROL_KEYS:
            raise ValueError("Runner relay transfer control has an invalid shape")
        if self._runner_id is None:
            raise ValueError("Runner relay welcome is required before transfers")
        transfer_id = _canonical_uuid(payload.get("transfer_id"), "transfer_id")
        if message_type == "close":
            if self._sessions.pop(transfer_id, None) is None:
                raise ValueError("Runner relay transfer is not open")
            return
        if transfer_id in self._sessions:
            raise ValueError("Runner relay transfer is already open")
        if len(self._sessions) >= _ACTIVE_TRANSFERS_MAX:
            raise ValueError("Runner relay active transfer limit was reached")
        noise_session = RunnerNoiseSession(
            runner_id=self._runner_id,
            runner_static_private_key=self._runner_static_private_key,
            capability_signing_public_key=self._capability_signing_public_key,
            replay_store=self._replay_store,
        )
        self._sessions[transfer_id] = EncryptedRunnerRpcSession(
            transfer_id=transfer_id,
            noise_session=noise_session,
            dispatcher_factory=self._dispatcher_factory,
        )
