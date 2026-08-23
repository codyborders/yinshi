"""Tests for Pydantic models."""

from datetime import datetime


def test_repo_create_minimal():
    """RepoCreate should work with just a name."""
    from yinshi.models import RepoCreate

    repo = RepoCreate(name="my-repo")
    assert repo.name == "my-repo"
    assert repo.remote_url is None
    assert repo.local_path is None


def test_repo_create_with_url():
    """RepoCreate should accept a remote URL."""
    from yinshi.models import RepoCreate

    repo = RepoCreate(name="my-repo", remote_url="https://github.com/user/repo")
    assert repo.remote_url == "https://github.com/user/repo"


def test_repo_out():
    """RepoOut should serialize all fields."""
    from yinshi.models import RepoOut

    repo = RepoOut(
        id="abc123",
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
        name="test",
        root_path="/tmp/test",
    )
    assert repo.id == "abc123"
    assert repo.root_path == "/tmp/test"


def test_workspace_create_defaults():
    """WorkspaceCreate should have optional name."""
    from yinshi.models import WorkspaceCreate

    ws = WorkspaceCreate()
    assert ws.name is None


def test_session_create_defaults():
    """SessionCreate should default to the current MiniMax model key."""
    from yinshi.models import SessionCreate

    s = SessionCreate()
    assert s.model == "minimax/MiniMax-M2.7"


def test_ws_prompt():
    """WSPrompt should carry prompt text."""
    from yinshi.models import WSPrompt

    msg = WSPrompt(prompt="Hello, world")
    assert msg.type == "prompt"
    assert msg.prompt == "Hello, world"


def test_ws_cancel():
    """WSCancel should have type cancel."""
    from yinshi.models import WSCancel

    msg = WSCancel()
    assert msg.type == "cancel"


def test_workspace_update_valid_states():
    """WorkspaceUpdate should accept 'ready' and 'archived'."""
    from yinshi.models import WorkspaceUpdate

    assert WorkspaceUpdate(state="ready").state == "ready"
    assert WorkspaceUpdate(state="archived").state == "archived"
    assert WorkspaceUpdate().state is None


def test_workspace_update_invalid_state():
    """WorkspaceUpdate should reject invalid state values."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import WorkspaceUpdate

    with pytest.raises(ValidationError):
        WorkspaceUpdate(state="deleted")


def test_pi_config_import_strips_whitespace():
    """PiConfigImport should strip surrounding whitespace from the repo URL."""
    from yinshi.models import PiConfigImport

    body = PiConfigImport(repo_url="  owner/repo  ")
    assert body.repo_url == "owner/repo"


def test_runner_github_access_success_rejects_coercible_ids():
    """Runner broker success fields should use strict positive integers."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import RunnerGitHubAccessOut

    with pytest.raises(ValidationError):
        RunnerGitHubAccessOut.model_validate(
            {
                "clone_url": "https://github.com/owner/repo.git",
                "repository_installation_id": 456,
                "installation_id": "123",
                "access_token": "short-lived-token",
                "manage_url": None,
            }
        )


def test_runner_github_access_success_hides_token_from_repr():
    """Runner broker success models should not reveal access tokens."""
    from yinshi.models import RunnerGitHubAccessOut

    response = RunnerGitHubAccessOut(
        clone_url="https://github.com/owner/repo.git",
        repository_installation_id=456,
        installation_id=123,
        access_token="short-lived-model-secret",
        manage_url=None,
    )

    assert "short-lived-model-secret" not in repr(response)


def test_runner_github_access_success_rejects_invalid_text_fields():
    """Runner broker success text fields should be bounded and unpadded."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import RunnerGitHubAccessOut

    valid_payload = {
        "clone_url": "https://github.com/owner/repo.git",
        "repository_installation_id": 456,
        "installation_id": 123,
        "access_token": "short-lived-token",
        "manage_url": None,
    }
    invalid_fields = (
        ("clone_url", " "),
        ("access_token", ""),
        ("manage_url", "x" * 4097),
    )
    for field_name, invalid_value in invalid_fields:
        payload = {**valid_payload, field_name: invalid_value}
        with pytest.raises(ValidationError):
            RunnerGitHubAccessOut.model_validate(payload)


def test_runner_github_access_error_rejects_invalid_text_fields():
    """Runner broker error fields should be strict, bounded, and unpadded."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import RunnerGitHubAccessErrorOut

    valid_detail = {
        "code": "install_not_found",
        "message": "Repository not installed",
        "connect_url": None,
        "manage_url": None,
    }
    invalid_fields = (
        ("code", 123),
        ("message", " "),
        ("connect_url", "x" * 4097),
    )
    for field_name, invalid_value in invalid_fields:
        payload = {"detail": {**valid_detail, field_name: invalid_value}}
        with pytest.raises(ValidationError):
            RunnerGitHubAccessErrorOut.model_validate(payload)


def test_runner_github_access_error_rejects_extra_fields():
    """Runner broker error envelopes should reject unknown protocol fields."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import RunnerGitHubAccessErrorOut

    with pytest.raises(ValidationError):
        RunnerGitHubAccessErrorOut.model_validate(
            {
                "detail": {
                    "code": "install_not_found",
                    "message": "Repository not installed",
                    "connect_url": None,
                    "manage_url": None,
                    "unexpected": "field",
                }
            }
        )


def test_pi_config_category_update_rejects_duplicates():
    """PiConfigCategoryUpdate should reject duplicate category names."""
    import pytest
    from pydantic import ValidationError

    from yinshi.models import PiConfigCategoryUpdate

    with pytest.raises(ValidationError):
        PiConfigCategoryUpdate(enabled_categories=["skills", "skills"])
