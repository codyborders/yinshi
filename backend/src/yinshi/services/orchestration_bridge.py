"""Query-bound capability and strict wire policy for the orchestration bridge.

Phase 4 of the thread orchestration plan introduces a duplex protocol between
the Python backend and the Node sidecar. Pi custom tools inside the sidecar
send ``orchestration_request`` frames over the existing per-query Unix socket;
the backend validates them against a query-bound capability and answers with a
bounded ``orchestration_response``.

Security invariants enforced here:

- The capability token is random per query and lives only in backend memory
  and the in-memory query options handed to the sidecar. It never enters
  prompts, files, environment variables, logs, or persisted event journals.
- The capability is bound to the exact tenant, runtime, session, prompt run,
  connection, and expiration window.
- Every frame is validated against an explicit strict shape with a fixed
  operation allowlist. Unknown operations, unknown fields, oversized frames,
  and stale capabilities fail closed.
- Handler failures are mapped to bounded, safe error codes. Raw exception
  details never reach the sidecar, logs, or telemetry.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

ORCHESTRATION_REQUEST_TYPE = "orchestration_request"
ORCHESTRATION_CANCEL_TYPE = "orchestration_cancel"
ORCHESTRATION_RESPONSE_TYPE = "orchestration_response"

ORCHESTRATION_OPERATION_PING = "ping_thread_bridge"

THREAD_OPERATIONS: frozenset[str] = frozenset(
    {
        "spawn_thread",
        "list_children",
        "get_thread",
        "wait_for_threads",
        "cancel_thread",
        "report_thread_result",
    }
)
ORCHESTRATION_OPERATIONS: frozenset[str] = THREAD_OPERATIONS | {ORCHESTRATION_OPERATION_PING}

ORCHESTRATION_REQUEST_MAX_BYTES = 64 * 1024
ORCHESTRATION_RESPONSE_MAX_BYTES = 256 * 1024

_ORCHESTRATION_CAPABILITY_TTL_SECONDS = 30 * 60.0

_ID_MAX_CHARS = 128
_REQUEST_ID_MAX_CHARS = 64
_CAPABILITY_MAX_CHARS = 256
_OPERATION_MAX_CHARS = 64
_PING_ECHO_MAX_CHARS = 256

REQUEST_FRAME_FIELDS: frozenset[str] = frozenset(
    {"type", "id", "request_id", "capability", "operation", "arguments"}
)

THREAD_ERROR_MESSAGES: dict[str, str] = {
    "depth_exceeded": "The thread depth limit has been reached.",
    "child_limit_exceeded": "The direct-child limit has been reached.",
    "active_thread_limit_exceeded": "The active-thread limit has been reached.",
    "tree_limit_exceeded": "The thread tree limit has been reached.",
    "spawn_turn_limit_exceeded": "The current turn has reached its child spawn limit.",
    "thread_not_found": "Thread not found.",
    "runtime_unavailable": "The thread runtime is unavailable.",
    "workspace_provisioning_failed": "The child workspace could not be provisioned.",
}

_ALLOWED_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "request_too_large",
        "unknown_operation",
        "invalid_arguments",
        "capability_invalid",
        "capability_expired",
        "session_mismatch",
        "duplicate_request",
        "too_many_requests",
        "handler_timeout",
        "handler_failed",
        "response_too_large",
    }
) | frozenset(THREAD_ERROR_MESSAGES)


class OrchestrationHandler(Protocol):
    """Connection-free handler for validated operation arguments."""

    async def __call__(self, arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]: ...


class OrchestrationProtocolError(Exception):
    """A wire-protocol or capability violation with a bounded error code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in _ALLOWED_ERROR_CODES:
            raise ValueError(f"Unknown orchestration error code: {code}")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class OrchestrationRequest:
    """One strictly validated inbound orchestration request frame."""

    session_id: str
    request_id: str
    capability: str = field(repr=False)
    operation: str
    arguments: dict[str, Any]
    protocol_version: int = 1
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedThreadCaller:
    """Backend authority for one SDK tool call, without the capability secret."""

    session_id: str
    run_id: str
    tenant_id: str | None
    runtime_id: str | None
    tool_call_id: str
    expires_at: float
    database_path: str | None = field(default=None, repr=False)


class ThreadOrchestrationHandler(Protocol):
    """A thread operation receives only a caller verified by the transport."""

    async def __call__(
        self,
        arguments: dict[str, Any],
        *,
        caller: VerifiedThreadCaller,
    ) -> dict[str, Any]: ...


