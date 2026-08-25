"""Verify restricted worker RPC over encrypted Noise frames.

Real IK handshakes wrap canonical requests. Tests decrypt responses on the
client, exercise the existing worker repository API, and reject bad ordering.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.connection import Keypair, NoiseConnection

from yinshi.services.runner_capabilities import (
    create_runner_capability,
    runner_capability_signing_public_key,
)
from yinshi.services.runner_noise_session import (
    RUNNER_NOISE_PROLOGUE,
    RunnerCapabilityReplayStore,
    RunnerNoiseSession,
)
from yinshi.services.runner_rpc import (
    EncryptedRunnerRpcSession,
    RunnerRpcRequest,
    _parse_request,
    _required_scope,
)
from yinshi.services.runner_rpc_transport import (
    TRANSPORT_ACK,
    TRANSPORT_HEADER,
    TRANSPORT_MAGIC,
    TRANSPORT_PAYLOAD_BYTES_MAX,
    TRANSPORT_PULL,
    TRANSPORT_REQUEST,
    TRANSPORT_RESPONSE,
)
from yinshi.tenant import TenantContext
from yinshi.worker_auth import WorkerPrincipal
from yinshi.worker_runtime import WorkerHttpDispatcher, WorkerHttpResponse

_RUNNER_PRIVATE_KEY = bytes.fromhex(
    "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
)
_CLIENT_PRIVATE_KEY = bytes.fromhex(
    "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1"
)
_USER_ID = "user-1"
_WORKSPACE_ID = "11111111111141118111111111111111"


def test_active_run_discovery_requires_session_stream_scope() -> None:
    """Only the exact active-run path should receive stream authority."""
    session_id = "a" * 32
    request = RunnerRpcRequest(
        version=1,
        sequence=0,
        request_id=str(uuid.uuid4()),
        method="GET",
        path=f"/api/sessions/{session_id}/runs/active",
        body=None,
        query={},
    )

    assert _required_scope(request) == "session.stream"

    near_match = RunnerRpcRequest(
        version=1,
        sequence=0,
        request_id=str(uuid.uuid4()),
        method="GET",
        path=f"/api/sessions/{session_id}/runs/actives",
        body=None,
        query={},
    )
    with pytest.raises(ValueError, match="not allowed"):
        _required_scope(near_match)


def test_bounded_history_routes_require_session_read_scope() -> None:
    """Only exact bounded history paths should receive session read authority."""
    session_id = "a" * 32
    message_id = "b" * 32
    for path in (
        f"/api/sessions/{session_id}/messages/page",
        f"/api/sessions/{session_id}/messages/bundle",
        f"/api/sessions/{session_id}/messages/{message_id}/field",
    ):
        request = RunnerRpcRequest(
            version=2,
            sequence=0,
            request_id="11111111-1111-4111-8111-111111111111",
            method="GET",
            path=path,
            body=None,
            query={},
        )
        assert _required_scope(request) == "session.read"

    rejected_routes = (
        ("GET", f"/api/sessions/{session_id}/messages/pages"),
        ("GET", f"/api/sessions/{session_id}/messages/bundles"),
        ("GET", f"/api/sessions/{session_id}/messages/{message_id}/fields"),
        ("POST", f"/api/sessions/{session_id}/messages/bundle"),
    )
    for method, path in rejected_routes:
        request = RunnerRpcRequest(
            version=2,
            sequence=0,
            request_id="11111111-1111-4111-8111-111111111111",
            method=method,
            path=path,
            body=None,
            query={},
        )
        with pytest.raises(ValueError, match="not allowed"):
            _required_scope(request)


def _public_key(private_key: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


async def _open_session(
    tmp_path: Path,
    *,
    scopes: list[str] | None = None,
    dispatcher_factory: Callable[[str], WorkerHttpDispatcher] | None = None,
    max_session_bytes: int = 65_536,
) -> tuple[EncryptedRunnerRpcSession, NoiseConnection]:
    runner_public_key = _public_key(_RUNNER_PRIVATE_KEY)
    client_public_key = _public_key(_CLIENT_PRIVATE_KEY)
    capability, claims = create_runner_capability(
        user_id="user-1",
        runner_id="runner-1",
        runner_public_key=_base64url(runner_public_key),
        initiator_public_key=_base64url(client_public_key),
        scopes=scopes or ["worker.health"],
        max_session_bytes=max_session_bytes,
        current_time=1_900_000_000,
    )
    signing_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")
    noise_session = RunnerNoiseSession(
        runner_id="runner-1",
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=signing_key,
        replay_store=RunnerCapabilityReplayStore(tmp_path / "replay.sqlite3"),
    )
    rpc_session = EncryptedRunnerRpcSession(
        transfer_id=claims.transfer_id,
        noise_session=noise_session,
        dispatcher_factory=dispatcher_factory,
    )

    initiator = NoiseConnection.from_name(b"Noise_IK_25519_ChaChaPoly_SHA256")
    initiator.set_as_initiator()
    initiator.set_keypair_from_private_bytes(Keypair.STATIC, _CLIENT_PRIVATE_KEY)
    initiator.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, runner_public_key)
    initiator.set_prologue(RUNNER_NOISE_PROLOGUE)
    initiator.start_handshake()
    first_message = bytes(initiator.write_message(capability.encode("ascii")))
    response = (await rpc_session.handle_frame(first_message, current_time=1_900_000_001))[0]
    assert json.loads(initiator.read_message(response))["transfer_id"] == claims.transfer_id
    return rpc_session, initiator


_TRANSPORT_HEADER = TRANSPORT_HEADER
_TRANSPORT_MAGIC = TRANSPORT_MAGIC
_TRANSPORT_REQUEST = TRANSPORT_REQUEST
_TRANSPORT_ACK = TRANSPORT_ACK
_TRANSPORT_RESPONSE = TRANSPORT_RESPONSE
_TRANSPORT_PULL = TRANSPORT_PULL
_TRANSPORT_PAYLOAD_BYTES_MAX = TRANSPORT_PAYLOAD_BYTES_MAX


def _transport_frames(payload: bytes, *, kind: int) -> list[bytes]:
    count = max(
        1, (len(payload) + _TRANSPORT_PAYLOAD_BYTES_MAX - 1) // _TRANSPORT_PAYLOAD_BYTES_MAX
    )
    frames: list[bytes] = []
    for index in range(count):
        start = index * _TRANSPORT_PAYLOAD_BYTES_MAX
        chunk = payload[start : start + _TRANSPORT_PAYLOAD_BYTES_MAX]
        frames.append(
            _TRANSPORT_HEADER.pack(_TRANSPORT_MAGIC, kind, index, count, len(payload)) + chunk
        )
    return frames


def _transport_request_payload(
    *,
    method: str,
    path: str,
    sequence: int,
    body: object = None,
    query: dict[str, str] | None = None,
    response_mode: str | None = None,
) -> tuple[bytes, str]:
    request_id = str(uuid.uuid4())
    request: dict[str, object] = {
        "body": body,
        "method": method,
        "path": path,
        "query": query or {},
        "request_id": request_id,
        "sequence": sequence,
        "type": "request",
        "v": 2,
    }
    if response_mode is not None:
        request["response_mode"] = response_mode
    payload = json.dumps(
        request,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return payload, request_id


async def _transport_round_trip(
    session: EncryptedRunnerRpcSession,
    initiator: NoiseConnection,
    request_payload: bytes,
) -> bytes:
    response_frame: bytes | None = None
    request_frames = _transport_frames(request_payload, kind=_TRANSPORT_REQUEST)
    for index, frame in enumerate(request_frames):
        encrypted_response = (
            await session.handle_frame(
                bytes(initiator.encrypt(frame)),
                current_time=1_900_000_002,
            )
        )[0]
        plaintext_response = bytes(initiator.decrypt(encrypted_response))
        if index + 1 < len(request_frames):
            header = _TRANSPORT_HEADER.unpack(plaintext_response[: _TRANSPORT_HEADER.size])
            assert header == (
                _TRANSPORT_MAGIC,
                _TRANSPORT_ACK,
                index,
                len(request_frames),
                len(request_payload),
            )
            assert len(plaintext_response) == _TRANSPORT_HEADER.size
        else:
            response_frame = plaintext_response

    assert response_frame is not None
    magic, kind, index, count, total = _TRANSPORT_HEADER.unpack(
        response_frame[: _TRANSPORT_HEADER.size]
    )
    assert (magic, kind, index) == (_TRANSPORT_MAGIC, _TRANSPORT_RESPONSE, 0)
    response = bytearray(total)
    first_payload = response_frame[_TRANSPORT_HEADER.size :]
    response[0 : len(first_payload)] = first_payload
    for response_index in range(1, count):
        pull = _TRANSPORT_HEADER.pack(
            _TRANSPORT_MAGIC,
            _TRANSPORT_PULL,
            response_index,
            count,
            total,
        )
        encrypted_fragment = (
            await session.handle_frame(
                bytes(initiator.encrypt(pull)),
                current_time=1_900_000_003,
            )
        )[0]
        fragment = bytes(initiator.decrypt(encrypted_fragment))
        fragment_header = _TRANSPORT_HEADER.unpack(fragment[: _TRANSPORT_HEADER.size])
        assert fragment_header == (
            _TRANSPORT_MAGIC,
            _TRANSPORT_RESPONSE,
            response_index,
            count,
            total,
        )
        start = response_index * _TRANSPORT_PAYLOAD_BYTES_MAX
        fragment_payload = fragment[_TRANSPORT_HEADER.size :]
        response[start : start + len(fragment_payload)] = fragment_payload
    return bytes(response)


class _HistoryQueryDispatcher(WorkerHttpDispatcher):
    """Record bounded history requests after encrypted validation."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object, dict[str, str] | None]] = []

    @property
    def user_id(self) -> str:
        return _USER_ID

    async def request(
        self,
        *,
        method: str,
        path: str,
        body: object,
        query: dict[str, str] | None = None,
    ) -> WorkerHttpResponse:
        self.requests.append((method, path, body, query))
        return WorkerHttpResponse(
            status_code=200,
            body={"ok": True},
            content_type="application/json",
        )


