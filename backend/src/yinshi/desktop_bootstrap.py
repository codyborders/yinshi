"""One-time inherited-capability bootstrap for the loopback desktop helper."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from http.cookies import CookieError, SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BOOTSTRAP_HEADER = b"x-yinshi-bootstrap"
_BOOTSTRAP_PATH = "/desktop/bootstrap"
_SESSION_COOKIE = "yinshi_desktop_session"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _hash_token(token: str) -> bytes:
    """Hash a validated capability before retaining it in helper memory."""
    if not isinstance(token, str):
        raise TypeError("token must be a string")
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("token must be a 32-128 character base64url value")
    return hashlib.sha256(token.encode("ascii")).digest()


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    """Return every exact ASGI header value for duplicate rejection."""
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return []
    return [value for header_name, value in headers if header_name.lower() == name]


def _cookie_token(scope: Scope) -> str | None:
    """Parse one host-only desktop session cookie without accepting duplicates."""
    cookie_values = _header_values(scope, b"cookie")
    if not cookie_values:
        return None
    try:
        cookie_text = b"; ".join(cookie_values).decode("ascii")
        cookies = SimpleCookie(cookie_text)
    except (CookieError, UnicodeDecodeError):
        return None
    morsel = cookies.get(_SESSION_COOKIE)
    if morsel is None or _TOKEN_PATTERN.fullmatch(morsel.value) is None:
        return None
    return morsel.value


async def _send_http_response(
    send: Send,
    *,
    status_code: int,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Send one empty, non-cacheable bootstrap boundary response."""
    response_headers = [(b"cache-control", b"no-store")]
    if headers is not None:
        response_headers.extend(headers)
    response_start: Message = {
        "type": "http.response.start",
        "status": status_code,
        "headers": response_headers,
    }
    response_body: Message = {"type": "http.response.body", "body": b""}
    await send(response_start)
    await send(response_body)


class DesktopBootstrapMiddleware:
    """Exchange one inherited nonce for a random HttpOnly helper session."""

    def __init__(self, application: ASGIApp, *, instance_nonce: str) -> None:
        if not callable(application):
            raise TypeError("application must be callable")
        self._application = application
        self._nonce_hash = _hash_token(instance_nonce)
        self._session_hash: bytes | None = None
        self._nonce_consumed = False
        self._lock = asyncio.Lock()

    def _session_is_valid(self, scope: Scope) -> bool:
        """Compare a presented session cookie to the in-memory session hash."""
        if self._session_hash is None:
            return False
        token = _cookie_token(scope)
        if token is None:
            return False
        return hmac.compare_digest(_hash_token(token), self._session_hash)

    async def _bootstrap(self, scope: Scope, send: Send) -> None:
        """Consume one exact nonce and return a fresh browser session cookie."""
        if scope.get("method") != "POST":
            await _send_http_response(send, status_code=405)
            return
        nonce_values = _header_values(scope, _BOOTSTRAP_HEADER)
        if len(nonce_values) != 1:
            await _send_http_response(send, status_code=403)
            return
        try:
            nonce = nonce_values[0].decode("ascii")
            nonce_hash = _hash_token(nonce)
        except (UnicodeDecodeError, ValueError):
            await _send_http_response(send, status_code=403)
            return

        async with self._lock:
            if self._nonce_consumed:
                await _send_http_response(send, status_code=409)
                return
            if not hmac.compare_digest(nonce_hash, self._nonce_hash):
                await _send_http_response(send, status_code=403)
                return
            session_token = secrets.token_urlsafe(32)
            self._session_hash = _hash_token(session_token)
            self._nonce_consumed = True

        cookie = (f"{_SESSION_COOKIE}={session_token}; HttpOnly; Path=/; SameSite=Strict").encode(
            "ascii"
        )
        await _send_http_response(
            send,
            status_code=204,
            headers=[(b"set-cookie", cookie)],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Protect every helper HTTP and WebSocket route after bootstrap."""
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            await self._application(scope, receive, send)
            return
        if scope_type == "http" and scope.get("path") == _BOOTSTRAP_PATH:
            await self._bootstrap(scope, send)
            return
        if self._session_is_valid(scope):
            await self._application(scope, receive, send)
            return
        if scope_type == "websocket":
            close_message: Message = {
                "type": "websocket.close",
                "code": 4401,
                "reason": "Not authenticated",
            }
            await send(close_message)
            return
        await _send_http_response(send, status_code=401)
