"""Tests for GitHub App service behavior.

These tests cover three layers of the integration:
- URL normalization for supported GitHub input forms
- token caching and per-user installation resolution
- fallback behavior when the GitHub App is not configured
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import _configure_test_env


@pytest.fixture()
def github_app_env(tmp_path, monkeypatch) -> Iterator[None]:
    """Configure a fresh GitHub App test environment."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_bytes(private_key_pem)

    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("GITHUB_APP_SLUG", "yinshi-dev")

    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    init_control_db()
    yield
    get_settings.cache_clear()


def _insert_user(user_id: str) -> None:
    """Insert a minimal user row for foreign-key-safe control DB tests."""
    from yinshi.db import get_control_db

    with get_control_db() as db:
        db.execute(
            "INSERT INTO users (id, email) VALUES (?, ?)",
            (user_id, f"{user_id}@example.com"),
        )
        db.commit()


@pytest.mark.parametrize(
    ("value", "expected_url"),
    [
        ("owner/repo", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo.git"),
    ],
)
def test_normalize_github_remote_supports_expected_inputs(
    value: str,
    expected_url: str,
) -> None:
    """normalize_github_remote should canonicalize supported GitHub inputs."""
    from yinshi.services.github_app import normalize_github_remote

    remote = normalize_github_remote(value)
    assert remote is not None
    assert remote.owner == "owner"
    assert remote.repo == "repo"
    assert remote.clone_url == expected_url


def test_normalize_github_remote_rejects_extra_path_segments() -> None:
    """normalize_github_remote should reject non-repo GitHub paths."""
    from yinshi.services.github_app import normalize_github_remote

    assert normalize_github_remote("https://github.com/owner/repo/issues") is None
    assert normalize_github_remote("https://gitlab.com/owner/repo") is None


@pytest.mark.asyncio
async def test_get_installation_token_uses_cache(github_app_env) -> None:
    """get_installation_token should reuse a cached token until refresh time."""
    from yinshi.services import github_app

    github_app._INSTALLATION_TOKEN_CACHE.clear()

    with (
        patch.object(github_app, "generate_app_jwt", return_value="app-jwt"),
        patch.object(
            github_app,
            "_request_github_json",
            new=AsyncMock(
                return_value=(
                    201,
                    {
                        "token": "installation-token",
                        "expires_at": "2999-01-01T00:00:00Z",
                    },
                )
            ),
        ) as request_mock,
    ):
        first_token = await github_app.get_installation_token(101)
        second_token = await github_app.get_installation_token(101)

    assert first_token == "installation-token"
    assert second_token == "installation-token"
    assert request_mock.await_count == 1


@pytest.mark.asyncio
async def test_resolve_github_clone_access_adds_manage_hint_for_owner_installation(
    github_app_env,
) -> None:
    """resolve_github_clone_access should keep a manage URL hint for excluded repos."""
    from yinshi.db import get_control_db
    from yinshi.services.github_app import resolve_github_clone_access

    _insert_user("user-1")
    with get_control_db() as db:
        db.execute(
            """
            INSERT INTO github_installations (
                user_id, installation_id, account_login, account_type, html_url
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "user-1",
                501,
                "Acme",
                "Organization",
                "https://github.com/organizations/acme/settings/installations/501",
            ),
        )
        db.commit()

    with patch(
        "yinshi.services.github_app.get_repo_installation",
        new=AsyncMock(return_value=None),
    ):
        clone_access = await resolve_github_clone_access(
            "user-1",
            "git@github.com:Acme/private-repo.git",
        )

    assert clone_access is not None
    assert clone_access.clone_url == "https://github.com/Acme/private-repo.git"
    assert clone_access.installation_id is None
    assert clone_access.access_token is None
    assert clone_access.manage_url is not None
    assert clone_access.manage_url.endswith("/501")


@pytest.mark.asyncio
async def test_resolve_github_clone_access_returns_anonymous_probe_without_saved_installation(
    github_app_env,
) -> None:
    """Private repos should still probe anonymously until auth is actually needed."""
    from yinshi.services.github_app import resolve_github_clone_access

    _insert_user("user-2")

    with patch(
        "yinshi.services.github_app.get_repo_installation",
        new=AsyncMock(return_value=777),
    ):
        clone_access = await resolve_github_clone_access(
            "user-2",
            "https://github.com/example/private-repo",
        )

    assert clone_access is not None
    assert clone_access.clone_url == "https://github.com/example/private-repo.git"
    assert clone_access.repository_installation_id == 777
    assert clone_access.installation_id is None
    assert clone_access.access_token is None
    assert clone_access.manage_url is None


@pytest.mark.asyncio
async def test_resolve_github_clone_access_preserves_manage_url_for_revoked_installation(
    github_app_env,
) -> None:
    """Revoked installations should keep the manage URL needed for recovery."""
    from yinshi.db import get_control_db
    from yinshi.exceptions import GitHubInstallationUnusableError
    from yinshi.services import github_app

    _insert_user("user-3")
    with get_control_db() as db:
        db.execute(
            """
            INSERT INTO github_installations (
                user_id, installation_id, account_login, account_type, html_url
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "user-3",
                888,
                "acme",
                "Organization",
                "https://github.com/organizations/acme/settings/installations/888",
            ),
        )
        db.commit()

    with (
        patch.object(github_app, "get_repo_installation", new=AsyncMock(return_value=888)),
        patch.object(
            github_app,
            "get_installation_token",
            new=AsyncMock(
                side_effect=GitHubInstallationUnusableError(
                    "The connected GitHub installation is no longer usable."
                )
            ),
        ),
    ):
        with pytest.raises(GitHubInstallationUnusableError) as error_info:
            await github_app.resolve_github_clone_access(
                "user-3",
                "https://github.com/acme/private-repo",
            )

    assert error_info.value.manage_url is not None
    assert error_info.value.manage_url.endswith("/888")


@pytest.mark.asyncio
async def test_resolve_github_clone_access_falls_back_to_anonymous_when_unconfigured(
    tmp_path,
    monkeypatch,
) -> None:
    """resolve_github_clone_access should not require app config for public GitHub URLs."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    from yinshi.config import get_settings
    from yinshi.services.github_app import resolve_github_clone_access

    get_settings.cache_clear()
    try:
        clone_access = await resolve_github_clone_access(
            None,
            "owner/public-repo",
        )
    finally:
        get_settings.cache_clear()

    assert clone_access is not None
    assert clone_access.clone_url == "https://github.com/owner/public-repo.git"
    assert clone_access.repository_installation_id is None
    assert clone_access.installation_id is None
    assert clone_access.access_token is None
