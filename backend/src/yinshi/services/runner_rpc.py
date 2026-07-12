"""Strict encrypted RPC envelope for the restricted BYOC worker surface."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from yinshi.services.runner_capabilities import RUNNER_PROTOCOL_VERSION
from yinshi.services.runner_noise_session import RunnerNoiseSession

_REQUEST_KEYS = {"body", "method", "path", "request_id", "sequence", "type", "v"}
_REQUEST_BYTES_MAX = 49_152


@dataclass(frozen=True, slots=True)
class RunnerRpcRequest:
    """Validated ordered request decrypted only inside the user's runner."""

    sequence: int
    request_id: str
    method: str
    path: str
    body: Any


def _parse_request(plaintext: bytes, *, expected_sequence: int) -> RunnerRpcRequest:
    """Parse one exact canonical RPC shape and enforce application ordering."""
    if not isinstance(plaintext, bytes):
        raise TypeError("Runner RPC plaintext must be bytes")
    if not plaintext or len(plaintext) > _REQUEST_BYTES_MAX:
        raise ValueError("Runner RPC request has an invalid length")
    try:
        payload = json.loads(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Runner RPC request is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _REQUEST_KEYS:
        raise ValueError("Runner RPC request has an invalid shape")
    if payload.get("v") != 1 or payload.get("type") != "request":
        raise ValueError("Runner RPC request has an unsupported version or type")
    sequence = payload.get("sequence")
    if type(sequence) is not int or sequence != expected_sequence:
        raise ValueError("Runner RPC request sequence is out of order")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Runner RPC request_id must not be empty")
    try:
        normalized_request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise ValueError("Runner RPC request_id must be a UUID") from exc
    if normalized_request_id != request_id:
        raise ValueError("Runner RPC request_id must be canonical")
    method = payload.get("method")
    path = payload.get("path")
    if not isinstance(method, str) or not method:
        raise ValueError("Runner RPC method must not be empty")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Runner RPC path must be absolute")
    if "?" in path or "#" in path or ".." in path:
        raise ValueError("Runner RPC path must be normalized")
    return RunnerRpcRequest(
        sequence=sequence,
        request_id=normalized_request_id,
        method=method,
        path=path,
        body=payload.get("body"),
    )


class EncryptedRunnerRpcSession:
    """Perform one capability handshake, then dispatch ordered encrypted RPCs."""

    def __init__(self, *, transfer_id: str, noise_session: RunnerNoiseSession) -> None:
        if not isinstance(transfer_id, str) or not transfer_id:
            raise ValueError("transfer_id must not be empty")
        try:
            normalized_transfer_id = str(uuid.UUID(transfer_id))
        except ValueError as exc:
            raise ValueError("transfer_id must be a UUID") from exc
        if normalized_transfer_id != transfer_id:
            raise ValueError("transfer_id must be canonical")
        if not isinstance(noise_session, RunnerNoiseSession):
            raise TypeError("noise_session must be RunnerNoiseSession")
        self._transfer_id = normalized_transfer_id
        self._noise_session = noise_session
        self._established = False
        self._failed = False
        self._next_request_sequence = 0

    def handle_frame(self, ciphertext: bytes, *, current_time: int) -> bytes:
        """Accept a handshake or decrypt, dispatch, and encrypt one RPC response."""
        if self._failed:
            raise RuntimeError("Runner RPC session failed and cannot be reused")
        if type(current_time) is not int or current_time < 0:
            raise ValueError("current_time must be a non-negative integer")
        if not self._established:
            return self._accept_handshake(ciphertext, current_time=current_time)

        try:
            plaintext = self._noise_session.decrypt(ciphertext)
            request = _parse_request(
                plaintext,
                expected_sequence=self._next_request_sequence,
            )
            response_body = self._dispatch(request)
            response = json.dumps(
                {
                    "body": response_body,
                    "request_id": request.request_id,
                    "sequence": request.sequence,
                    "status": 200,
                    "type": "response",
                    "v": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self._next_request_sequence += 1
            return self._noise_session.encrypt(response)
        except (TypeError, ValueError):
            self._failed = True
            raise

    def _accept_handshake(self, message: bytes, *, current_time: int) -> bytes:
        """Bind the relay transfer ID to the signed capability inside Noise."""
        try:
            response = self._noise_session.accept_handshake(
                message,
                current_time=current_time,
            )
            if self._noise_session.capability.transfer_id != self._transfer_id:
                raise ValueError("Runner capability transfer_id does not match relay routing")
            self._established = True
            return response
        except (TypeError, ValueError):
            self._failed = True
            raise

    def _dispatch(self, request: RunnerRpcRequest) -> dict[str, str]:
        """Dispatch only methods exposed by the restricted worker contract."""
        capability = self._noise_session.capability
        if request.method == "GET" and request.path == "/health":
            if request.body is not None:
                raise ValueError("Runner health request body must be null")
            if "worker.health" not in capability.scopes:
                raise ValueError("Runner capability does not allow worker.health")
            return {
                "protocol": RUNNER_PROTOCOL_VERSION,
                "status": "ok",
            }
        raise ValueError("Runner RPC method or path is not allowed")
