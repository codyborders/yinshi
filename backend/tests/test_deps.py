"""Tests for shared API dependency helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def test_get_user_email_returns_email():
    """Should return user_email from request state when present."""
    from yinshi.api.deps import get_user_email

    request = MagicMock()
    request.state.user_email = "user@example.com"
    assert get_user_email(request) == "user@example.com"


def test_get_user_email_returns_none_when_missing():
    """Should return None when user_email is not set on request state."""
    from yinshi.api.deps import get_user_email

    request = MagicMock(spec=[])
    request.state = MagicMock(spec=[])
    assert get_user_email(request) is None


def test_check_owner_allows_matching_emails():
    """Should not raise when owner and user emails match."""
    from yinshi.api.deps import check_owner

    check_owner("user@example.com", "user@example.com")


def test_check_owner_raises_on_mismatch():
    """Should raise 403 when owner and user emails differ."""
    from yinshi.api.deps import check_owner

    with pytest.raises(HTTPException) as exc_info:
        check_owner("owner@example.com", "other@example.com")
    assert exc_info.value.status_code == 403


def test_check_owner_allows_none_user():
    """Should not raise when user_email is None (auth disabled)."""
    from yinshi.api.deps import check_owner

    check_owner("owner@example.com", None)


def test_check_owner_allows_none_owner():
    """Should not raise when owner_email is None."""
    from yinshi.api.deps import check_owner

    check_owner(None, "user@example.com")


def test_check_workspace_owner_404_when_missing():
    """Missing workspaces should raise 404 instead of silently passing."""
    from yinshi.api.deps import check_workspace_owner

    request = SimpleNamespace(state=SimpleNamespace())
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        check_workspace_owner(db, "missing-workspace", request)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Workspace not found"


def test_check_session_owner_404_when_missing():
    """Missing sessions should raise 404 instead of silently passing."""
    from yinshi.api.deps import check_session_owner

    request = SimpleNamespace(state=SimpleNamespace())
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        check_session_owner(db, "missing-session", request)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"
