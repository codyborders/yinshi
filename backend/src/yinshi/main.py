"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from yinshi.api import (
    auth_routes,
    catalog,
    datadog_proxy,
    github,
    repos,
    runners,
    sessions,
    settings,
    stream,
    terminals,
    workspace_files,
    workspaces,
)
from yinshi.auth import AuthMiddleware, setup_oauth
from yinshi.config import Settings, get_settings, https_required
from yinshi.db import init_control_db, init_db
from yinshi.rate_limit import limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

AppMode = Literal["desktop", "hosted", "worker"]


class RequestBodyLimitMiddleware:
    """Reject declared or streamed HTTP bodies above a process-wide limit."""

    def __init__(self, app: ASGIApp, *, body_bytes_max: int) -> None:
        if body_bytes_max <= 0:
            raise ValueError("body_bytes_max must be positive")
        self._app = app
        self._body_bytes_max = body_bytes_max

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                await Response(status_code=400)(scope, receive, send)
                return
            if declared_bytes > self._body_bytes_max:
                await Response(status_code=413)(scope, receive, send)
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._body_bytes_max:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await Response(status_code=413)(scope, receive, send)


class _RequestBodyTooLarge(Exception):
    """Signal an ASGI receive stream that crossed its configured body limit."""


_API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_DESKTOP_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach browser hardening headers to every HTTP response."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        content_security_policy: str = _API_CONTENT_SECURITY_POLICY,
    ) -> None:
        super().__init__(app)
        if not isinstance(content_security_policy, str):
            raise TypeError("content_security_policy must be a string")
        if not content_security_policy.strip():
            raise ValueError("content_security_policy must not be empty")
        self._content_security_policy = content_security_policy

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            self._content_security_policy,
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=()",
        )
        return response


class DesktopStaticFiles(StaticFiles):
    """Serve packaged assets and limit SPA fallback to known UI routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            is_spa_route = path == "app" or path.startswith("app/")
            if error.status_code != 404 or not is_spa_route:
                raise
            return await super().get_response("index.html", scope)


class TransportSecurityMiddleware(BaseHTTPMiddleware):
    """Enforce HTTPS and HSTS when production transport hardening is enabled."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        require_https: bool,
        hsts_enabled: bool,
        trusted_proxy_ips: set[str] | None = None,
    ) -> None:
        """Configure transport security behavior from validated settings."""
        super().__init__(app)
        if not isinstance(require_https, bool):
            raise TypeError("require_https must be a boolean")
        if not isinstance(hsts_enabled, bool):
            raise TypeError("hsts_enabled must be a boolean")
        if trusted_proxy_ips is not None and not isinstance(trusted_proxy_ips, set):
            raise TypeError("trusted_proxy_ips must be a set or None")
        self._require_https = require_https
        self._hsts_enabled = hsts_enabled
        self._trusted_proxy_ips = trusted_proxy_ips or set()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Redirect plaintext requests and attach HSTS to HTTPS responses."""
        request_scheme = request.url.scheme.lower()
        client_host = request.client.host.lower() if request.client is not None else ""
        forwarded_proto = request.headers.get("x-forwarded-proto")
        if forwarded_proto and client_host in self._trusted_proxy_ips:
            request_scheme = forwarded_proto.split(",", maxsplit=1)[0].strip().lower()
        if self._require_https:
            if request_scheme != "https":
                https_url = request.url.replace(scheme="https")
                return RedirectResponse(str(https_url), status_code=307)
        response = await call_next(request)
        if self._hsts_enabled:
            if request_scheme == "https":
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown."""
    app_settings = get_settings()
    logger.info("Starting %s", app_settings.app_name)
    init_db()
    init_control_db()
    setup_oauth()

    # Per-user container isolation
    reaper_task: asyncio.Task[None] | None = None
    if app_settings.container_enabled:
        from yinshi.services.container import ContainerManager

        mgr = ContainerManager(settings=app_settings)
        await mgr.initialize()
        app.state.container_manager = mgr
        reaper_task = asyncio.create_task(mgr.run_reaper())
        logger.info("Container isolation enabled (image=%s)", app_settings.container_image)
    else:
        app.state.container_manager = None

    yield

    if reaper_task:
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
    if app.state.container_manager:
        await app.state.container_manager.destroy_all()
    logger.info("Shutdown complete")


