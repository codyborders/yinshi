"""Strict encrypted RPC envelope for the restricted BYOC worker surface."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from yinshi.services.runner_capabilities import RUNNER_PROTOCOL_VERSION
from yinshi.services.runner_noise_session import RunnerNoiseSession
from yinshi.services.runner_rpc_transport import (
    NOISE_CIPHERTEXT_BYTES_MAX as _NOISE_CIPHERTEXT_BYTES_MAX,
)
from yinshi.services.runner_rpc_transport import NOISE_TAG_BYTES as _NOISE_TAG_BYTES
from yinshi.services.runner_rpc_transport import TRANSPORT_ACK as _TRANSPORT_ACK
from yinshi.services.runner_rpc_transport import TRANSPORT_HEADER as _TRANSPORT_HEADER
from yinshi.services.runner_rpc_transport import TRANSPORT_MAGIC as _TRANSPORT_MAGIC
from yinshi.services.runner_rpc_transport import (
    TRANSPORT_PAYLOAD_BYTES_MAX as _TRANSPORT_PAYLOAD_BYTES_MAX,
)
from yinshi.services.runner_rpc_transport import TRANSPORT_PULL as _TRANSPORT_PULL
from yinshi.services.runner_rpc_transport import TRANSPORT_REQUEST as _TRANSPORT_REQUEST
from yinshi.services.runner_rpc_transport import TRANSPORT_RESPONSE as _TRANSPORT_RESPONSE
from yinshi.services.runner_rpc_transport import (
    fragment_count,
)
from yinshi.worker_runtime import WorkerHttpDispatcher

_REQUEST_KEYS_V1 = {"body", "method", "path", "request_id", "sequence", "type", "v"}
_REQUEST_KEYS_V2 = _REQUEST_KEYS_V1 | {"query"}
_REQUEST_BYTES_MAX = 2 * 1_024 * 1_024
_RESPONSE_BYTES_MAX = 10 * 1_024 * 1_024
_NOISE_PLAINTEXT_BYTES_MAX = _NOISE_CIPHERTEXT_BYTES_MAX - _NOISE_TAG_BYTES
# Capability budgets count ciphertext for requests, acknowledgements, pulls, and responses.
# Callers must include Noise tags and transport headers in their requested budget.
_RESOURCE_ID = r"[0-9a-f]{32}"
_REPOSITORY_MEMBER_PATH = re.compile(rf"^/api/repos/{_RESOURCE_ID}$")
_WORKSPACE_COLLECTION_PATH = re.compile(rf"^/api/repos/{_RESOURCE_ID}/workspaces$")
_WORKSPACE_MEMBER_PATH = re.compile(rf"^/api/workspaces/{_RESOURCE_ID}$")
_SESSION_COLLECTION_PATH = re.compile(rf"^/api/workspaces/{_RESOURCE_ID}/sessions$")
_SESSION_MEMBER_PATH = re.compile(rf"^/api/sessions/{_RESOURCE_ID}$")
_SESSION_READ_PATH = re.compile(rf"^/api/sessions/{_RESOURCE_ID}/(?:messages|tree)$")
_PROMPT_RUN_COLLECTION_PATH = re.compile(rf"^/api/sessions/{_RESOURCE_ID}/runs$")
_PROMPT_RUN_EVENTS_PATH = re.compile(
    rf"^/api/sessions/{_RESOURCE_ID}/runs/{_RESOURCE_ID}/events/[0-9]{{1,6}}$"
)
_PROMPT_RUN_CANCEL_PATH = re.compile(rf"^/api/sessions/{_RESOURCE_ID}/runs/{_RESOURCE_ID}/cancel$")
_WORKSPACE_FILE_READ_PATH = re.compile(
    rf"^/api/workspaces/{_RESOURCE_ID}/files/(?:changed|diff|preview|tree)$"
)
_WORKSPACE_FILE_WRITE_PATH = re.compile(rf"^/api/workspaces/{_RESOURCE_ID}/files/content$")
_TERMINAL_COLLECTION_PATH = re.compile(rf"^/api/workspaces/{_RESOURCE_ID}/terminals$")
_TERMINAL_MEMBER_PATH = re.compile(rf"^/api/workspaces/{_RESOURCE_ID}/terminals/{_RESOURCE_ID}$")
_TERMINAL_ACTION_PATH = re.compile(
    rf"^/api/workspaces/{_RESOURCE_ID}/terminals/{_RESOURCE_ID}/(?:input|resize|restart)$"
)
_TERMINAL_EVENTS_PATH = re.compile(
    rf"^/api/workspaces/{_RESOURCE_ID}/terminals/{_RESOURCE_ID}/events/[0-9]{{1,10}}$"
)
_PROVIDER_CONNECTION_COLLECTION_PATH = "/api/settings/connections"
_PROVIDER_CONNECTION_MEMBER_PATH = re.compile(rf"^/api/settings/connections/{_RESOURCE_ID}$")
_PROVIDER_AUTH_START_PATH = re.compile(r"^/auth/providers/[a-z0-9-]{1,64}/start$")
_PROVIDER_AUTH_CALLBACK_PATH = re.compile(r"^/auth/providers/[a-z0-9-]{1,64}/callback$")
_PI_CONFIG_COLLECTION_PATH = "/api/settings/pi-config"
_PI_UPLOAD_COLLECTION_PATH = "/api/settings/pi-config/uploads"
_PI_UPLOAD_CHUNK_PATH = re.compile(
    rf"^/api/settings/pi-config/uploads/{_RESOURCE_ID}/chunks/[0-9]{{1,5}}$"
)
_PI_UPLOAD_COMPLETE_PATH = re.compile(rf"^/api/settings/pi-config/uploads/{_RESOURCE_ID}/complete$")
_PI_UPLOAD_MEMBER_PATH = re.compile(rf"^/api/settings/pi-config/uploads/{_RESOURCE_ID}$")
DispatcherFactory = Callable[[str], WorkerHttpDispatcher]


@dataclass(frozen=True, slots=True)
class RunnerRpcRequest:
    """Validated ordered request decrypted only inside the user's runner."""

    version: int
    sequence: int
    request_id: str
    method: str
    path: str
    body: Any
    query: dict[str, str]


