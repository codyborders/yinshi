"""OAuth login/callback endpoints for Google and GitHub."""

import logging
import sqlite3
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer

from yinshi.auth import (
    SESSION_MAX_AGE,
    _resolve_tenant_from_user_id,
    create_session_token,
    oauth,
    revoke_auth_sessions,
    verify_session_token,
)
from yinshi.config import get_settings
from yinshi.db import get_control_db
from yinshi.exceptions import GitHubAppError, GitHubInstallationUnusableError
from yinshi.rate_limit import limiter
from yinshi.services.accounts import resolve_or_create_user
from yinshi.services.github_app import get_installation_details
from yinshi.services.provider_connections import create_provider_connection
from yinshi.services.sidecar import create_sidecar_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
_GITHUB_INSTALL_STATE_MAX_AGE_S = 600


def _set_session_cookie(response: RedirectResponse, user_id: str) -> None:
    """Set the yinshi_session cookie on a response."""
    settings = get_settings()
    session_token = create_session_token(user_id)
    response.set_cookie(
        key="yinshi_session",
        value=session_token,
        httponly=True,
        secure=not settings.debug,
        max_age=SESSION_MAX_AGE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Remove the current session cookie from a response."""
    response.delete_cookie("yinshi_session", path="/")


def _github_install_state_serializer() -> URLSafeTimedSerializer:
    """Build a serializer dedicated to GitHub install state tokens."""
    settings = get_settings()
    return URLSafeTimedSerializer(settings.secret_key)


def _create_github_install_state(user_id: str) -> str:
    """Sign a user id into a GitHub install state token."""
    assert user_id, "user_id must not be empty"
    serializer = _github_install_state_serializer()
    return serializer.dumps(user_id, salt="github-install-state")


def _verify_github_install_state(state: str) -> str | None:
    """Verify a GitHub install state token and return the user id."""
    assert state, "state must not be empty"
    serializer = _github_install_state_serializer()
    try:
        user_id = serializer.loads(
            state,
            salt="github-install-state",
            max_age=_GITHUB_INSTALL_STATE_MAX_AGE_S,
        )
    except (BadSignature, BadTimeSignature):
        return None
    if isinstance(user_id, str):
        return user_id
    return None


def _current_user_id(request: Request) -> str | None:
    """Return the authenticated user id from the session cookie."""
    token = request.cookies.get("yinshi_session")
    if not token:
        return None
    return verify_session_token(token)


# --- Google OAuth ---


@router.get("/login/google")
async def login_google(request: Request) -> Response:
    """Redirect to Google OAuth."""
    settings = get_settings()
    if not settings.google_client_id:
        return JSONResponse({"error": "Google OAuth not configured"})
    response = await oauth.google.authorize_redirect(
        request,
        settings.google_redirect_uri,
    )
    return cast(Response, response)


@router.get("/callback/google")
@limiter.limit("10/minute")
async def callback_google(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback."""
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning(
            "Google OAuth rejected: error=%s description=%s",
            exc.error,
            exc.description,
        )
        return RedirectResponse(url="/?error=oauth_error")
    except Exception as exc:
        # Catches state mismatch, missing session, and other authlib internals.
        logger.error("Google token exchange failed: %s", exc, exc_info=True)
        return RedirectResponse(url="/?error=oauth_error")

    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse(url="/?error=no_user_info")

    email = user_info["email"]

    try:
        tenant = resolve_or_create_user(
            provider="google",
            provider_user_id=user_info["sub"],
            email=email,
            display_name=user_info.get("name"),
            avatar_url=user_info.get("picture"),
            provider_data=dict(user_info),
        )
    except (sqlite3.Error, OSError) as exc:
        logger.error(
            "Account provisioning failed for email=%s: %s",
            email,
            exc,
            exc_info=True,
        )
        return RedirectResponse(url="/?error=account_error")

    response = RedirectResponse(url="/app")
    _set_session_cookie(response, tenant.user_id)
    logger.info("Google login: user=%s email=%s", tenant.user_id, email)
    return response


# --- GitHub OAuth ---


@router.get("/login/github")
async def login_github(request: Request) -> Response:
    """Redirect to GitHub OAuth."""
    settings = get_settings()
    if not settings.github_client_id:
        return JSONResponse({"error": "GitHub OAuth not configured"})
    response = await oauth.github.authorize_redirect(
        request,
        settings.github_redirect_uri,
    )
    return cast(Response, response)


@router.get("/callback/github")
@limiter.limit("10/minute")
async def callback_github(request: Request) -> RedirectResponse:
    """Handle GitHub OAuth callback."""
    try:
        token = await oauth.github.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning(
            "GitHub OAuth rejected: error=%s description=%s",
            exc.error,
            exc.description,
        )
        return RedirectResponse(url="/?error=oauth_error")
    except Exception as exc:
        logger.error("GitHub token exchange failed: %s", exc, exc_info=True)
        return RedirectResponse(url="/?error=oauth_error")

    # GitHub doesn't include user info in the token; call the API.
    try:
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {token['access_token']}"}
            user_resp = await client.get("https://api.github.com/user", headers=headers)
            user_resp.raise_for_status()
            user_data = user_resp.json()

            emails_resp = await client.get("https://api.github.com/user/emails", headers=headers)
            emails_resp.raise_for_status()
            emails = emails_resp.json()
    except (httpx.HTTPError, KeyError) as exc:
        logger.error("GitHub API call failed: %s", exc, exc_info=True)
        return RedirectResponse(url="/?error=github_api_error")

    # Find the primary verified email, falling back to any verified email.
    primary_email = next(
        (e["email"] for e in emails if e.get("primary") and e.get("verified")),
        None,
    )
    verified_email = next(
        (e["email"] for e in emails if e.get("verified")),
        None,
    )
    email = primary_email or verified_email
    if not email:
        return RedirectResponse(url="/?error=no_verified_email")

    try:
        tenant = resolve_or_create_user(
            provider="github",
            provider_user_id=str(user_data["id"]),
            email=email,
            display_name=user_data.get("name") or user_data.get("login"),
            avatar_url=user_data.get("avatar_url"),
            provider_data=user_data,
        )
    except (sqlite3.Error, OSError) as exc:
        logger.error(
            "Account provisioning failed for email=%s: %s",
            email,
            exc,
            exc_info=True,
        )
        return RedirectResponse(url="/?error=account_error")

    response = RedirectResponse(url="/app")
    _set_session_cookie(response, tenant.user_id)
    logger.info("GitHub login: user=%s email=%s", tenant.user_id, email)
    return response


@router.get("/github/install", response_model=None)
async def github_install(request: Request) -> Response:
    """Redirect an authenticated user to the GitHub App install flow."""
    settings = get_settings()
    if not settings.github_app_slug:
        return JSONResponse({"error": "GitHub App not configured"}, status_code=503)

    user_id = _current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/?error=not_authenticated")

    state = _create_github_install_state(user_id)
    query = urlencode({"state": state})
    install_url = f"https://github.com/apps/{settings.github_app_slug}/installations/new?{query}"
    return RedirectResponse(url=install_url)


@router.get("/github/install/callback")
async def github_install_callback(request: Request) -> RedirectResponse:
    """Persist a GitHub installation after the GitHub App flow returns."""
    state = request.query_params.get("state")
    if not state:
        return RedirectResponse(url="/app?github_connect_error=invalid_state")

    user_id = _verify_github_install_state(state)
    if not user_id:
        return RedirectResponse(url="/app?github_connect_error=invalid_state")

    installation_id_text = request.query_params.get("installation_id")
    if not installation_id_text:
        return RedirectResponse(url="/app?github_connect_error=missing_installation")

    try:
        installation_id = int(installation_id_text)
    except ValueError:
        return RedirectResponse(url="/app?github_connect_error=missing_installation")

    if installation_id <= 0:
        return RedirectResponse(url="/app?github_connect_error=missing_installation")

    setup_action = request.query_params.get("setup_action")
    if setup_action == "request":
        return RedirectResponse(url="/app?github_connect_error=not_granted")

    try:
        installation = await get_installation_details(installation_id)
    except GitHubInstallationUnusableError:
        return RedirectResponse(url="/app?github_connect_error=not_granted")
    except GitHubAppError:
        logger.exception("GitHub App callback failed for installation=%s", installation_id)
        return RedirectResponse(url="/app?github_connect_error=install_failed")

    account = installation.get("account")
    if not isinstance(account, dict):
        return RedirectResponse(url="/app?github_connect_error=install_failed")

    account_login = account.get("login")
    account_type = account.get("type")
    html_url = installation.get("html_url")
    if installation.get("suspended_at"):
        return RedirectResponse(url="/app?github_connect_error=not_granted")
    if not isinstance(account_login, str):
        return RedirectResponse(url="/app?github_connect_error=install_failed")
    if not isinstance(account_type, str):
        return RedirectResponse(url="/app?github_connect_error=install_failed")
    if not isinstance(html_url, str):
        return RedirectResponse(url="/app?github_connect_error=install_failed")

    with get_control_db() as db:
        db.execute(
            """
            INSERT INTO github_installations (
                user_id, installation_id, account_login, account_type, html_url
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, installation_id) DO UPDATE SET
                account_login = excluded.account_login,
                account_type = excluded.account_type,
                html_url = excluded.html_url
            """,
            (user_id, installation_id, account_login, account_type, html_url),
        )
        db.commit()

    return RedirectResponse(url="/app?github_connected=1")


@router.post("/providers/{provider}/start")
async def start_provider_auth(provider: str, request: Request) -> dict[str, str | None]:
    """Start a provider OAuth flow through the sidecar."""
    user_id = _current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sidecar = await create_sidecar_connection()
    try:
        flow = await sidecar.start_oauth_flow(provider)
    finally:
        await sidecar.disconnect()
    return {
        "flow_id": flow["flow_id"],
        "provider": flow["provider"],
        "auth_url": flow["auth_url"],
        "instructions": flow.get("instructions"),
    }


@router.get("/providers/{provider}/callback")
async def callback_provider_auth(provider: str, flow_id: str, request: Request) -> JSONResponse:
    """Poll an OAuth flow and persist its credential when complete."""
    user_id = _current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sidecar = await create_sidecar_connection()
    try:
        flow = await sidecar.get_oauth_flow_status(flow_id)
        if flow["provider"] != provider:
            raise HTTPException(status_code=400, detail="Provider mismatch")
        status = flow["status"]
        if status in {"pending", "starting"}:
            return JSONResponse(
                status_code=202,
                content={
                    "status": status,
                    "provider": provider,
                    "flow_id": flow_id,
                    "instructions": flow.get("instructions"),
                    "progress": flow.get("progress", []),
                },
            )
        if status == "error":
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "provider": provider,
                    "flow_id": flow_id,
                    "error": flow.get("error") or "OAuth flow failed",
                },
            )
        if status != "complete":
            raise HTTPException(status_code=500, detail=f"Unexpected OAuth status: {status}")

        credentials = flow.get("credentials")
        if not isinstance(credentials, dict) or not credentials:
            raise HTTPException(status_code=500, detail="OAuth flow did not return credentials")
        create_provider_connection(
            user_id,
            provider,
            "oauth",
            credentials,
            label="",
        )
        await sidecar.clear_oauth_flow(flow_id)
        return JSONResponse(
            {
                "status": "complete",
                "provider": provider,
                "flow_id": flow_id,
            }
        )
    finally:
        await sidecar.disconnect()