class _BundleResponseDispatcher(WorkerHttpDispatcher):
    """Return one configured response from the exact history bundle route."""

    def __init__(self, response_body: object) -> None:
        self.response_body = response_body

    @property
    def user_id(self) -> str:
        return _USER_ID

    async def request(
        self,
        *,
        method: str,
        path: str,
        body: object,
        query: dict[str, str] | None = None,
    ) -> WorkerHttpResponse:
        assert method == "GET"
        assert path == f"/api/sessions/{'a' * 32}/messages/bundle"
        assert body is None
        assert query == {}
        return WorkerHttpResponse(
            status_code=200,
            body=self.response_body,
            content_type="application/json",
        )


class _LargeResponseDispatcher(WorkerHttpDispatcher):
    """Test dispatcher that exercises RPC transport above worker route limits."""

    def __init__(self, *, expected_body: object, response_body: object) -> None:
        self.expected_body = expected_body
        self.response_body = response_body

    @property
    def user_id(self) -> str:
        return _USER_ID

    async def request(
        self,
        *,
        method: str,
        path: str,
        body: object,
        query: dict[str, str] | None = None,
    ) -> WorkerHttpResponse:
        assert method == "PUT"
        assert path == f"/api/workspaces/{_WORKSPACE_ID}/files/content"
        assert body == self.expected_body
        assert query == {"path": "large.txt"}
        return WorkerHttpResponse(
            status_code=200,
            body=self.response_body,
            content_type="application/json",
        )


