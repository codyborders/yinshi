"""Tests for custom exception hierarchy."""


def test_exception_hierarchy():
    """All custom exceptions should inherit from YinshiError."""
    from yinshi.exceptions import (
        YinshiError,
        RepoNotFoundError,
        WorkspaceNotFoundError,
        SessionNotFoundError,
        GitError,
        SidecarError,
        SidecarNotConnectedError,
    )

    assert issubclass(RepoNotFoundError, YinshiError)
    assert issubclass(WorkspaceNotFoundError, YinshiError)
    assert issubclass(SessionNotFoundError, YinshiError)
    assert issubclass(GitError, YinshiError)
    assert issubclass(SidecarError, YinshiError)
    assert issubclass(SidecarNotConnectedError, SidecarError)


def test_exceptions_carry_message():
    """Exceptions should carry their message."""
    from yinshi.exceptions import GitError

    err = GitError("clone failed")
    assert str(err) == "clone failed"