# --- Backward compatibility: /auth/login redirects to /auth/login/google ---


@router.get("/login")
async def login_redirect(request: Request) -> RedirectResponse:
    """Legacy /auth/login redirects to Google OAuth."""
    return RedirectResponse(url="/auth/login/google", status_code=307)


@router.get("/callback")
async def callback_redirect(request: Request) -> RedirectResponse:
    """Legacy /auth/callback redirects to Google callback.

    Preserves query parameters (state, code, scope) from the OAuth
    provider -- dropping them causes a state mismatch error.
    """
    target = "/auth/callback/google"
    query_string = request.url.query
    if query_string:
        target = f"{target}?{query_string}"
    return RedirectResponse(url=target, status_code=307)


# --- Common endpoints ---


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """Return current user info.

    This endpoint is under /auth/ (an open path), so the middleware
    doesn't populate request.state.tenant. We manually check the cookie.
    """
    token = request.cookies.get("yinshi_session")
    if not token:
        return {"authenticated": False}

    user_id = verify_session_token(token)
    if not user_id:
        return {"authenticated": False}

    tenant = _resolve_tenant_from_user_id(user_id)
    if not tenant:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": tenant.email,
        "user_id": tenant.user_id,
    }


@router.post("/logout")
async def logout() -> RedirectResponse:
    """Clear session cookie."""
    response = RedirectResponse(url="/")
    _clear_session_cookie(response)
    return response


@router.post("/logout-all")
async def logout_all(request: Request) -> JSONResponse:
    """Revoke all auth sessions for the current user and clear the cookie."""
    user_id = _current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    revoke_auth_sessions(user_id)
    response = JSONResponse({"status": "ok"})
    _clear_session_cookie(response)
    return response