def _encrypted_request(
    initiator: NoiseConnection,
    *,
    method: str,
    path: str,
    sequence: int,
    body: object = None,
    version: int = 1,
    query: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    request_id = str(uuid.uuid4())
    payload: dict[str, object] = {
        "body": body,
        "method": method,
        "path": path,
        "request_id": request_id,
        "sequence": sequence,
        "type": "request",
        "v": version,
    }
    if version == 2:
        payload["query"] = query or {}
    request = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return bytes(initiator.encrypt(request)), request_id


def _history_cursor(
    *,
    created_at: str = "2026-08-23T00:00:00+00:00",
    message_id: str = "b" * 32,
) -> str:
    """Build one canonical bounded-history cursor for RPC validation tests."""
    created_at_bytes = created_at.encode("utf-8")
    raw = bytes((1, len(created_at_bytes))) + created_at_bytes + bytes.fromhex(message_id)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_bounded_history_queries_reach_worker_unchanged(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Valid first-page, cursor, and field queries survive encrypted dispatch."""
    session_id = "a" * 32
    message_id = "b" * 32
    cursor = _history_cursor(message_id=message_id)
    through = _history_cursor(
        created_at="2026-08-23T00:00:01+00:00",
        message_id="c" * 32,
    )
    dispatcher = _HistoryQueryDispatcher()
    session, initiator = await _open_session(
        tmp_path,
        scopes=["session.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    requests = (
        (f"/api/sessions/{session_id}/messages/page", {}),
        (f"/api/sessions/{session_id}/messages/page", {"cursor": cursor}),
        (f"/api/sessions/{session_id}/messages/bundle", {}),
        (
            f"/api/sessions/{session_id}/messages/bundle",
            {
                "cursor": cursor,
                "through": through,
                "snapshot": "123",
                "snapshot_count": "66",
                "snapshot_tail": through,
                "active_run_id": "none",
            },
        ),
        (
            f"/api/sessions/{session_id}/messages/bundle",
            {
                "cursor": _history_cursor(
                    created_at="2026-08-23T00:00:00+00:00",
                    message_id="a" * 32,
                ),
                "through": _history_cursor(
                    created_at="2026-08-23T00:00:00Z",
                    message_id="a" * 32,
                ),
                "snapshot": "9007199254740991",
                "snapshot_count": "9007199254740991",
                "snapshot_tail": _history_cursor(
                    created_at="2026-08-23T00:00:00Z",
                    message_id="a" * 32,
                ),
                "active_run_id": "d" * 32,
            },
        ),
        (
            f"/api/sessions/{session_id}/messages/{message_id}/field",
            {"name": "content", "offset": "0"},
        ),
        (
            f"/api/sessions/{session_id}/messages/{message_id}/field",
            {"name": "full_message", "offset": "1000000000"},
        ),
    )

    for sequence, (path, query) in enumerate(requests):
        encrypted_request, request_id = _encrypted_request(
            initiator,
            method="GET",
            path=path,
            sequence=sequence,
            version=2,
            query=query,
        )
        response = json.loads(
            initiator.decrypt(
                (await session.handle_frame(encrypted_request, current_time=1_900_000_002))[0]
            )
        )
        assert response["request_id"] == request_id
        assert response["status"] == 200

    assert dispatcher.requests == [("GET", path, None, query) for path, query in requests]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "query"),
    [
        ("page", {"cursor": ""}),
        ("page", {"Cursor": _history_cursor()}),
        ("page", {"cursor": _history_cursor(), "extra": "value"}),
        ("page", {"cursor": _history_cursor() + "="}),
        ("page", {"cursor": "abc_123"}),
        ("page", {"cursor": "A" * 129}),
        ("bundle", {"cursor": _history_cursor()}),
        ("bundle", {"through": _history_cursor()}),
        ("bundle", {"snapshot": "1"}),
        ("bundle", {"snapshot_count": "1"}),
        (
            "bundle",
            {"cursor": _history_cursor(), "through": _history_cursor()},
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(),
                "snapshot": "1",
                "snapshot_count": "1",
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "1",
                "snapshot_tail": "invalid",
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "0",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
                "active_run_id": "none",
                "extra": "value",
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
                "active_run_id": "NONE",
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
                "active_run_id": "a" * 31,
            },
        ),
        (
            "bundle",
            {
                "cursor": "invalid",
                "through": "invalid",
                "snapshot": "1",
                "snapshot_count": "1",
                "snapshot_tail": "invalid",
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "01",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "9007199254740992",
                "snapshot_count": "1",
                "snapshot_tail": _history_cursor(),
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "01",
                "snapshot_tail": _history_cursor(),
            },
        ),
        (
            "bundle",
            {
                "cursor": _history_cursor(),
                "through": _history_cursor(created_at="2026-08-23T00:00:01+00:00"),
                "snapshot": "1",
                "snapshot_count": "9007199254740992",
                "snapshot_tail": _history_cursor(),
            },
        ),
        ("field", {}),
        ("field", {"name": "content"}),
        ("field", {"offset": "0"}),
        ("field", {"name": "content", "offset": "0", "extra": "value"}),
        ("field", {"name": "Content", "offset": "0"}),
        ("field", {"name": "content", "Name": "full_message", "offset": "0"}),
        ("field", {"name": "content", "offset": "+1"}),
        ("field", {"name": "content", "offset": "-1"}),
        ("field", {"name": "content", "offset": "01"}),
        ("field", {"name": "content", "offset": "1000000001"}),
    ],
)
async def test_bounded_history_queries_reject_invalid_variants(
    tmp_path: Path,
    db: sqlite3.Connection,
    route: str,
    query: dict[str, str],
) -> None:
    """Malformed history query data must fail before worker dispatch."""
    session_id = "a" * 32
    message_id = "b" * 32
    if route == "page":
        path = f"/api/sessions/{session_id}/messages/page"
    elif route == "bundle":
        path = f"/api/sessions/{session_id}/messages/bundle"
    else:
        path = f"/api/sessions/{session_id}/messages/{message_id}/field"
    dispatcher = _HistoryQueryDispatcher()
    session, initiator = await _open_session(
        tmp_path,
        scopes=["session.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    encrypted_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=path,
        sequence=0,
        version=2,
        query=query,
    )

    with pytest.raises(ValueError, match="history"):
        (await session.handle_frame(encrypted_request, current_time=1_900_000_002))[0]

    assert dispatcher.requests == []


@pytest.mark.asyncio
async def test_bounded_history_query_rejects_duplicate_json_keys(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Duplicate query keys must not normalize into one dispatched value."""
    session_id = "a" * 32
    message_id = "b" * 32
    request_id = str(uuid.uuid4())
    dispatcher = _HistoryQueryDispatcher()
    session, initiator = await _open_session(
        tmp_path,
        scopes=["session.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    plaintext = (
        '{"body":null,"method":"GET","path":"/api/sessions/'
        f'{session_id}/messages/{message_id}/field","query":{{"name":"content",'
        '"name":"full_message","offset":"0"},'
        f'"request_id":"{request_id}","sequence":0,"type":"request","v":2}}'
    ).encode("utf-8")

    with pytest.raises(ValueError, match="unique"):
        (
            await session.handle_frame(
                bytes(initiator.encrypt(plaintext)),
                current_time=1_900_000_002,
            )
        )[0]

    assert dispatcher.requests == []


@pytest.mark.asyncio
async def test_legacy_client_receives_legacy_response_when_both_fit(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """A legacy client keeps unframed request and response plaintext when both fit."""
    session, initiator = await _open_session(tmp_path)
    request, request_id = _encrypted_request(
        initiator,
        method="GET",
        path="/health",
        sequence=0,
    )

    encrypted_response = (await session.handle_frame(request, current_time=1_900_000_002))[0]
    response = json.loads(initiator.decrypt(encrypted_response))

    assert response == {
        "body": {"protocol": "yinshi-runner-v1", "status": "ok"},
        "request_id": request_id,
        "sequence": 0,
        "status": 200,
        "type": "response",
        "v": 1,
    }


@pytest.mark.asyncio
async def test_encrypted_repository_list_uses_restricted_worker_app(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Repository RPC reuses the worker route rather than a second data implementation."""
    from yinshi.main import create_app

    worker_directory = tmp_path / "worker-user"
    principal = WorkerPrincipal(
        tenant=TenantContext(
            user_id="user-1",
            email="worker-user@runner.invalid",
            data_dir=str(worker_directory),
            db_path=str(worker_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )
    dispatcher = WorkerHttpDispatcher(
        app=create_app(mode="worker", worker_principal=principal),
        principal=principal,
    )
    session, initiator = await _open_session(
        tmp_path,
        scopes=["provider.configure", "repository.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    request, request_id = _encrypted_request(
        initiator,
        method="GET",
        path="/api/repos",
        sequence=0,
    )

    encrypted_response = (await session.handle_frame(request, current_time=1_900_000_002))[0]
    response = json.loads(initiator.decrypt(encrypted_response))

    assert response == {
        "body": [],
        "request_id": request_id,
        "sequence": 0,
        "status": 200,
        "type": "response",
        "v": 1,
    }

    provider_request, provider_request_id = _encrypted_request(
        initiator,
        method="GET",
        path="/auth/providers/openai-codex/callback",
        sequence=1,
        version=2,
        query={"flow_id": "11111111-1111-4111-8111-111111111111"},
    )
    provider_response = (
        await session.handle_frame(
            provider_request,
            current_time=1_900_000_002,
        )
    )[0]
    assert provider_response is not None
    provider_payload = json.loads(initiator.decrypt(provider_response))
    assert provider_payload["request_id"] == provider_request_id
    assert provider_payload["status"] in {502, 503}

    unexpected_query_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path="/api/repos",
        sequence=2,
        version=2,
        query={"path": "ignored.txt"},
    )
    with pytest.raises(ValueError, match="does not accept query"):
        (await session.handle_frame(unexpected_query_request, current_time=1_900_000_003))[0]


@pytest.mark.asyncio
async def test_encrypted_repository_import_rejects_paths_and_reuses_worker_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Repository writes allow credential-free HTTPS clones and reject raw paths."""
    from yinshi.main import create_app

    worker_directory = tmp_path / "worker-import"
    principal = WorkerPrincipal(
        tenant=TenantContext(
            user_id="user-1",
            email="worker-user@runner.invalid",
            data_dir=str(worker_directory),
            db_path=str(worker_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )
    dispatcher = WorkerHttpDispatcher(
        app=create_app(mode="worker", worker_principal=principal),
        principal=principal,
    )

    async def clone_repository(
        remote_url: str,
        destination: str,
        *,
        access_token: str | None,
    ) -> str:
        assert remote_url == "https://example.com/team/project.git"
        assert access_token is None
        Path(destination).mkdir(parents=True)
        return destination

    monkeypatch.setattr("yinshi.api.repos.clone_repo", clone_repository)
    rejected_session, rejected_initiator = await _open_session(
        tmp_path / "rejected",
        scopes=["repository.write"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    rejected_request, _ = _encrypted_request(
        rejected_initiator,
        method="POST",
        path="/api/repos",
        sequence=0,
        body={"name": "unsafe", "local_path": "/private/source"},
    )
    with pytest.raises(ValueError, match="local path"):
        (
            await rejected_session.handle_frame(
                rejected_request,
                current_time=1_900_000_002,
            )
        )[0]

    session, initiator = await _open_session(
        tmp_path / "accepted",
        scopes=["repository.write"],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    request, request_id = _encrypted_request(
        initiator,
        method="POST",
        path="/api/repos",
        sequence=0,
        body={
            "name": "project",
            "remote_url": "https://example.com/team/project.git",
        },
    )

    encrypted_response = (await session.handle_frame(request, current_time=1_900_000_002))[0]
    response = json.loads(initiator.decrypt(encrypted_response))

    assert response["status"] == 201
    assert response["request_id"] == request_id
    assert response["body"]["name"] == "project"
    assert Path(response["body"]["root_path"]).is_relative_to(worker_directory)


@pytest.mark.asyncio
async def test_encrypted_workspace_and_session_crud_use_scoped_worker_routes(
    tmp_path: Path,
    git_repo: str,
    db: sqlite3.Connection,
) -> None:
    """Workspace and session CRUD stays in one account-scoped worker app."""
    from shutil import copytree

    from yinshi.db import get_control_db, init_control_db
    from yinshi.main import create_app
    from yinshi.tenant import get_user_db

    worker_directory = tmp_path / "worker-crud"
    principal = WorkerPrincipal(
        tenant=TenantContext(
            user_id="user-1",
            email="worker-user@runner.invalid",
            data_dir=str(worker_directory),
            db_path=str(worker_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )
    init_control_db()
    with get_control_db() as control_database:
        control_database.execute(
            "INSERT INTO users (id, email, status) VALUES (?, ?, 'active')",
            (principal.tenant.user_id, principal.tenant.email),
        )
        control_database.commit()
    application = create_app(mode="worker", worker_principal=principal)
    dispatcher = WorkerHttpDispatcher(app=application, principal=principal)
    repository_id = "a" * 32
    repository_path = worker_directory / "repos" / repository_id
    copytree(git_repo, repository_path)
    with get_user_db(principal.tenant) as worker_db:
        worker_db.execute(
            "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
            (repository_id, "project", str(repository_path)),
        )
        worker_db.commit()

    async def prompt_events(request, selected_session_id, body):
        assert selected_session_id == worker_session_id
        assert body.prompt == "encrypted prompt"
        yield {"type": "status", "status": "started"}
        yield {"type": "result", "usage": {}}

    from yinshi.services.prompt_journal import PromptJournal
    from yinshi.services.terminal_journal import TerminalJournal

    class TerminalWriter:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        def write(self, data: bytes) -> None:
            self.messages.append(json.loads(data))

        async def drain(self) -> None:
            await asyncio.sleep(0)

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            await asyncio.sleep(0)

    terminal_reader = asyncio.StreamReader()
    terminal_writer = TerminalWriter()
    terminal_reader.feed_data(b'{"type":"init_status","success":true}\n')

    async def connect_terminal(_socket_path: str):
        return terminal_reader, terminal_writer

    prompt_journal = PromptJournal(executor=prompt_events)
    terminal_journal = TerminalJournal(
        connector=connect_terminal,
        scrollback_lines=1000,
        idle_seconds=7200,
    )
    application.state.prompt_journal = prompt_journal
    application.state.terminal_journal = terminal_journal
    session, initiator = await _open_session(
        tmp_path / "crud-session",
        scopes=[
            "files.read",
            "files.write",
            "pi.configure",
            "provider.configure",
            "workspace.read",
            "workspace.write",
            "session.read",
            "session.write",
            "session.stream",
            "terminal",
        ],
        dispatcher_factory=lambda _user_id: dispatcher,
    )
    create_workspace_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/repos/{repository_id}/workspaces",
        sequence=0,
        body={"name": "feature"},
    )
    create_workspace_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    create_workspace_request,
                    current_time=1_900_000_002,
                )
            )[0]
        )
    )
    assert create_workspace_response["status"] == 201
    workspace_id = create_workspace_response["body"]["id"]

    list_workspace_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=f"/api/repos/{repository_id}/workspaces",
        sequence=1,
        body=None,
    )
    list_workspace_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    list_workspace_request,
                    current_time=1_900_000_003,
                )
            )[0]
        )
    )
    assert [item["id"] for item in list_workspace_response["body"]] == [workspace_id]

    create_session_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/workspaces/{workspace_id}/sessions",
        sequence=2,
        body={"model": "anthropic/claude-sonnet-4"},
    )
    create_session_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    create_session_request,
                    current_time=1_900_000_004,
                )
            )[0]
        )
    )
    assert create_session_response["status"] == 201
    worker_session_id = create_session_response["body"]["id"]

    get_session_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=f"/api/sessions/{worker_session_id}",
        sequence=3,
        body=None,
    )
    get_session_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    get_session_request,
                    current_time=1_900_000_005,
                )
            )[0]
        )
    )
    assert get_session_response["status"] == 200
    assert get_session_response["body"]["workspace_id"] == workspace_id

    workspace_path = Path(create_workspace_response["body"]["path"])
    (workspace_path / "notes.txt").write_text("before", encoding="utf-8")
    read_file_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=f"/api/workspaces/{workspace_id}/files/preview",
        sequence=4,
        version=2,
        query={"path": "notes.txt"},
    )
    read_file_response = json.loads(
        initiator.decrypt(
            (await session.handle_frame(read_file_request, current_time=1_900_000_006))[0]
        )
    )
    assert read_file_response["body"] == {"path": "notes.txt", "content": "before"}
    assert read_file_response["v"] == 2

    write_file_request, _ = _encrypted_request(
        initiator,
        method="PUT",
        path=f"/api/workspaces/{workspace_id}/files/content",
        sequence=5,
        version=2,
        query={"path": "notes.txt"},
        body={"content": "after"},
    )
    write_file_response = json.loads(
        initiator.decrypt(
            (await session.handle_frame(write_file_request, current_time=1_900_000_007))[0]
        )
    )
    assert write_file_response["status"] == 200
    assert (workspace_path / "notes.txt").read_text(encoding="utf-8") == "after"

    tree_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=f"/api/workspaces/{workspace_id}/files/tree",
        sequence=6,
        version=2,
    )
    tree_response = json.loads(
        initiator.decrypt((await session.handle_frame(tree_request, current_time=1_900_000_008))[0])
    )
    assert tree_response["status"] == 200
    assert any(node["path"] == "notes.txt" for node in tree_response["body"]["files"])

    idempotency_key = "22222222-2222-4222-8222-222222222222"
    start_run_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/sessions/{worker_session_id}/runs",
        sequence=7,
        body={
            "prompt": "encrypted prompt",
            "model": None,
            "thinking": None,
            "idempotency_key": idempotency_key,
        },
    )
    start_run_response = json.loads(
        initiator.decrypt(
            (await session.handle_frame(start_run_request, current_time=1_900_000_009))[0]
        )
    )
    assert start_run_response["status"] == 202
    run_id = start_run_response["body"]["id"]

    event_response_body: dict[str, object] = {}
    for request_sequence in range(8, 18):
        event_request, _ = _encrypted_request(
            initiator,
            method="GET",
            path=f"/api/sessions/{worker_session_id}/runs/{run_id}/events/0",
            sequence=request_sequence,
            body=None,
        )
        event_response = json.loads(
            initiator.decrypt(
                (
                    await session.handle_frame(
                        event_request,
                        current_time=1_900_000_002 + request_sequence,
                    )
                )[0]
            )
        )
        assert event_response["status"] == 200
        event_response_body = event_response["body"]
        if event_response_body["status"] == "completed":
            break
        await asyncio.sleep(0)

    assert event_response_body["status"] == "completed"
    assert [event["type"] for event in event_response_body["events"]] == [
        "status",
        "result",
    ]

    terminal_reader.feed_data(
        (
            json.dumps(
                {
                    "id": workspace_id,
                    "type": "terminal_ready",
                    "cwd": "/workspace",
                    "pid": 123,
                    "replay": "",
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    terminal_sequence = request_sequence + 1
    start_terminal_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/workspaces/{workspace_id}/terminals",
        sequence=terminal_sequence,
        body={"cols": 80, "rows": 24},
    )
    start_terminal_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    start_terminal_request,
                    current_time=1_900_000_020,
                )
            )[0]
        )
    )
    assert start_terminal_response["status"] == 201
    terminal_id = start_terminal_response["body"]["id"]
    terminal_reader.feed_data(b'{"type":"terminal_data","data":"ready\\r\\n"}\n')
    await asyncio.sleep(0)

    terminal_events_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path=(f"/api/workspaces/{workspace_id}/terminals/{terminal_id}/events/0"),
        sequence=terminal_sequence + 1,
    )
    terminal_events_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    terminal_events_request,
                    current_time=1_900_000_021,
                )
            )[0]
        )
    )
    assert terminal_events_response["body"]["events"] == [
        {
            "id": workspace_id,
            "type": "terminal_ready",
            "cwd": "/workspace",
            "pid": 123,
            "replay": "",
        },
        {"type": "terminal_data", "data": "ready\r\n"},
    ]

    terminal_input_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/workspaces/{workspace_id}/terminals/{terminal_id}/input",
        sequence=terminal_sequence + 2,
        body={"data": "pwd\r"},
    )
    terminal_input_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    terminal_input_request,
                    current_time=1_900_000_022,
                )
            )[0]
        )
    )
    assert terminal_input_response["status"] == 204
    assert terminal_writer.messages[-1] == {
        "type": "terminal_input",
        "id": workspace_id,
        "data": "pwd\r",
    }

    from tests.test_pi_config import _build_pi_archive

    pi_archive = _build_pi_archive()
    start_upload_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path="/api/settings/pi-config/uploads",
        sequence=terminal_sequence + 3,
        version=2,
        body={
            "purpose": "pi_config",
            "filename": "pi-config.zip",
            "size_bytes": len(pi_archive),
            "sha256": hashlib.sha256(pi_archive).hexdigest(),
        },
    )
    start_upload_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    start_upload_request,
                    current_time=1_900_000_023,
                )
            )[0]
        )
    )
    assert start_upload_response["status"] == 201
    upload_id = start_upload_response["body"]["id"]
    encoded_archive = base64.urlsafe_b64encode(pi_archive).rstrip(b"=").decode("ascii")
    upload_chunk_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/settings/pi-config/uploads/{upload_id}/chunks/0",
        sequence=terminal_sequence + 4,
        version=2,
        body={"data": encoded_archive},
    )
    upload_chunk_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    upload_chunk_request,
                    current_time=1_900_000_024,
                )
            )[0]
        )
    )
    assert upload_chunk_response["body"]["next_chunk_index"] == 1

    complete_upload_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path=f"/api/settings/pi-config/uploads/{upload_id}/complete",
        sequence=terminal_sequence + 5,
        version=2,
    )
    complete_upload_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    complete_upload_request,
                    current_time=1_900_000_025,
                )
            )[0]
        )
    )
    assert complete_upload_response["status"] == 201
    assert complete_upload_response["body"]["status"] == "ready"

    create_provider_request, _ = _encrypted_request(
        initiator,
        method="POST",
        path="/api/settings/connections",
        sequence=terminal_sequence + 6,
        version=2,
        body={
            "provider": "anthropic",
            "auth_strategy": "api_key",
            "secret": "sk-ant-encrypted-worker-test",
            "label": "Worker Anthropic",
            "config": {},
        },
    )
    create_provider_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    create_provider_request,
                    current_time=1_900_000_026,
                )
            )[0]
        )
    )
    assert create_provider_response["status"] == 201
    assert "secret" not in create_provider_response["body"]

    list_provider_request, _ = _encrypted_request(
        initiator,
        method="GET",
        path="/api/settings/connections",
        sequence=terminal_sequence + 7,
        version=2,
    )
    list_provider_response = json.loads(
        initiator.decrypt(
            (
                await session.handle_frame(
                    list_provider_request,
                    current_time=1_900_000_027,
                )
            )[0]
        )
    )
    assert [item["provider"] for item in list_provider_response["body"]] == ["anthropic"]
    assert "sk-ant-encrypted-worker-test" not in json.dumps(list_provider_response)
    await prompt_journal.close()
    await terminal_journal.close_all()


