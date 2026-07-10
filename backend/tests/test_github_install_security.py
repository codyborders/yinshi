"""GitHub App install tests prove user-to-installation ownership binding."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import _configure_test_env


def _configure_github_app_settings(tmp_path, monkeypatch) -> None:
    """Configure inert GitHub App credentials for route tests."""
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_text("test-private-key", encoding="utf-8")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("GITHUB_APP_SLUG", "yinshi-dev")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "github-app-client-id")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "github-app-client-secret")
    monkeypatch.setenv(
        "GITHUB_APP_USER_CALLBACK_URL",
        "http://testserver/auth/github/install/verify",
    )

    from yinshi.config import get_settings

    get_settings.cache_clear()


def test_install_callback_requires_user_authorization_before_storage(
    tmp_path,
    monkeypatch,
) -> None:
    """An installation ID alone must not create a user binding."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)
    _configure_github_app_settings(tmp_path, monkeypatch)

    from yinshi.db import init_control_db

    init_control_db()

    from yinshi.api.auth_routes import _create_github_install_state
    from yinshi.auth import create_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="github-install-security-user",
        email="install-security@example.com",
        display_name="Install Security User",
    )
    state = _create_github_install_state(tenant.user_id)

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.get_installation_details",
            new=AsyncMock(
                return_value={
                    "account": {"login": "acme", "type": "Organization"},
                    "html_url": "https://github.com/organizations/acme/settings/installations/42",
                    "suspended_at": None,
                }
            ),
        ),
    ):
        client.cookies.set("yinshi_session", create_session_token(tenant.user_id))
        response = client.get(
            f"/auth/github/install/callback?state={state}&installation_id=42&setup_action=install",
            follow_redirects=False,
        )

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=github-app-client-id" in location
    assert "state=" in location
    with get_control_db() as db:
        installation = db.execute(
            "SELECT installation_id FROM github_installations WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
    assert installation is None
