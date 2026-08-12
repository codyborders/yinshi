"""Repository lifecycle review follow-up tests."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack


def test_delete_repo_discards_quarantine_after_post_commit_oserror(
    client: TestClient,
    git_repo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Post-commit OSError remains successful and quarantine disposal continues."""
    stack = create_full_stack(client, git_repo, name="post-commit-oserror")
    repo_id = str(stack["repo"]["id"])
    workspace_path = Path(str(stack["workspace"]["path"]))
    quarantine_path = workspace_path.parent / ".yinshi-delete-quarantine"
    caplog.set_level("ERROR", logger="yinshi.api.repos")

    with patch(
        "yinshi.api.repos.cleanup_repository_worktrees",
        new_callable=AsyncMock,
        side_effect=OSError("private cleanup detail"),
    ):
        response = client.delete(f"/api/repos/{repo_id}")

    assert response.status_code == 204
    assert not workspace_path.exists()
    assert not quarantine_path.exists()
    cleanup_records = [
        record
        for record in caplog.records
        if record.name == "yinshi.api.repos" and record.levelname == "ERROR"
    ]
    assert [record.getMessage() for record in cleanup_records] == ["Repository Git cleanup failed"]
    assert "private cleanup detail" not in caplog.text