@pytest.mark.parametrize("response_mode", [None, "push"])
def test_runner_rpc_request_accepts_only_exact_push_mode(response_mode: str | None) -> None:
    """V2 requests accept the legacy shape or literal push mode only."""
    payload, request_id = _transport_request_payload(
        method="GET",
        path=f"/api/sessions/{'a' * 32}/messages/bundle",
        sequence=0,
        response_mode=response_mode,
    )

    request = _parse_request(payload, expected_sequence=0)

    assert request.request_id == request_id
    assert request.response_mode == response_mode


@pytest.mark.parametrize("response_mode", ["pull", "", "PUSH", False, None])
def test_runner_rpc_request_rejects_invalid_explicit_response_mode(
    response_mode: object,
) -> None:
    """Present response_mode fields must contain only literal push."""
    payload, _ = _transport_request_payload(
        method="GET",
        path=f"/api/sessions/{'a' * 32}/messages/bundle",
        sequence=0,
    )
    request = json.loads(payload)
    request["response_mode"] = response_mode

    with pytest.raises(ValueError, match="response_mode"):
        _parse_request(
            json.dumps(request, separators=(",", ":")).encode(),
            expected_sequence=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("body_size, expected_frames", [(10, 1), (280_000, 5), (900_000, 14)])
async def test_history_bundle_push_returns_all_ordered_frames_without_pulls(
    body_size: int,
    expected_frames: int,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Negotiated history bundles push every canonical fragment in nonce order."""
    response_body = {"data": "x" * body_size}
    dispatcher = _BundleResponseDispatcher(response_body)
    session, initiator = await _open_session(
        tmp_path,
        scopes=["session.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
        max_session_bytes=16 * 1_024 * 1_024,
    )
    request_payload, request_id = _transport_request_payload(
        method="GET",
        path=f"/api/sessions/{'a' * 32}/messages/bundle",
        sequence=0,
        response_mode="push",
    )

    encrypted_frames = await session.handle_frame(
        bytes(initiator.encrypt(request_payload)),
        current_time=1_900_000_002,
    )

    assert len(encrypted_frames) == expected_frames
    plaintext_frames = [bytes(initiator.decrypt(frame)) for frame in encrypted_frames]
    if expected_frames == 1:
        response = json.loads(plaintext_frames[0])
    else:
        fragments = [
            _TRANSPORT_HEADER.unpack(frame[: _TRANSPORT_HEADER.size]) for frame in plaintext_frames
        ]
        total = fragments[0][4]
        assert fragments == [
            (_TRANSPORT_MAGIC, _TRANSPORT_RESPONSE, index, expected_frames, total)
            for index in range(expected_frames)
        ]
        response = json.loads(
            b"".join(frame[_TRANSPORT_HEADER.size :] for frame in plaintext_frames)
        )
    assert response["request_id"] == request_id
    assert response["body"] == response_body

    if expected_frames > 1:
        pull = _TRANSPORT_HEADER.pack(
            _TRANSPORT_MAGIC,
            _TRANSPORT_PULL,
            1,
            expected_frames,
            fragments[0][4],
        )
        with pytest.raises(ValueError, match="fragment"):
            await session.handle_frame(
                bytes(initiator.encrypt(pull)),
                current_time=1_900_000_003,
            )


@pytest.mark.asyncio
async def test_push_is_bundle_only_and_checks_response_limit_before_emission(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Push fails closed for other routes and oversized bundle responses."""
    wrong_route_session, wrong_route_initiator = await _open_session(tmp_path)
    wrong_route, _ = _transport_request_payload(
        method="GET",
        path="/health",
        sequence=0,
        response_mode="push",
    )
    with pytest.raises(ValueError, match="limited to history bundles"):
        await wrong_route_session.handle_frame(
            bytes(wrong_route_initiator.encrypt(wrong_route)),
            current_time=1_900_000_002,
        )

    response_body = {"data": "x" * (10 * 1_024 * 1_024)}
    dispatcher = _BundleResponseDispatcher(response_body)
    session, initiator = await _open_session(
        tmp_path,
        scopes=["session.read"],
        dispatcher_factory=lambda _user_id: dispatcher,
        max_session_bytes=16 * 1_024 * 1_024,
    )
    request, _ = _transport_request_payload(
        method="GET",
        path=f"/api/sessions/{'a' * 32}/messages/bundle",
        sequence=0,
        response_mode="push",
    )
    with pytest.raises(ValueError, match="exceeded transport limit"):
        await session.handle_frame(
            bytes(initiator.encrypt(request)),
            current_time=1_900_000_002,
        )


@pytest.mark.asyncio
async def test_legacy_request_transitions_to_fragments_for_large_response(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """A legacy request receives framed output only when its response exceeds Noise."""
    expected_body = {"content": "small"}
    response_body = {"content": "r" * 100_000}
    dispatcher = _LargeResponseDispatcher(
        expected_body=expected_body,
        response_body=response_body,
    )
    session, initiator = await _open_session(
        tmp_path,
        scopes=["files.write"],
        dispatcher_factory=lambda _user_id: dispatcher,
        max_session_bytes=1 * 1_024 * 1_024,
    )
    request, request_id = _encrypted_request(
        initiator,
        method="PUT",
        path=f"/api/workspaces/{_WORKSPACE_ID}/files/content",
        sequence=0,
        body=expected_body,
        version=2,
        query={"path": "large.txt"},
    )

    encrypted_first = (await session.handle_frame(request, current_time=1_900_000_002))[0]
    first = bytes(initiator.decrypt(encrypted_first))
    magic, kind, index, count, total = _TRANSPORT_HEADER.unpack(first[: _TRANSPORT_HEADER.size])
    assert (magic, kind, index) == (_TRANSPORT_MAGIC, _TRANSPORT_RESPONSE, 0)
    response = bytearray(total)
    first_payload = first[_TRANSPORT_HEADER.size :]
    response[0 : len(first_payload)] = first_payload
    for response_index in range(1, count):
        pull = _TRANSPORT_HEADER.pack(
            _TRANSPORT_MAGIC,
            _TRANSPORT_PULL,
            response_index,
            count,
            total,
        )
        encrypted_fragment = (
            await session.handle_frame(
                bytes(initiator.encrypt(pull)),
                current_time=1_900_000_003,
            )
        )[0]
        fragment = bytes(initiator.decrypt(encrypted_fragment))
        start = response_index * _TRANSPORT_PAYLOAD_BYTES_MAX
        fragment_payload = fragment[_TRANSPORT_HEADER.size :]
        response[start : start + len(fragment_payload)] = fragment_payload

    decoded = json.loads(response)
    assert decoded["request_id"] == request_id
    assert decoded["body"] == response_body


@pytest.mark.asyncio
async def test_encrypted_rpc_fragments_large_requests_and_responses(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Bounded encrypted fragments carry every supported large RPC payload."""
    file_content = "f" * (512 * 1_024)
    prompt = "p" * 100_000
    expected_body = {"content": file_content, "prompt": prompt}
    generic_body = "r" * (8 * 1_024 * 1_024)
    response_body = {
        "event": "e" * (1 * 1_024 * 1_024),
        "generic": generic_body,
    }
    dispatcher = _LargeResponseDispatcher(
        expected_body=expected_body,
        response_body=response_body,
    )
    session, initiator = await _open_session(
        tmp_path,
        scopes=["files.write"],
        dispatcher_factory=lambda _user_id: dispatcher,
        max_session_bytes=32 * 1_024 * 1_024,
    )
    request_payload, request_id = _transport_request_payload(
        method="PUT",
        path=f"/api/workspaces/{_WORKSPACE_ID}/files/content",
        sequence=0,
        body=expected_body,
        query={"path": "large.txt"},
    )

    response_payload = await _transport_round_trip(session, initiator, request_payload)
    response = json.loads(response_payload)

    assert response["request_id"] == request_id
    assert response["body"] == response_body


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_stream", ["out-of-order", "excessive", "truncated"])
async def test_encrypted_rpc_rejects_invalid_fragment_streams_and_fails_closed(
    invalid_stream: str,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Invalid fragment metadata closes the encrypted RPC session."""
    session, initiator = await _open_session(
        tmp_path,
        max_session_bytes=4 * 1_024 * 1_024,
    )
    payload, _ = _transport_request_payload(
        method="GET",
        path="/health",
        sequence=0,
        body={"value": "x" * 70_000},
    )
    frames = _transport_frames(payload, kind=_TRANSPORT_REQUEST)
    if invalid_stream == "out-of-order":
        invalid_frame = frames[1]
    elif invalid_stream == "excessive":
        invalid_frame = (
            _TRANSPORT_HEADER.pack(
                _TRANSPORT_MAGIC,
                _TRANSPORT_REQUEST,
                0,
                1_000,
                2 * 1_024 * 1_024 + 1,
            )
            + b"x"
        )
    else:
        first_response = (
            await session.handle_frame(
                bytes(initiator.encrypt(frames[0])),
                current_time=1_900_000_002,
            )
        )[0]
        initiator.decrypt(first_response)
        invalid_frame = _TRANSPORT_HEADER.pack(
            _TRANSPORT_MAGIC,
            _TRANSPORT_PULL,
            1,
            2,
            len(payload),
        )

    with pytest.raises(ValueError, match="fragment"):
        (
            await session.handle_frame(
                bytes(initiator.encrypt(invalid_frame)),
                current_time=1_900_000_003,
            )
        )[0]
    with pytest.raises(RuntimeError, match="failed"):
        (
            await session.handle_frame(
                bytes(initiator.encrypt(frames[0])),
                current_time=1_900_000_004,
            )
        )[0]


@pytest.mark.asyncio
async def test_encrypted_rpc_rejects_replayed_sequence(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Application ordering fails closed even when attacker sends valid ciphertext."""
    session, initiator = await _open_session(tmp_path)
    request, _request_id = _encrypted_request(
        initiator,
        method="GET",
        path="/health",
        sequence=1,
    )

    with pytest.raises(ValueError, match="sequence"):
        (await session.handle_frame(request, current_time=1_900_000_002))[0]
