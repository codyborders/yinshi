"""Behavior tests for privacy-safe backend logging."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

_USER_ID = "USER_ID_PRIVATE_SENTINEL"
_ACCOUNT_NAME = "ACCOUNT_NAME_PRIVATE_SENTINEL"
_REPOSITORY_PATH = "/REPOSITORY_PATH_PRIVATE_SENTINEL/repository"
_REPOSITORY_ID = "REPOSITORY_ID_PRIVATE_SENTINEL"
_CONNECTION_ID = "CONNPRIV"
_PROVIDER_RESPONSE = "PROVIDER_RESPONSE_PRIVATE_SENTINEL"
_EXCEPTION_TEXT = "EXCEPTION_TEXT_PRIVATE_SENTINEL"


async def test_settings_command_logs_exclude_private_values(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Command-list logs retain only command counts and fixed event text."""
    from yinshi.api import settings
    from yinshi.services.sidecar_runtime import TenantSidecarContext
    from yinshi.tenant import TenantContext

    user_sentinel = "USER_PRIVATE_SETTINGS_SENTINEL"
    tenant_sentinel = "TENANT_PRIVATE_SETTINGS_SENTINEL"
    event_sentinel = "EVENT_PRIVATE_SETTINGS_SENTINEL"
    socket_sentinel = f"/tmp/{tenant_sentinel}/private.sock"
    agent_sentinel = f"/tmp/{tenant_sentinel}/private-agent"
    tenant = TenantContext(
        user_id=user_sentinel,
        email=f"{tenant_sentinel}@example.test",
        data_dir=str(tmp_path / tenant_sentinel),
        db_path=str(tmp_path / tenant_sentinel / "yinshi.db"),
    )
    request = Mock()
    request.state.tenant = tenant
    caplog.set_level(logging.DEBUG, logger="yinshi.api.settings")
    with (
        patch(
            "yinshi.api.settings.resolve_tenant_sidecar_context",
            new=AsyncMock(
                return_value=TenantSidecarContext(
                    socket_path=socket_sentinel,
                    agent_dir=agent_sentinel,
                    settings_payload=None,
                )
            ),
        ),
        patch(
            "yinshi.api.settings._fetch_imported_commands",
            new=AsyncMock(return_value={"commands": [event_sentinel]}),
        ),
    ):
        await settings.list_pi_config_commands(request)

    private_values = (
        user_sentinel,
        user_sentinel[:8],
        tenant_sentinel,
        event_sentinel,
        socket_sentinel,
        agent_sentinel,
    )
    for record in caplog.records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(value not in rendered_record for value in private_values)
    assert "pi-config/commands: returning 1 commands" in caplog.text


@pytest.mark.parametrize("error_type", ["git", "app"])
async def test_github_relink_logs_exclude_private_context(
    error_type: str,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """GitHub relink failures use a fixed warning without tenant context."""
    from yinshi.api import auth_routes
    from yinshi.exceptions import GitError, GitHubAppError
    from yinshi.tenant import TenantContext

    error = GitError(_EXCEPTION_TEXT) if error_type == "git" else GitHubAppError(_EXCEPTION_TEXT)
    tenant = TenantContext(
        user_id=_USER_ID,
        email="private@example.test",
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "yinshi.db"),
    )
    database_context = Mock()
    database_context.__enter__ = Mock(return_value=Mock())
    database_context.__exit__ = Mock(return_value=False)
    caplog.set_level(logging.WARNING, logger="yinshi.api.auth_routes")

    with (
        patch("yinshi.api.auth_routes._resolve_tenant_from_user_id", return_value=tenant),
        patch("yinshi.api.auth_routes.get_user_db", return_value=database_context),
        patch(
            "yinshi.api.auth_routes.relink_github_repos_for_tenant",
            new=AsyncMock(side_effect=error),
        ),
    ):
        await auth_routes._refresh_connected_github_repos(_USER_ID, _ACCOUNT_NAME)

    assert _USER_ID not in caplog.text
    assert _ACCOUNT_NAME not in caplog.text
    assert _EXCEPTION_TEXT not in caplog.text
    assert caplog.messages == ["Failed to refresh GitHub repo links"]


