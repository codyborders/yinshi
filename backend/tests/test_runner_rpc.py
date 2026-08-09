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
from yinshi.services.runner_rpc import EncryptedRunnerRpcSession
from yinshi.tenant import TenantContext
from yinshi.worker_auth import WorkerPrincipal
from yinshi.worker_runtime import WorkerHttpDispatcher

_RUNNER_PRIVATE_KEY = bytes.fromhex(
    "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
)
_CLIENT_PRIVATE_KEY = bytes.fromhex(
    "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1"
)


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
) -> tuple[EncryptedRunnerRpcSession, NoiseConnection]:
    runner_public_key = _public_key(_RUNNER_PRIVATE_KEY)
    client_public_key = _public_key(_CLIENT_PRIVATE_KEY)
    capability, claims = create_runner_capability(
        user_id="user-1",
        runner_id="runner-1",
        runner_public_key=_base64url(runner_public_key),
        initiator_public_key=_base64url(client_public_key),
        scopes=scopes or ["worker.health"],
        max_session_bytes=65_536,
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
    response = await rpc_session.handle_frame(first_message, current_time=1_900_000_001)
    assert json.loads(initiator.read_message(response))["transfer_id"] == claims.transfer_id
    return rpc_session, initiator


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


@pytest.mark.asyncio
async def test_encrypted_health_rpc_returns_allowlisted_metadata(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Health request crosses the authenticated channel without relay plaintext."""
    session, initiator = await _open_session(tmp_path)
    request, request_id = _encrypted_request(
        initiator,
        method="GET",
        path="/health",
        sequence=0,
    )

    encrypted_response = await session.handle_frame(request, current_time=1_900_000_002)
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

    encrypted_response = await session.handle_frame(request, current_time=1_900_000_002)
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
    provider_response = await session.handle_frame(
        provider_request,
        current_time=1_900_000_002,
    )
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
        await session.handle_frame(unexpected_query_request, current_time=1_900_000_003)


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
        await rejected_session.handle_frame(
            rejected_request,
            current_time=1_900_000_002,
        )

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

    encrypted_response = await session.handle_frame(request, current_time=1_900_000_002)
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
            await session.handle_frame(
                create_workspace_request,
                current_time=1_900_000_002,
            )
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
            await session.handle_frame(
                list_workspace_request,
                current_time=1_900_000_003,
            )
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
            await session.handle_frame(
                create_session_request,
                current_time=1_900_000_004,
            )
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
            await session.handle_frame(
                get_session_request,
                current_time=1_900_000_005,
            )
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
        initiator.decrypt(await session.handle_frame(read_file_request, current_time=1_900_000_006))
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
            await session.handle_frame(write_file_request, current_time=1_900_000_007)
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
        initiator.decrypt(await session.handle_frame(tree_request, current_time=1_900_000_008))
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
        initiator.decrypt(await session.handle_frame(start_run_request, current_time=1_900_000_009))
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
                await session.handle_frame(
                    event_request,
                    current_time=1_900_000_002 + request_sequence,
                )
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
            await session.handle_frame(
                start_terminal_request,
                current_time=1_900_000_020,
            )
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
            await session.handle_frame(
                terminal_events_request,
                current_time=1_900_000_021,
            )
        )
    )
    assert terminal_events_response["body"]["events"] == [
        {"type": "terminal_data", "data": "ready\r\n"}
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
            await session.handle_frame(
                terminal_input_request,
                current_time=1_900_000_022,
            )
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
            await session.handle_frame(
                start_upload_request,
                current_time=1_900_000_023,
            )
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
            await session.handle_frame(
                upload_chunk_request,
                current_time=1_900_000_024,
            )
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
            await session.handle_frame(
                complete_upload_request,
                current_time=1_900_000_025,
            )
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
            await session.handle_frame(
                create_provider_request,
                current_time=1_900_000_026,
            )
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
            await session.handle_frame(
                list_provider_request,
                current_time=1_900_000_027,
            )
        )
    )
    assert [item["provider"] for item in list_provider_response["body"]] == ["anthropic"]
    assert "sk-ant-encrypted-worker-test" not in json.dumps(list_provider_response)
    await prompt_journal.close()
    await terminal_journal.close_all()


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
        await session.handle_frame(request, current_time=1_900_000_002)
