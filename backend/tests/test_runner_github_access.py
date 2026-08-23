"""Runner GitHub access endpoint validation, authentication, and responses."""

import asyncio
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

_RUNNER_REGISTRATION: dict[str, object] = {
    "runner_version": "0.1.0",
    "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
    "data_dir": "/var/lib/yinshi",
    "sqlite_dir": "/var/lib/yinshi/sqlite",
    "shared_files_dir": "/mnt/yinshi-s3-files",
    "storage_profile": "aws_ebs_s3_files",
    "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
}


@pytest.fixture
def runner_token(auth_client: TestClient) -> str:
    """Register one runner and return its bearer token."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    register_response = auth_client.post(
        "/runner/register",
        json={
            **_RUNNER_REGISTRATION,
            "registration_token": create_response.json()["registration_token"],
        },
    )
    assert register_response.status_code == 201
    return cast(str, register_response.json()["runner_token"])


def test_runner_github_access_rejects_extra_fields(
    auth_client: TestClient,
    runner_token: str,
) -> None:
    """The endpoint rejects unexpected request fields."""
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git", "extra_field": True},
    )
    assert response.status_code == 422


def test_runner_github_access_requires_bearer_token(
    auth_client: TestClient,
) -> None:
    """The endpoint rejects unauthenticated requests."""
    response = auth_client.post(
        "/runner/github-access",
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Runner bearer token is required"


def test_runner_github_access_handler_returns_strict_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler returns its validated protocol model."""
    from yinshi.api import runners
    from yinshi.models import RunnerGitHubAccessIn, RunnerGitHubAccessOut
    from yinshi.services.github_app import GitHubCloneAccess

    async def fake_resolve(user_id: str, remote_url: str) -> GitHubCloneAccess:
        return GitHubCloneAccess(
            clone_url=remote_url,
            access_token="short-lived-token",
            installation_id=123,
            repository_installation_id=456,
            manage_url=None,
        )

    def fake_authenticate_runner_token(token: str) -> dict[str, Any]:
        return {"user_id": "user-1"}

    monkeypatch.setattr(
        runners,
        "authenticate_runner_token",
        fake_authenticate_runner_token,
    )
    monkeypatch.setattr(runners, "_resolve_github_clone_access", fake_resolve)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/runner/github-access",
            "headers": [(b"authorization", b"Bearer runner-token")],
        }
    )

    response = asyncio.run(
        runners.resolve_runner_github_access(
            RunnerGitHubAccessIn(remote_url="https://github.com/owner/repo.git"),
            request,
        )
    )

    assert isinstance(response, RunnerGitHubAccessOut)


def test_runner_github_access_returns_canonical_fields(
    auth_client: TestClient,
    runner_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner GitHub access endpoint returns canonical response fields."""
    from yinshi.services.github_app import GitHubCloneAccess

    mock_access = GitHubCloneAccess(
        clone_url="https://github.com/owner/repo.git",
        access_token="ghp_testtoken",
        installation_id=123,
        repository_installation_id=456,
        manage_url="https://github.com/apps/yinshi/installations/123",
    )

    async def fake_resolve(user_id: str, remote_url: str) -> GitHubCloneAccess:
        return mock_access

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_resolve,
    )

    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["clone_url"] == "https://github.com/owner/repo.git"
    assert body["access_token"] == "ghp_testtoken"
    assert body["repository_installation_id"] == 456
    assert body["installation_id"] == 123
    assert "manage_url" in body


def test_runner_github_access_handles_access_error(
    auth_client: TestClient,
    runner_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint returns structured 400 for GitHubAccessError."""
    from yinshi.exceptions import GitHubAccessError

    async def fake_access_error(user_id: str, remote_url: str) -> None:
        raise GitHubAccessError(
            code="install_not_found",
            message="Repository not installed",
            connect_url="https://github.com/apps/yinshi",
            manage_url="https://github.com/settings/installations",
        )

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_access_error,
    )
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "install_not_found"
    assert detail["message"] == "Repository not installed"
    assert detail["connect_url"] == "https://github.com/apps/yinshi"
    assert detail["manage_url"] == "https://github.com/settings/installations"


def test_runner_github_access_rejects_invalid_error_payload(
    auth_client: TestClient,
    runner_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid integration failures should not reach the runner protocol."""
    from yinshi.exceptions import GitHubAccessError

    async def fake_access_error(user_id: str, remote_url: str) -> None:
        raise GitHubAccessError(
            code=cast(str, 123),
            message="Repository not installed",
        )

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_access_error,
    )

    with TestClient(auth_client.app, raise_server_exceptions=False) as safe_client:
        response = safe_client.post(
            "/runner/github-access",
            headers={"Authorization": f"Bearer {runner_token}"},
            json={"remote_url": "https://github.com/owner/repo.git"},
        )

    assert response.status_code == 500


def test_runner_github_access_handles_app_error(
    auth_client: TestClient,
    runner_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint returns 502 for GitHubAppError."""
    from yinshi.exceptions import GitHubAppError

    async def fake_app_error(user_id: str, remote_url: str) -> None:
        raise GitHubAppError("GitHub API unreachable")

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_app_error,
    )
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub integration error"


def test_runner_github_access_returns_null_for_non_github(
    auth_client: TestClient,
    runner_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-GitHub remotes return JSON null."""

    async def fake_null_resolve(user_id: str, remote_url: str) -> None:
        return None

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_null_resolve,
    )
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://gitlab.com/owner/repo.git"},
    )
    assert response.status_code == 200
    assert response.json() is None