@pytest.mark.parametrize("operation", ["remote-metadata", "worktree-sync"])
async def test_workspace_fallback_logs_exclude_repository_context(
    operation: str,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Workspace fallback warnings exclude repository context and failure text."""
    from yinshi.exceptions import GitError
    from yinshi.services import workspace
    from yinshi.tenant import TenantContext

    tenant = TenantContext(
        user_id=_USER_ID,
        email="private@example.test",
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "yinshi.db"),
    )
    caplog.set_level(logging.WARNING, logger="yinshi.services.workspace")

    if operation == "remote-metadata":
        with (
            patch(
                "yinshi.services.workspace.validate_local_repo", new=AsyncMock(return_value=True)
            ),
            patch(
                "yinshi.services.workspace._resolve_remote_checkout",
                new=AsyncMock(side_effect=GitError(_EXCEPTION_TEXT)),
            ),
        ):
            await workspace._refresh_repo_remote_metadata(
                tenant,
                _REPOSITORY_PATH,
                "https://github.com/private/repository.git",
                None,
            )
    else:
        database = Mock()
        database.execute.return_value.fetchone.return_value = {
            "id": _REPOSITORY_ID,
            "root_path": _REPOSITORY_PATH,
            "remote_url": "https://github.com/private/repository.git",
        }
        with (
            patch("yinshi.services.workspace.generate_branch_name", return_value="private-branch"),
            patch(
                "yinshi.services.workspace.resolve_remote_base_ref",
                new=AsyncMock(side_effect=GitError(_EXCEPTION_TEXT)),
            ),
            patch("yinshi.services.workspace.create_worktree", new=AsyncMock()),
            patch("yinshi.services.workspace.ensure_secret_guardrails"),
        ):
            await workspace._create_workspace_for_repo_unlocked(database, _REPOSITORY_ID)

    assert _REPOSITORY_PATH not in caplog.text
    assert _REPOSITORY_ID not in caplog.text
    assert _EXCEPTION_TEXT not in caplog.text


async def test_workspace_session_cleanup_log_excludes_session_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Session-file cleanup failures use fixed warning text."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from yinshi.services import workspace

    session_id = "SESSION_FILE_PRIVATE_SENTINEL"
    workspace_id = "workspace-log-privacy"
    workspace_row = {
        "id": workspace_id,
        "repo_id": "repository-log-privacy",
        "path": "/tmp/workspace-log-privacy",
    }
    first_workspace_result = Mock()
    first_workspace_result.fetchone.return_value = workspace_row
    second_workspace_result = Mock()
    second_workspace_result.fetchone.return_value = workspace_row
    delegation_result = Mock()
    delegation_result.fetchone.return_value = {"child_count": 0}
    sessions_result = Mock()
    sessions_result.fetchall.return_value = [{"id": session_id}]
    repository_result = Mock()
    repository_result.fetchone.return_value = {
        "id": "repository-log-privacy",
        "root_path": "/tmp/repository-log-privacy",
    }
    delete_result = Mock()
    database = Mock()
    database.execute.side_effect = [
        first_workspace_result,
        second_workspace_result,
        delegation_result,
        sessions_result,
        repository_result,
        delete_result,
    ]
    caplog.set_level(logging.WARNING, logger="yinshi.services.workspace")

    @asynccontextmanager
    async def unlocked(_repo_id: str, _lock_root: Path) -> AsyncIterator[None]:
        yield

    with (
        patch("yinshi.services.workspace.repository_lifecycle", side_effect=unlocked),
        patch(
            "yinshi.services.repository_lifecycle.get_settings",
            return_value=Mock(db_path="/tmp/yinshi.db"),
        ),
        patch(
            "yinshi.services.workspace.delete_local_pi_session_file",
            side_effect=OSError("SESSION_FILE_PATH_PRIVATE_SENTINEL"),
        ),
        patch("yinshi.services.workspace.delete_worktree", new=AsyncMock()),
    ):
        await workspace.delete_workspace(database, workspace_id)

    assert session_id not in caplog.text
    assert "SESSION_FILE_PATH_PRIVATE_SENTINEL" not in caplog.text
    assert caplog.messages == ["Failed to delete Pi session file"]


def test_missing_provider_connection_log_excludes_connection_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing connection refreshes use a fixed warning."""
    from yinshi.services import provider_connections

    database = Mock()
    database.execute.return_value.fetchone.return_value = None
    database_context = Mock()
    database_context.__enter__ = Mock(return_value=database)
    database_context.__exit__ = Mock(return_value=False)
    caplog.set_level(logging.WARNING, logger="yinshi.services.provider_connections")

    with patch(
        "yinshi.services.provider_connections.get_control_db",
        return_value=database_context,
    ):
        provider_connections.update_provider_connection_secret(
            _USER_ID,
            _CONNECTION_ID,
            "api_key",
            "unused-private-secret",
        )

    assert _CONNECTION_ID not in caplog.text
    assert caplog.messages == ["Skipping refresh for missing provider connection"]
