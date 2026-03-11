"""Google OAuth login/callback endpoints."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yinshi.auth import SESSION_MAX_AGE, create_session_token, oauth
from yinshi.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth."""
    settings = get_settings()
    if not settings.google_client_id:
        return {"error": "OAuth not configured"}
    redirect_uri = settings.google_redirect_uri
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    """Handle Google OAuth callback."""
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse(url="/login?error=no_user_info")

    email = user_info["email"]
    session_token = create_session_token(email)

    settings = get_settings()
    response = RedirectResponse(url="/app")
    response.set_cookie(
        key="yinshi_session",
        value=session_token,
        httponly=True,
        secure=not settings.debug,
        max_age=SESSION_MAX_AGE,
        samesite="lax",
    )
    logger.info("User logged in: %s", email)
    return response


@router.get("/me")
async def me(request: Request):
    """Return current user info."""
    email = getattr(request.state, "user_email", None)
    if not email:
        return {"authenticated": False}
    return {"authenticated": True, "email": email}


@router.post("/logout")
async def logout():
    """Clear session cookie."""
    response = RedirectResponse(url="/")
    response.delete_cookie("yinshi_session")
    return response