@dataclass
class OrchestrationCapability:
    """A random per-query secret bound to one exact execution context.

    The token lives in backend memory and the sidecar's in-memory query
    options for the duration of one query. It is never persisted, logged, or
    placed in prompts, files, or environment variables.
    """

    token: str = field(repr=False)
    session_id: str
    run_id: str | None
    tenant_id: str | None
    runtime_id: str | None
    connection_id: str
    expires_at: float
    allowed_operations: frozenset[str] = frozenset({ORCHESTRATION_OPERATION_PING})
    database_path: str | None = field(default=None, repr=False)
    _claimed: bool = field(default=False, init=False, repr=False)
    _revoked: bool = field(default=False, init=False, repr=False)

    def claim(self, *, session_id: str, connection_id: str) -> None:
        """Bind this single-use lease before any query bytes are sent."""
        if self._claimed or self._revoked or self.expires_at <= time.monotonic():
            raise ValueError("The orchestration capability is no longer available")
        if self.session_id != session_id or not connection_id:
            raise ValueError("The orchestration capability context does not match")
        if self.connection_id and self.connection_id != connection_id:
            raise ValueError("The orchestration capability connection does not match")
        self.connection_id = connection_id
        self._claimed = True

    def revoke(self) -> None:
        """Permanently retire the lease, including after partial query setup."""
        self._revoked = True


def verify_orchestration_request(
    capability: OrchestrationCapability,
    request: OrchestrationRequest,
    *,
    connection_id: str,
) -> VerifiedThreadCaller | None:
    """Verify an inbound request against the query-bound capability.

    Fails closed on token mismatch, foreign connections, expired windows,
    and session mismatches. Binds the request to the exact tenant, runtime,
    session, prompt run, connection, and expiration of the active query.
    """
    _verify_query_identity(capability, request.session_id, request.capability, connection_id)
    if request.operation not in capability.allowed_operations:
        raise OrchestrationProtocolError("unknown_operation", "The operation is not allowed.")
    if request.protocol_version == 1:
        return None
    if not capability.run_id or not request.tool_call_id or not capability.database_path:
        raise OrchestrationProtocolError(
            "capability_invalid", "A durable run and database binding are required."
        )
    return VerifiedThreadCaller(
        session_id=capability.session_id,
        run_id=capability.run_id,
        tenant_id=capability.tenant_id,
        runtime_id=capability.runtime_id,
        tool_call_id=request.tool_call_id,
        expires_at=capability.expires_at,
        database_path=capability.database_path,
    )


def _verify_query_identity(
    capability: OrchestrationCapability,
    session_id: str,
    token: str,
    connection_id: str,
) -> None:
    if capability._revoked or not connection_id or capability.connection_id != connection_id:
        raise OrchestrationProtocolError(
            "capability_invalid", "The capability connection does not match."
        )
    if not token.isascii() or not secrets.compare_digest(capability.token, token):
        raise OrchestrationProtocolError(
            "capability_invalid", "The capability token does not match."
        )
    if capability.expires_at <= time.monotonic():
        raise OrchestrationProtocolError("capability_expired", "The capability has expired.")
    if capability.session_id != session_id:
        raise OrchestrationProtocolError("session_mismatch", "The active session does not match.")


def verify_orchestration_cancel(
    capability: OrchestrationCapability,
    message: dict[str, Any],
    *,
    connection_id: str,
    frame_bytes: int,
) -> str:
    """Authenticate one strict cancellation before resolving its pending request."""
    fields = {"type", "protocol_version", "id", "request_id", "capability"}
    if frame_bytes > ORCHESTRATION_REQUEST_MAX_BYTES or set(message) != fields:
        raise OrchestrationProtocolError("invalid_request", "Invalid cancellation frame.")
    if (
        message["type"] != ORCHESTRATION_CANCEL_TYPE
        or type(message["protocol_version"]) is not int
        or message["protocol_version"] != 2
    ):
        raise OrchestrationProtocolError("invalid_request", "Invalid cancellation version.")
    for identity_field, maximum in (("id", 128), ("request_id", 256), ("capability", 256)):
        if not _require_bounded_string(message[identity_field], max_chars=maximum):
            raise OrchestrationProtocolError("invalid_request", "Invalid cancellation identity.")
    _verify_query_identity(capability, message["id"], message["capability"], connection_id)
    return str(message["request_id"])


def generate_orchestration_capability(
    session_id: str,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
    runtime_id: str | None = None,
    connection_id: str = "",
    ttl_seconds: float = _ORCHESTRATION_CAPABILITY_TTL_SECONDS,
    allowed_operations: frozenset[str] = frozenset({ORCHESTRATION_OPERATION_PING}),
    database_path: str | None = None,
) -> OrchestrationCapability:
    """Create a fresh random capability bound to one query's context.

    An empty connection identity permits one later claim by the query owner.
    A supplied connection identity must match that owner.
    """
    if not session_id:
        raise ValueError("session_id is required for an orchestration capability")
    if not allowed_operations or not allowed_operations <= ORCHESTRATION_OPERATIONS:
        raise ValueError("orchestration operations are invalid")
    if allowed_operations & THREAD_OPERATIONS and not run_id:
        raise ValueError("thread operations require a durable run")
    return OrchestrationCapability(
        token=secrets.token_urlsafe(32),
        session_id=session_id,
        run_id=run_id,
        tenant_id=tenant_id,
        runtime_id=runtime_id,
        connection_id=connection_id,
        expires_at=time.monotonic() + ttl_seconds,
        allowed_operations=allowed_operations,
        database_path=database_path,
    )


