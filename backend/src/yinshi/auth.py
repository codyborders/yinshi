"""Google OAuth authentication and session middleware."""

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import Request, Response
from itsdangerous import URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

from yinshi.config import get_settings

logger = logging.getLogger(__name__)

oauth = OAuth()

SESSION_MAX_AGE = 86400 * 30  # 30 days


def setup_oauth() -> None:
    """Register Google OAuth provider."""
    settings = get_settings()
    if not settings.google_client_id:
        logger.warning("Google OAuth not configured -- auth disabled")
        return
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def create_session_token(email: str) -> str:
    """Create a signed session token."""
    settings = get_settings()
    serializer = URLSafeTimedSerializer(settings.secret_key)
    return serializer.dumps(email, salt="yinshi-session")


def verify_session_token(token: str) -> str | None:
    """Verify and decode a session token. Returns email or None."""
    settings = get_settings()
    serializer = URLSafeTimedSerializer(settings.secret_key)
    try:
        return serializer.loads(token, salt="yinshi-session", max_age=SESSION_MAX_AGE)
    except Exception:
        return None


def _auth_disabled() -> bool:
    """Check if authentication is disabled."""
    settings = get_settings()
    return settings.disable_auth or not settings.google_client_id


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that checks for valid session cookie on protected routes."""

    OPEN_PATHS = {"/auth/login", "/auth/callback", "/health"}

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path

        # Skip auth if explicitly disabled
        if _auth_disabled():
            return await call_next(request)

        # Allow open paths
        if path in self.OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)

        # Allow WebSocket upgrade (auth checked in WS handler itself)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Check session cookie
        token = request.cookies.get("yinshi_session")
        if not token:
            return Response(status_code=401, content="Not authenticated")

        email = verify_session_token(token)
        if not email:
            return Response(status_code=401, content="Invalid session")

        request.state.user_email = email
        return await call_next(request)