def _configure_middleware(
    application: FastAPI,
    app_settings: Settings,
    *,
    mode: AppMode,
) -> None:
    """Attach the shared hosted middleware stack to one application instance."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI instance")
    if not isinstance(app_settings, Settings):
        raise TypeError("app_settings must be Settings")

    cors_origins = [app_settings.frontend_url]
    if app_settings.debug and "http://localhost:5173" not in cors_origins:
        cors_origins.append("http://localhost:5173")

    # Middleware order: last registered = outermost = runs first.
    # CORS must remain outermost so preflight responses carry its headers.
    require_https = https_required(app_settings)
    application.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.secret_key,
        https_only=require_https,
        same_site="lax",
    )
    application.add_middleware(AuthMiddleware)
    application.add_middleware(
        TransportSecurityMiddleware,
        require_https=require_https,
        hsts_enabled=app_settings.hsts_enabled and not app_settings.debug,
        trusted_proxy_ips=app_settings.trusted_proxy_ip_set,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        body_bytes_max=app_settings.request_body_max_bytes,
    )
    content_security_policy = _API_CONTENT_SECURITY_POLICY
    if mode == "desktop":
        content_security_policy = _DESKTOP_CONTENT_SECURITY_POLICY
    application.add_middleware(
        SecurityHeadersMiddleware,
        content_security_policy=content_security_policy,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=app_settings.trusted_host_list,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )


def _include_routes(application: FastAPI, *, mode: AppMode) -> None:
    """Attach only the route groups allowed for one application mode."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI instance")
    if mode not in ("desktop", "hosted", "worker"):
        raise ValueError(f"Unsupported application mode: {mode}")

    execution_routers = (
        catalog.router,
        repos.router,
        workspaces.router,
        workspace_files.router,
        terminals.router,
        sessions.router,
        stream.router,
        settings.router,
    )
    control_routers = (
        auth_routes.router,
        datadog_proxy.router,
        github.router,
        runners.router,
    )
    selected_routers = list(execution_routers)
    if mode == "hosted":
        selected_routers = [*control_routers, *execution_routers]
    for router in selected_routers:
        application.include_router(router)


async def health() -> dict[str, str]:
    """Return process liveness without exposing runtime details."""
    return {"status": "ok"}


def create_app(
    *,
    mode: AppMode = "hosted",
    desktop_asset_dir: str | Path | None = None,
) -> FastAPI:
    """Build one independently configured Yinshi application mode."""
    if mode not in ("desktop", "hosted", "worker"):
        raise ValueError(f"Unsupported application mode: {mode}")
    if desktop_asset_dir is not None and mode != "desktop":
        raise ValueError("desktop_asset_dir is only valid in desktop mode")
    asset_directory: Path | None = None
    if desktop_asset_dir is not None:
        asset_directory = Path(desktop_asset_dir).resolve()
        if not asset_directory.is_dir() or not (asset_directory / "index.html").is_file():
            raise ValueError("desktop_asset_dir must contain index.html")

    app_settings = get_settings()
    if not isinstance(app_settings, Settings):
        raise TypeError("get_settings must return Settings")

    application = FastAPI(
        title="Yinshi",
        lifespan=lifespan,
        docs_url="/docs" if app_settings.debug else None,
        openapi_url="/openapi.json" if app_settings.debug else None,
    )
    application.state.limiter = limiter
    application.state.mode = mode
    application.add_exception_handler(
        RateLimitExceeded,
        cast(Any, _rate_limit_exceeded_handler),
    )
    _configure_middleware(application, app_settings, mode=mode)
    _include_routes(application, mode=mode)
    application.add_api_route("/health", health, methods=["GET"])
    if asset_directory is not None:
        application.mount(
            "/",
            DesktopStaticFiles(directory=str(asset_directory), html=True),
            name="desktop-ui",
        )
    return application


app = create_app()
app_settings = get_settings()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=app_settings.host, port=app_settings.port)
