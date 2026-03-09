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
    """SessionCreate should default to minimax model."""
    from yinshi.models import SessionCreate

    s = SessionCreate()
    assert s.model == "minimax"


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
