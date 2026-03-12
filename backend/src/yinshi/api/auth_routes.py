"""OAuth login/callback endpoints for Google and GitHub."""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yinshi.auth import (
    SESSION_MAX_AGE,
    _resolve_tenant_from_user_id,
    create_session_token,
    oauth,
    verify_session_token,
)
from yinshi.config import get_settings
from yinshi.services.accounts import resolve_or_create_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


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


# --- Google OAuth ---


@router.get("/login/google")
async def login_google(request: Request):
    """Redirect to Google OAuth."""
    settings = get_settings()
    if not settings.google_client_id:
        return {"error": "Google OAuth not configured"}
    return await oauth.google.authorize_redirect(request, settings.google_redirect_uri)


@router.get("/callback/google")
async def callback_google(request: Request):
    """Handle Google OAuth callback."""
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse(url="/login?error=no_user_info")

    email = user_info["email"]
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id=user_info["sub"],
        email=email,
        display_name=user_info.get("name"),
        avatar_url=user_info.get("picture"),
        provider_data=dict(user_info),
    )

    response = RedirectResponse(url="/app")
    _set_session_cookie(response, tenant.user_id)
    logger.info("Google login: user=%s email=%s", tenant.user_id, email)
    return response


# --- GitHub OAuth ---


@router.get("/login/github")
async def login_github(request: Request):
    """Redirect to GitHub OAuth."""
    settings = get_settings()
    if not settings.github_client_id:
        return {"error": "GitHub OAuth not configured"}
    return await oauth.github.authorize_redirect(request, settings.github_redirect_uri)


@router.get("/callback/github")
async def callback_github(request: Request):
    """Handle GitHub OAuth callback."""
    token = await oauth.github.authorize_access_token(request)

    # GitHub doesn't include user info in the token; call the API
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        user_resp = await client.get("https://api.github.com/user", headers=headers)
        user_data = user_resp.json()

        # Get verified email
        emails_resp = await client.get(
            "https://api.github.com/user/emails", headers=headers
        )
        emails = emails_resp.json()

    # Find the primary verified email, falling back to any verified email
    email = next(
        (e["email"] for e in emails if e.get("primary") and e.get("verified")),
        None,
    )
    if not email:
        email = next(
            (e["email"] for e in emails if e.get("verified")),
            None,
        )
    if not email:
        return RedirectResponse(url="/login?error=no_verified_email")

    tenant = resolve_or_create_user(
        provider="github",
        provider_user_id=str(user_data["id"]),
        email=email,
        display_name=user_data.get("name") or user_data.get("login"),
        avatar_url=user_data.get("avatar_url"),
        provider_data=user_data,
    )

    response = RedirectResponse(url="/app")
    _set_session_cookie(response, tenant.user_id)
    logger.info("GitHub login: user=%s email=%s", tenant.user_id, email)
    return response


# --- Backward compatibility: /auth/login redirects to /auth/login/google ---


@router.get("/login")
async def login_redirect(request: Request):
    """Legacy /auth/login redirects to Google OAuth."""
    return RedirectResponse(url="/auth/login/google", status_code=307)


@router.get("/callback")
async def callback_redirect(request: Request):
    """Legacy /auth/callback redirects to Google callback."""
    return RedirectResponse(url="/auth/callback/google", status_code=307)


# --- Common endpoints ---


@router.get("/me")
async def me(request: Request):
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
async def logout():
    """Clear session cookie."""
    response = RedirectResponse(url="/")
    response.delete_cookie("yinshi_session")
    return response