def _require_bounded_string(value: Any, *, max_chars: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_chars


async def handle_ping_thread_bridge(
    arguments: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    """The single Phase 4 operation: a harmless bounded round trip.

    Accepts an optional ``message`` string of at most 256 characters and
    echoes it back with a fixed status. No database, filesystem, network,
    or credential access. Unknown fields and wrong types fail closed.
    """
    if set(arguments) - {"message"}:
        raise OrchestrationProtocolError(
            "invalid_arguments",
            "The ping operation accepts only the bounded message field.",
        )
    message = arguments.get("message", "")
    if (
        not isinstance(message, str)
        or len(message) > _PING_ECHO_MAX_CHARS
        or any(
            ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
            for char in message
        )
    ):
        raise OrchestrationProtocolError(
            "invalid_arguments",
            "The ping message must be a string of at most 256 characters.",
        )
    return {
        "status": "ok",
        "echo": message,
        "session_bound": True,
        "session_id": session_id,
    }


def build_orchestration_success(
    *,
    session_id: str,
    request_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build one success response frame with the exact wire shape."""
    return {
        "type": ORCHESTRATION_RESPONSE_TYPE,
        "id": session_id,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def build_orchestration_error(
    *,
    session_id: str,
    request_id: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """Build one failure response frame with a bounded safe error."""
    if code not in _ALLOWED_ERROR_CODES:
        raise ValueError(f"Unknown orchestration error code: {code}")
    if not _require_bounded_string(session_id, max_chars=_ID_MAX_CHARS):
        session_id = "unknown"
    if not _require_bounded_string(request_id, max_chars=_REQUEST_ID_MAX_CHARS):
        request_id = ""
    return {
        "type": ORCHESTRATION_RESPONSE_TYPE,
        "id": session_id,
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def parse_orchestration_request(
    message: Any,
    *,
    frame_bytes: int,
) -> OrchestrationRequest:
    """Validate one inbound frame against the strict wire shape.

    Raises :class:`OrchestrationProtocolError` with a bounded error code for
    every malformed input. Unknown operations fail closed here.
    """
    if frame_bytes > ORCHESTRATION_REQUEST_MAX_BYTES:
        raise OrchestrationProtocolError(
            "request_too_large",
            "The orchestration request frame exceeded the 64 KiB limit.",
        )
    if not isinstance(message, dict):
        raise OrchestrationProtocolError(
            "invalid_request", "The orchestration request must be a JSON object."
        )
    protocol_version = message.get("protocol_version", 1)
    if type(protocol_version) is not int or protocol_version not in {1, 2}:
        raise OrchestrationProtocolError("invalid_request", "Unknown protocol version.")
    expected_fields = REQUEST_FRAME_FIELDS
    if protocol_version == 2:
        expected_fields = expected_fields | {"protocol_version", "tool_call_id"}
        if not _require_bounded_string(message.get("tool_call_id"), max_chars=256):
            raise OrchestrationProtocolError("invalid_request", "The tool call ID is invalid.")
    if set(message) != set(expected_fields):
        raise OrchestrationProtocolError(
            "invalid_request",
            "The orchestration request fields do not match the strict schema.",
        )
    if message.get("type") != ORCHESTRATION_REQUEST_TYPE:
        raise OrchestrationProtocolError(
            "invalid_request", "Unknown orchestration request frame type."
        )
    if not _require_bounded_string(message.get("id"), max_chars=_ID_MAX_CHARS):
        raise OrchestrationProtocolError(
            "invalid_request", "The orchestration request id must be a bounded string."
        )
    if not _require_bounded_string(message.get("request_id"), max_chars=_REQUEST_ID_MAX_CHARS):
        raise OrchestrationProtocolError(
            "invalid_request",
            "The orchestration request_id must be a bounded string.",
        )
    token = message.get("capability")
    if not (
        isinstance(token, str)
        and _require_bounded_string(token, max_chars=_CAPABILITY_MAX_CHARS)
        and token.isascii()
    ):
        raise OrchestrationProtocolError(
            "invalid_request",
            "The orchestration capability must be a bounded string.",
        )
    operation = message.get("operation")
    if not _require_bounded_string(operation, max_chars=_OPERATION_MAX_CHARS):
        raise OrchestrationProtocolError(
            "invalid_request",
            "The orchestration operation must be a bounded string.",
        )
    allowed = ORCHESTRATION_OPERATIONS if protocol_version == 2 else {ORCHESTRATION_OPERATION_PING}
    if operation not in allowed:
        raise OrchestrationProtocolError(
            "unknown_operation", "The orchestration operation is not allowed."
        )
    arguments = message.get("arguments")
    if not isinstance(arguments, dict):
        raise OrchestrationProtocolError(
            "invalid_request", "The orchestration arguments must be a JSON object."
        )
    return OrchestrationRequest(
        session_id=str(message["id"]),
        request_id=str(message["request_id"]),
        capability=str(message["capability"]),
        operation=cast(str, operation),
        arguments=arguments,
        protocol_version=protocol_version,
        tool_call_id=message.get("tool_call_id"),
    )
