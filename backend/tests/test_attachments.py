"""Verify bounded session uploads, ownership, and durable file publication."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from tests.factories import create_full_stack


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_session_attachment_upload_publishes_detected_image(
    noauth_client: TestClient,
    git_repo: str,
    tmp_path: Path,
) -> None:
    """A complete digest-checked upload becomes a private session file."""
    stack = create_full_stack(noauth_client, git_repo)
    session_id = stack["session"]["id"]
    payload = bytes.fromhex("89504e470d0a1a0a") + b"test-image"
    started = noauth_client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "filename": "screen.png",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    assert started.status_code == 201, started.text
    attachment_id = started.json()["id"]

    appended = noauth_client.post(
        f"/api/sessions/{session_id}/attachments/{attachment_id}/chunks/0",
        json={"data": _base64url(payload)},
    )
    assert appended.status_code == 200, appended.text
    assert appended.json()["received_bytes"] == len(payload)

    completed = noauth_client.post(
        f"/api/sessions/{session_id}/attachments/{attachment_id}/complete"
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "ready"
    assert completed.json()["media_type"] == "image/png"
    stored = tmp_path / "users" / "attachments" / "sessions" / session_id / attachment_id
    assert stored.read_bytes() == payload
    assert stored.stat().st_mode & 0o777 == 0o600


def test_session_attachment_rejects_changed_retry_and_bad_digest(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Chunk retries must match, and completion must verify the declaration."""
    session_id = create_full_stack(noauth_client, git_repo)["session"]["id"]
    payload = b"plain text"
    started = noauth_client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "filename": "notes.txt",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(b"different").hexdigest(),
        },
    )
    attachment_id = started.json()["id"]
    path = f"/api/sessions/{session_id}/attachments/{attachment_id}"
    assert (
        noauth_client.post(f"{path}/chunks/0", json={"data": _base64url(payload)}).status_code
        == 200
    )
    retry = noauth_client.post(f"{path}/chunks/0", json={"data": _base64url(b"changed!!!")})
    assert retry.status_code == 400
    completed = noauth_client.post(f"{path}/complete")
    assert completed.status_code == 400
    assert "digest" in completed.json()["detail"]


def test_session_attachment_rejects_unsafe_filename(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """A client filename can never select a storage path."""
    session_id = create_full_stack(noauth_client, git_repo)["session"]["id"]
    response = noauth_client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "filename": "../secret.txt",
            "size_bytes": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        },
    )
    assert response.status_code == 400
    assert "simple filename" in response.json()["detail"]


def test_workspace_deletion_removes_session_attachment_directory(
    noauth_client: TestClient,
    git_repo: str,
    tmp_path: Path,
) -> None:
    """Deleting a workspace removes attachment bytes outside its worktree."""
    stack = create_full_stack(noauth_client, git_repo)
    session_id = stack["session"]["id"]
    payload = b"temporary context"
    started = noauth_client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "filename": "context.txt",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    attachment_id = started.json()["id"]
    member_path = f"/api/sessions/{session_id}/attachments/{attachment_id}"
    assert (
        noauth_client.post(
            f"{member_path}/chunks/0", json={"data": _base64url(payload)}
        ).status_code
        == 200
    )
    assert noauth_client.post(f"{member_path}/complete").status_code == 200
    session_directory = tmp_path / "users" / "attachments" / "sessions" / session_id
    assert session_directory.is_dir()

    deleted = noauth_client.delete(f"/api/workspaces/{stack['workspace']['id']}")

    assert deleted.status_code == 204, deleted.text
    assert not session_directory.exists()


def test_prompt_rejects_unknown_attachment_before_agent_start(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """A prompt cannot claim an attachment outside its ready session set."""
    session_id = create_full_stack(noauth_client, git_repo)["session"]["id"]

    response = noauth_client.post(
        f"/api/sessions/{session_id}/prompt",
        json={"prompt": "inspect it", "attachment_ids": ["f" * 32]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Attachment not found"


def test_prompt_forwards_ready_attachment_path_to_sidecar(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """A claimed file reaches the sidecar as a generated runtime path."""
    from unittest.mock import patch

    from tests.factories import make_mock_sidecar

    session_id = create_full_stack(noauth_client, git_repo)["session"]["id"]
    payload = b"context"
    started = noauth_client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "filename": "context.txt",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    ).json()
    member_path = f"/api/sessions/{session_id}/attachments/{started['id']}"
    assert (
        noauth_client.post(
            f"{member_path}/chunks/0", json={"data": _base64url(payload)}
        ).status_code
        == 200
    )
    assert noauth_client.post(f"{member_path}/complete").status_code == 200
    observed: list[dict[str, object]] = []

    async def query_with_attachment(
        _session_id: str,
        _prompt: str,
        attachments: list[dict[str, object]],
    ):
        observed.extend(attachments)
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    sidecar = make_mock_sidecar(query_with_attachment)
    with patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar):
        response = noauth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "inspect it", "attachment_ids": [started["id"]]},
        )

    assert response.status_code == 200, response.text
    assert observed == [
        {
            "id": started["id"],
            "filename": "context.txt",
            "media_type": "text/plain",
            "size_bytes": len(payload),
            "path": observed[0]["path"],
        }
    ]
    assert str(observed[0]["path"]).endswith(started["id"])
