"""Additional runner endpoint tests.

Tests for runner GitHub access validation, bearer auth enforcement,
canonical field returns, error handling, and null responses.
"""


def test_runner_github_access_rejects_extra_fields(auth_client) -> None:
    """The runner GitHub access endpoint rejects unexpected fields with 422."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    assert register_response.status_code == 201
    runner_token = register_response.json()["runner_token"]
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git", "extra_field": True},
    )
    assert response.status_code == 422


def test_runner_github_access_requires_bearer_token(auth_client) -> None:
    """The runner GitHub access endpoint rejects unauthenticated requests."""
    response = auth_client.post(
        "/runner/github-access",
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Runner bearer token is required"


def test_runner_github_access_returns_canonical_fields(auth_client, monkeypatch) -> None:
    """The runner GitHub access endpoint returns canonical response fields."""
    from yinshi.services.github_app import GitHubCloneAccess

    mock_access = GitHubCloneAccess(
        clone_url="https://github.com/owner/repo.git",
        access_token="ghp_testtoken",
        installation_id=123,
        repository_installation_id=456,
        manage_url="https://github.com/apps/yinshi/installations/123",
    )

    async def fake_resolve(user_id, url):
        return mock_access

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        lambda uid, url: fake_resolve(uid, url),
    )

    # Create a runner and get its token
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    create_payload = create_response.json()

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_payload["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    assert register_response.status_code == 201
    runner_token = register_response.json()["runner_token"]
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


def test_runner_github_access_handles_access_error(auth_client, monkeypatch) -> None:
    """The endpoint returns structured 400 for GitHubAccessError."""
    from yinshi.exceptions import GitHubAccessError

    async def fake_access_error(uid, url):
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
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    runner_token = register_response.json()["runner_token"]
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


def test_runner_github_access_handles_app_error(auth_client, monkeypatch) -> None:
    """The endpoint returns 502 for GitHubAppError."""
    from yinshi.exceptions import GitHubAppError

    async def fake_app_error(uid, url):
        raise GitHubAppError("GitHub API unreachable")

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_app_error,
    )
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    runner_token = register_response.json()["runner_token"]
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://github.com/owner/repo.git"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub integration error"


def test_runner_github_access_returns_null_for_non_github(auth_client, monkeypatch) -> None:
    """Non-GitHub remotes return JSON null."""

    async def fake_null_resolve(uid, url):
        return None

    monkeypatch.setattr(
        "yinshi.api.runners._resolve_github_clone_access",
        fake_null_resolve,
    )
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "test", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    runner_token = register_response.json()["runner_token"]
    response = auth_client.post(
        "/runner/github-access",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={"remote_url": "https://gitlab.com/owner/repo.git"},
    )
    assert response.status_code == 200
    assert response.json() is None