def _parse_request(plaintext: bytes, *, expected_sequence: int) -> RunnerRpcRequest:
    """Parse one exact RPC shape and enforce application ordering."""
    if not isinstance(plaintext, bytes):
        raise TypeError("Runner RPC plaintext must be bytes")
    if not plaintext or len(plaintext) > _REQUEST_BYTES_MAX:
        raise ValueError("Runner RPC request has an invalid length")
    try:
        payload = json.loads(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Runner RPC request is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Runner RPC request has an invalid shape")
    version = payload.get("v")
    expected_keys = _REQUEST_KEYS_V1 if version == 1 else _REQUEST_KEYS_V2
    if version not in {1, 2} or set(payload) != expected_keys:
        raise ValueError("Runner RPC request has an invalid shape or version")
    if payload.get("type") != "request":
        raise ValueError("Runner RPC request has an unsupported type")
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
    if "?" in path or "#" in path or ".." in path or "\\" in path or "\x00" in path:
        raise ValueError("Runner RPC path must be normalized")
    query_payload = payload.get("query", {})
    if not isinstance(query_payload, dict) or len(query_payload) > 16:
        raise ValueError("Runner RPC query must be a bounded object")
    query: dict[str, str] = {}
    for key, value in query_payload.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("Runner RPC query key is invalid")
        if not key.replace("_", "").isalnum():
            raise ValueError("Runner RPC query key is invalid")
        if not isinstance(value, str) or len(value) > 2_048:
            raise ValueError("Runner RPC query value is invalid")
        query[key] = value
    return RunnerRpcRequest(
        version=version,
        sequence=sequence,
        request_id=normalized_request_id,
        method=method,
        path=path,
        body=payload.get("body"),
        query=query,
    )


def _required_scope(request: RunnerRpcRequest) -> str:
    """Map one exact worker route to its least-privilege capability scope."""
    if request.method == "GET" and request.path == "/health":
        return "worker.health"
    is_repository_collection = request.path == "/api/repos"
    is_repository_member = _REPOSITORY_MEMBER_PATH.fullmatch(request.path) is not None
    if request.method == "GET" and (is_repository_collection or is_repository_member):
        return "repository.read"
    if request.method in {"POST", "PATCH", "DELETE"}:
        if is_repository_collection or is_repository_member:
            return "repository.write"

    is_workspace_collection = _WORKSPACE_COLLECTION_PATH.fullmatch(request.path) is not None
    is_workspace_member = _WORKSPACE_MEMBER_PATH.fullmatch(request.path) is not None
    if request.method == "GET" and is_workspace_collection:
        return "workspace.read"
    if request.method == "POST" and is_workspace_collection:
        return "workspace.write"
    if request.method in {"PATCH", "DELETE"} and is_workspace_member:
        return "workspace.write"

    is_session_collection = _SESSION_COLLECTION_PATH.fullmatch(request.path) is not None
    is_session_member = _SESSION_MEMBER_PATH.fullmatch(request.path) is not None
    is_session_read = _SESSION_READ_PATH.fullmatch(request.path) is not None
    if request.method == "GET" and (is_session_collection or is_session_member or is_session_read):
        return "session.read"
    if request.method == "POST" and is_session_collection:
        return "session.write"
    if request.method == "PATCH" and is_session_member:
        return "session.write"

    is_prompt_run_collection = _PROMPT_RUN_COLLECTION_PATH.fullmatch(request.path) is not None
    is_prompt_run_events = _PROMPT_RUN_EVENTS_PATH.fullmatch(request.path) is not None
    is_prompt_run_cancel = _PROMPT_RUN_CANCEL_PATH.fullmatch(request.path) is not None
    if request.method == "POST" and (is_prompt_run_collection or is_prompt_run_cancel):
        return "session.stream"
    if request.method == "GET" and is_prompt_run_events:
        return "session.stream"
    if request.method == "GET" and _WORKSPACE_FILE_READ_PATH.fullmatch(request.path):
        return "files.read"
    if request.method == "PUT" and _WORKSPACE_FILE_WRITE_PATH.fullmatch(request.path):
        return "files.write"
    is_terminal_collection = _TERMINAL_COLLECTION_PATH.fullmatch(request.path) is not None
    is_terminal_member = _TERMINAL_MEMBER_PATH.fullmatch(request.path) is not None
    is_terminal_action = _TERMINAL_ACTION_PATH.fullmatch(request.path) is not None
    is_terminal_events = _TERMINAL_EVENTS_PATH.fullmatch(request.path) is not None
    if request.method == "POST" and (is_terminal_collection or is_terminal_action):
        return "terminal"
    if request.method == "GET" and is_terminal_events:
        return "terminal"
    if request.method == "DELETE" and is_terminal_member:
        return "terminal"
    is_provider_collection = request.path == _PROVIDER_CONNECTION_COLLECTION_PATH
    is_provider_member = _PROVIDER_CONNECTION_MEMBER_PATH.fullmatch(request.path) is not None
    if request.method in {"GET", "POST"} and is_provider_collection:
        return "provider.configure"
    if request.method == "DELETE" and is_provider_member:
        return "provider.configure"
    if request.method == "GET" and request.path == "/api/catalog":
        return "provider.configure"
    if request.method == "POST" and _PROVIDER_AUTH_START_PATH.fullmatch(request.path):
        return "provider.configure"
    if request.method in {"GET", "POST"} and _PROVIDER_AUTH_CALLBACK_PATH.fullmatch(request.path):
        return "provider.configure"
    if request.method in {"GET", "DELETE"} and request.path == _PI_CONFIG_COLLECTION_PATH:
        return "pi.configure"
    if request.method == "GET" and request.path in {
        "/api/settings/pi-config/commands",
        "/api/settings/pi-release-notes",
    }:
        return "pi.configure"
    if request.method == "POST" and request.path in {
        "/api/settings/pi-config/github",
        "/api/settings/pi-config/sync",
    }:
        return "pi.configure"
    if request.method == "PATCH" and request.path == "/api/settings/pi-config/categories":
        return "pi.configure"
    is_pi_upload_chunk = _PI_UPLOAD_CHUNK_PATH.fullmatch(request.path) is not None
    is_pi_upload_complete = _PI_UPLOAD_COMPLETE_PATH.fullmatch(request.path) is not None
    is_pi_upload_member = _PI_UPLOAD_MEMBER_PATH.fullmatch(request.path) is not None
    if request.method == "POST" and (
        request.path == _PI_UPLOAD_COLLECTION_PATH or is_pi_upload_chunk or is_pi_upload_complete
    ):
        return "pi.configure"
    if request.method == "DELETE" and is_pi_upload_member:
        return "pi.configure"
    raise ValueError("Runner RPC method or path is not allowed")


def _validate_route_query(request: RunnerRpcRequest) -> None:
    """Allow query values only on file routes whose contract requires a path."""
    if request.method == "GET" and _PROVIDER_AUTH_CALLBACK_PATH.fullmatch(request.path):
        flow_id = request.query.get("flow_id")
        if set(request.query) != {"flow_id"} or not isinstance(flow_id, str):
            raise ValueError("Runner provider callback requires one flow_id query value")
        try:
            normalized_flow_id = str(uuid.UUID(flow_id))
        except ValueError as exc:
            raise ValueError("Runner provider callback flow_id must be a UUID") from exc
        if normalized_flow_id != flow_id:
            raise ValueError("Runner provider callback flow_id must be canonical")
        return
    requires_file_path = request.path.endswith(("/files/content", "/files/diff", "/files/preview"))
    if requires_file_path:
        if set(request.query) != {"path"} or not request.query["path"]:
            raise ValueError("Runner file request requires one path query value")
        return
    if request.query:
        raise ValueError("Runner RPC route does not accept query values")


def _validate_repository_import(request: RunnerRpcRequest) -> None:
    """Restrict direct worker imports to credential-free HTTPS repositories."""
    if request.method != "POST" or request.path != "/api/repos":
        return
    if not isinstance(request.body, dict):
        raise ValueError("Runner repository import body must be an object")
    if request.body.get("local_path") is not None:
        raise ValueError("Runner repository import cannot accept a local path")
    remote_url = request.body.get("remote_url")
    if not isinstance(remote_url, str) or not remote_url:
        raise ValueError("Runner repository import requires a remote URL")
    parsed_url = urlsplit(remote_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("Runner repository import requires a credential-free HTTPS URL")


class EncryptedRunnerRpcSession:
    """Perform one capability handshake, then dispatch ordered encrypted RPCs."""

    def __init__(
        self,
        *,
        transfer_id: str,
        noise_session: RunnerNoiseSession,
        dispatcher_factory: DispatcherFactory | None = None,
    ) -> None:
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
        if dispatcher_factory is not None and not callable(dispatcher_factory):
            raise TypeError("dispatcher_factory must be callable or None")
        self._transfer_id = normalized_transfer_id
        self._noise_session = noise_session
        self._dispatcher_factory = dispatcher_factory
        self._dispatcher: WorkerHttpDispatcher | None = None
        self._established = False
        self._failed = False
        self._next_request_sequence = 0
        self._request_buffer: bytearray | None = None
        self._request_fragment_count = 0
        self._next_request_fragment = 0
        self._response_payload: bytes | None = None
        self._response_fragment_count = 0
        self._next_response_fragment = 0

    async def handle_frame(self, ciphertext: bytes, *, current_time: int) -> bytes:
        """Accept a handshake or decrypt, dispatch, and encrypt one RPC response."""
        if self._failed:
            raise RuntimeError("Runner RPC session failed and cannot be reused")
        if type(current_time) is not int or current_time < 0:
            raise ValueError("current_time must be a non-negative integer")
        if not self._established:
            return self._accept_handshake(ciphertext, current_time=current_time)

        try:
            plaintext = self._noise_session.decrypt(ciphertext)
            if plaintext.startswith(_TRANSPORT_MAGIC):
                response = await self._handle_transport_frame(plaintext)
                return self._noise_session.encrypt(response)
            if self._request_buffer is not None or self._response_payload is not None:
                raise ValueError("Runner RPC transport fragment is invalid")
            response = await self._dispatch_payload(plaintext)
            if len(response) <= _NOISE_PLAINTEXT_BYTES_MAX:
                return self._noise_session.encrypt(response)
            return self._noise_session.encrypt(self._begin_framed_response(response))
        except (TypeError, ValueError):
            self._failed = True
            self._clear_transport_buffers()
            raise

    async def _handle_transport_frame(self, frame: bytes) -> bytes:
        """Validate one bounded transport fragment and advance exact stream state."""
        if len(frame) < _TRANSPORT_HEADER.size:
            raise ValueError("Runner RPC transport fragment is invalid")
        try:
            magic, kind, index, count, total = _TRANSPORT_HEADER.unpack(
                frame[: _TRANSPORT_HEADER.size]
            )
        except ValueError as exc:
            raise ValueError("Runner RPC transport fragment is invalid") from exc
        payload = frame[_TRANSPORT_HEADER.size :]
        if magic != _TRANSPORT_MAGIC:
            raise ValueError("Runner RPC transport fragment is invalid")
        if kind == _TRANSPORT_REQUEST:
            return await self._accept_request_fragment(
                index=index,
                count=count,
                total=total,
                payload=payload,
            )
        if kind == _TRANSPORT_PULL:
            return self._accept_response_pull(
                index=index,
                count=count,
                total=total,
                payload=payload,
            )
        raise ValueError("Runner RPC transport fragment is invalid")

    async def _accept_request_fragment(
        self,
        *,
        index: int,
        count: int,
        total: int,
        payload: bytes,
    ) -> bytes:
        """Buffer one ordered request fragment and dispatch only when complete."""
        if self._response_payload is not None:
            raise ValueError("Runner RPC transport fragment is invalid")
        self._validate_fragment(
            index=index,
            count=count,
            total=total,
            payload_length=len(payload),
            total_limit=_REQUEST_BYTES_MAX,
        )
        if self._request_buffer is None:
            if index != 0:
                raise ValueError("Runner RPC transport fragment is invalid")
            self._request_buffer = bytearray(total)
            self._request_fragment_count = count
            self._next_request_fragment = 0
        if (
            count != self._request_fragment_count
            or total != len(self._request_buffer)
            or index != self._next_request_fragment
        ):
            raise ValueError("Runner RPC transport fragment is invalid")
        start = index * _TRANSPORT_PAYLOAD_BYTES_MAX
        self._request_buffer[start : start + len(payload)] = payload
        self._next_request_fragment += 1
        if self._next_request_fragment < count:
            return _TRANSPORT_HEADER.pack(
                _TRANSPORT_MAGIC,
                _TRANSPORT_ACK,
                index,
                count,
                total,
            )

        request_payload = bytes(self._request_buffer)
        self._request_buffer = None
        self._request_fragment_count = 0
        self._next_request_fragment = 0
        response = await self._dispatch_payload(request_payload)
        return self._begin_framed_response(response)

    def _begin_framed_response(self, response: bytes) -> bytes:
        """Return first framed response and retain bounded remaining data for pulls."""
        if len(response) > _RESPONSE_BYTES_MAX:
            raise ValueError("Runner RPC response exceeded transport limit")
        response_count = self._fragment_count(len(response))
        first = self._pack_response_fragment(response, index=0, count=response_count)
        if response_count > 1:
            self._response_payload = response
            self._response_fragment_count = response_count
            self._next_response_fragment = 1
        return first

    def _accept_response_pull(
        self,
        *,
        index: int,
        count: int,
        total: int,
        payload: bytes,
    ) -> bytes:
        """Return one requested response fragment in exact order."""
        response = self._response_payload
        if (
            self._request_buffer is not None
            or response is None
            or payload
            or index != self._next_response_fragment
            or count != self._response_fragment_count
            or total != len(response)
        ):
            raise ValueError("Runner RPC transport fragment is invalid")
        fragment = self._pack_response_fragment(response, index=index, count=count)
        self._next_response_fragment += 1
        if self._next_response_fragment == count:
            self._response_payload = None
            self._response_fragment_count = 0
            self._next_response_fragment = 0
        return fragment

    async def _dispatch_payload(self, plaintext: bytes) -> bytes:
        """Parse and dispatch one complete bounded RPC request payload."""
        request = _parse_request(
            plaintext,
            expected_sequence=self._next_request_sequence,
        )
        status_code, response_body = await self._dispatch(request)
        response = json.dumps(
            {
                "body": response_body,
                "request_id": request.request_id,
                "sequence": request.sequence,
                "status": status_code,
                "type": "response",
                "v": request.version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._next_request_sequence += 1
        return response

    @staticmethod
    def _fragment_count(total: int) -> int:
        """Return canonical fragment count for one payload length."""
        return fragment_count(total)

    @classmethod
    def _validate_fragment(
        cls,
        *,
        index: int,
        count: int,
        total: int,
        payload_length: int,
        total_limit: int,
    ) -> None:
        """Reject noncanonical fragment metadata before buffering payload bytes."""
        if total < 1 or total > total_limit or count != cls._fragment_count(total):
            raise ValueError("Runner RPC transport fragment is invalid")
        if index >= count:
            raise ValueError("Runner RPC transport fragment is invalid")
        expected_length = min(
            _TRANSPORT_PAYLOAD_BYTES_MAX,
            total - index * _TRANSPORT_PAYLOAD_BYTES_MAX,
        )
        if payload_length != expected_length:
            raise ValueError("Runner RPC transport fragment is invalid")

    @staticmethod
    def _pack_response_fragment(response: bytes, *, index: int, count: int) -> bytes:
        """Pack one canonical response fragment without copying remaining payload."""
        start = index * _TRANSPORT_PAYLOAD_BYTES_MAX
        end = min(len(response), start + _TRANSPORT_PAYLOAD_BYTES_MAX)
        return (
            _TRANSPORT_HEADER.pack(
                _TRANSPORT_MAGIC,
                _TRANSPORT_RESPONSE,
                index,
                count,
                len(response),
            )
            + response[start:end]
        )

    def _clear_transport_buffers(self) -> None:
        """Release bounded request and response buffers after terminal failure."""
        self._request_buffer = None
        self._request_fragment_count = 0
        self._next_request_fragment = 0
        self._response_payload = None
        self._response_fragment_count = 0
        self._next_response_fragment = 0

    def _accept_handshake(self, message: bytes, *, current_time: int) -> bytes:
        """Bind relay routing and any worker dispatcher to signed capability identity."""
        try:
            response = self._noise_session.accept_handshake(
                message,
                current_time=current_time,
            )
            capability = self._noise_session.capability
            if capability.transfer_id != self._transfer_id:
                raise ValueError("Runner capability transfer_id does not match relay routing")
            if self._dispatcher_factory is not None:
                dispatcher = self._dispatcher_factory(capability.user_id)
                if not isinstance(dispatcher, WorkerHttpDispatcher):
                    raise TypeError("dispatcher_factory must return WorkerHttpDispatcher")
                if dispatcher.user_id != capability.user_id:
                    raise ValueError("Runner worker tenant does not match capability identity")
                self._dispatcher = dispatcher
            self._established = True
            return response
        except (TypeError, ValueError):
            self._failed = True
            raise

    async def _dispatch(self, request: RunnerRpcRequest) -> tuple[int, Any]:
        """Authorize and invoke an exact restricted worker operation."""
        required_scope = _required_scope(request)
        if required_scope not in self._noise_session.capability.scopes:
            raise ValueError(f"Runner capability does not allow {required_scope}")
        if request.path == "/health":
            if request.body is not None or request.query:
                raise ValueError("Runner health request body and query must be empty")
            return 200, {
                "protocol": RUNNER_PROTOCOL_VERSION,
                "status": "ok",
            }
        if self._dispatcher is None:
            raise ValueError("Runner worker dispatcher is unavailable")
        _validate_route_query(request)
        _validate_repository_import(request)
        worker_response = await self._dispatcher.request(
            method=request.method,
            path=request.path,
            body=request.body,
            query=request.query,
        )
        return worker_response.status_code, worker_response.body
