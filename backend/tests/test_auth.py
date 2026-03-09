"""Tests for authentication module."""


def test_create_and_verify_session_token():
    """Session tokens should be creatable and verifiable."""
    from yinshi.auth import create_session_token, verify_session_token

    token = create_session_token("user@example.com")
    assert isinstance(token, str)
    assert len(token) > 0

    email = verify_session_token(token)
    assert email == "user@example.com"


def test_verify_invalid_token():
    """Invalid tokens should return None."""
    from yinshi.auth import verify_session_token

    result = verify_session_token("garbage-token")
    assert result is None


def test_verify_empty_token():
    """Empty token should return None."""
    from yinshi.auth import verify_session_token

    result = verify_session_token("")
    assert result is None
