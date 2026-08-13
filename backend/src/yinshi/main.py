"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
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
    desktop_devices,
    github,
    managed_recovery_drills,
    managed_runtime,
    prompt_runs,
    repos,
    runner_relay,
    runners,
    runtime_uploads,
    sessions,
    settings,
    stream,
    terminal_channels,
    terminals,
    workspace_files,
    workspaces,
)
from yinshi.auth import AuthMiddleware, setup_oauth
from yinshi.config import Settings, get_settings, https_required
from yinshi.db import init_control_db, init_db
from yinshi.managed_recovery_drill_controller import ManagedRecoveryDrillController
from yinshi.managed_recovery_live import ManagedRecoveryLiveRunner
from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary
from yinshi.rate_limit import limiter
from yinshi.services.encrypted_uploads import EncryptedUploadManager
from yinshi.services.managed_backup_manager import ManagedBackupManager
from yinshi.services.managed_backup_store import create_managed_backup_store
from yinshi.services.managed_backups import (
    complete_managed_backup_restore,
    start_managed_backup_deletion,
    start_managed_backup_restore,
)
from yinshi.services.managed_guest_installer import (
    ManagedGuestInstaller as ConcreteManagedGuestInstaller,
)
from yinshi.services.managed_hosted_runtime import HostedManagedRuntime
from yinshi.services.managed_runtime_manager import ManagedRuntimeManager
from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciler
from yinshi.services.prompt_journal import PromptJournal
from yinshi.services.relay_process_lock import RelayProcessLock
from yinshi.services.runner_relay import runner_relay_broker
from yinshi.services.sprites import SpritesClient
from yinshi.services.terminal_journal import TerminalJournal
from yinshi.tenant import TenantContext
from yinshi.worker_auth import (
    WorkerPrincipal,
    WorkerPrincipalMiddleware,
    prepare_worker_principal_storage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

AppMode = Literal["desktop", "hosted", "worker"]

_MAX_BOOTSTRAP_SCRIPT_BYTES = 1024 * 1024
_MANAGED_RELAY_IDLE_TIMEOUT_SECONDS = 20.0
_MANAGED_REGION = "global"


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


class DesktopTenantMiddleware:
    """Inject one profile-local identity behind the desktop bootstrap boundary."""

    def __init__(self, application: ASGIApp, *, tenant: TenantContext) -> None:
        if not callable(application):
            raise TypeError("desktop tenant application must be callable")
        if not isinstance(tenant, TenantContext) or not tenant.user_id:
            raise ValueError("desktop tenant must have an identity")
        self._application = application
        self._tenant = tenant

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            state = scope.setdefault("state", {})
            state["tenant"] = self._tenant
            state["user_email"] = self._tenant.email
        await self._application(scope, receive, send)


_API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_DESKTOP_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://yinshi.io wss://yinshi.io; "
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


def _create_provider_http_client(app_settings: Settings) -> httpx.AsyncClient:
    """Create the dedicated Fly provider HTTP client."""
    return httpx.AsyncClient(
        base_url=app_settings.sprites_api_url,
        follow_redirects=False,
    )


def _create_artifact_http_client() -> httpx.AsyncClient:
    """Create the dedicated immutable artifact HTTP client."""
    return httpx.AsyncClient(follow_redirects=False)


def _read_bounded_bootstrap_script(path: Path) -> bytes:
    """Read one validated bootstrap script without unbounded allocation."""
    with path.open("rb") as script_file:
        bootstrap_script = script_file.read(_MAX_BOOTSTRAP_SCRIPT_BYTES + 1)
    if len(bootstrap_script) > _MAX_BOOTSTRAP_SCRIPT_BYTES:
        raise RuntimeError("Managed bootstrap script exceeds the 1 MiB limit")
    return bootstrap_script


async def _initialize_managed_runtime(
    app_settings: Settings,
) -> HostedManagedRuntime:
    """Build managed Fly services and close resources after partial failures."""
    bootstrap_script = await asyncio.to_thread(
        _read_bounded_bootstrap_script,
        Path(app_settings.sprites_bootstrap_script_path),
    )
    provider_http_client: httpx.AsyncClient | None = None
    artifact_http_client: httpx.AsyncClient | None = None
    manager: ManagedRuntimeManager | None = None
    try:
        provider_http_client = _create_provider_http_client(app_settings)
        artifact_http_client = _create_artifact_http_client()
        api_token = app_settings.sprites_api_token
        name_key = app_settings.sprites_name_key
        if api_token is None or name_key is None:
            raise RuntimeError("Validated managed runtime secrets are unavailable")
        provider = SpritesClient(
            api_token=api_token.get_secret_value(),
            http_client=provider_http_client,
        )
        guest_installer = ConcreteManagedGuestInstaller(
            client=provider,
            bootstrap_script=bootstrap_script,
            relay_idle_timeout_seconds=_MANAGED_RELAY_IDLE_TIMEOUT_SECONDS,
            bootstrap_timeout_seconds=app_settings.sprites_operation_stale_seconds,
            clock=asyncio.get_running_loop().time,
            sleep=asyncio.sleep,
        )
        manager = ManagedRuntimeManager(
            provider=provider,
            guest_installer=guest_installer,
            http_client=artifact_http_client,
            name_prefix=app_settings.sprites_name_prefix,
            name_key=name_key.get_secret_value(),
            artifact_url=app_settings.sprites_artifact_url,
            artifact_sha256=app_settings.sprites_artifact_sha256,
            artifact_version=app_settings.sprites_artifact_sha256,
            allowed_domains=tuple(app_settings.sprites_allowed_domains.split(",")),
            region=_MANAGED_REGION,
            control_url=app_settings.sprites_public_control_url,
            readiness_timeout_seconds=app_settings.sprites_wake_timeout_seconds,
            is_runner_connected=runner_relay_broker.is_runner_connected,
        )
        return HostedManagedRuntime(
            runtime_manager=manager,
            backup_provider=provider,
            inventory_provider=provider,
            provider_http_client=provider_http_client,
        )
    except BaseException:
        try:
            if manager is not None:
                await manager.aclose()
            elif artifact_http_client is not None:
                await artifact_http_client.aclose()
        finally:
            if provider_http_client is not None:
                await provider_http_client.aclose()
        raise


async def _attempt_shutdown_cleanup(
    cleanup: Callable[[], Awaitable[None]],
    first_error: BaseException | None,
) -> BaseException | None:
    """Run one shutdown cleanup and retain the first non-cancellation failure."""
    try:
        await cleanup()
    except asyncio.CancelledError:
        return first_error
    except BaseException as error:
        if first_error is None:
            return error
    return first_error


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown."""
    app_settings = get_settings()
    logger.info("Starting %s", app_settings.app_name)
    if not (app.state.mode == "hosted" and app_settings.managed_runtime_provider == "fly_sprites"):
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

    relay_process_lock: RelayProcessLock | None = None
    managed_runtime_manager: ManagedRuntimeManager | None = None
    managed_backup_manager: ManagedBackupManager | None = None
    managed_sprite_reconciler_task: asyncio.Task[None] | None = None
    provider_http_client: httpx.AsyncClient | None = None
    try:
        if app.state.mode == "hosted":
            relay_process_lock = RelayProcessLock(
                Path(app_settings.control_db_path).parent / "relay-process.lock"
            )
            relay_process_lock.acquire()
        if app.state.mode == "hosted" and app_settings.managed_runtime_provider == "fly_sprites":
            managed_backup_store = create_managed_backup_store(app_settings)
            try:
                await managed_backup_store.preflight()
            except Exception:
                logger.exception("managed_storage_preflight_failed")
                raise
            managed_runtime = await _initialize_managed_runtime(app_settings)
            managed_runtime_manager = managed_runtime.runtime_manager
            provider_http_client = managed_runtime.provider_http_client
            await managed_runtime_manager.reconcile_startup()
            sprites_name_key = app_settings.sprites_name_key
            if sprites_name_key is None:
                raise RuntimeError("Managed Sprite naming is unavailable")
            restore_name_prefix = f"{app_settings.sprites_name_prefix}-restore"[:30].rstrip("-")
            managed_sprite_reconciler = ManagedSpriteReconciler(
                provider=managed_runtime.inventory_provider,
                name_prefix=app_settings.sprites_name_prefix,
                restore_name_prefix=restore_name_prefix,
                restore_name_key=sprites_name_key.get_secret_value(),
                grace=timedelta(seconds=app_settings.sprites_reconcile_grace_seconds),
            )
            await managed_sprite_reconciler.reconcile_classified(raise_on_failure=True)
            backup_encryption_key = app_settings.backup_encryption_key
            sprites_name_key = app_settings.sprites_name_key
            if backup_encryption_key is None or sprites_name_key is None:
                raise RuntimeError("Managed backup encryption is unavailable")
            managed_backup_manager = ManagedBackupManager(
                provider=managed_runtime.backup_provider,
                store=managed_backup_store,
                relay=runner_relay_broker,
                runtime_service=managed_runtime_manager,
                restore_name_prefix=restore_name_prefix,
                restore_name_key=sprites_name_key.get_secret_value(),
                complete_restore=complete_managed_backup_restore,
                start_restore=start_managed_backup_restore,
                start_deletion=start_managed_backup_deletion,
                wrapping_key=bytes.fromhex(backup_encryption_key.get_secret_value()),
                object_prefix=app_settings.managed_backup_prefix,
                retention_days=app_settings.managed_backup_retention_days,
                staging_root=Path(app_settings.control_db_path).parent / "managed-backup-staging",
            )
            await managed_backup_manager.start()
            managed_sprite_reconciler_task = asyncio.create_task(
                managed_sprite_reconciler.run(
                    interval_seconds=app_settings.sprites_reconcile_interval_seconds
                )
            )
            app.state.managed_backup_store = managed_backup_store
            app.state.managed_backup_manager = managed_backup_manager
            app.state.managed_runtime_manager = managed_runtime_manager
            if app_settings.managed_recovery_drill_enabled:
                recovery_boundary = StagingManagedRecoveryBoundary(
                    runtime_manager=managed_runtime_manager,
                    backup_manager=managed_backup_manager,
                    provider=managed_runtime.backup_provider,
                    store=managed_backup_store,
                )
                await recovery_boundary.recover_retained_cleanup()
                recovery_runner = ManagedRecoveryLiveRunner(boundary=recovery_boundary)
                app.state.managed_recovery_drill_controller = ManagedRecoveryDrillController(
                    run_drill=recovery_runner.run
                )

        yield
    finally:
        cleanup_error: BaseException | None = None
        recovery_controller = getattr(app.state, "managed_recovery_drill_controller", None)
        recovery_close = getattr(recovery_controller, "aclose", None)
        if callable(recovery_close):
            cleanup_error = await _attempt_shutdown_cleanup(
                recovery_close,
                cleanup_error,
            )
        prompt_journal = getattr(app.state, "prompt_journal", None)
        if isinstance(prompt_journal, PromptJournal):
            cleanup_error = await _attempt_shutdown_cleanup(
                prompt_journal.close,
                cleanup_error,
            )
        terminal_journal = getattr(app.state, "terminal_journal", None)
        if isinstance(terminal_journal, TerminalJournal):
            cleanup_error = await _attempt_shutdown_cleanup(
                terminal_journal.close_all,
                cleanup_error,
            )
        if reaper_task is not None:

            async def stop_reaper() -> None:
                reaper_task.cancel()
                await asyncio.gather(reaper_task, return_exceptions=True)

            cleanup_error = await _attempt_shutdown_cleanup(stop_reaper, cleanup_error)
        container_manager = getattr(app.state, "container_manager", None)
        if container_manager is not None:
            cleanup_error = await _attempt_shutdown_cleanup(
                container_manager.destroy_all,
                cleanup_error,
            )
        if managed_sprite_reconciler_task is not None:

            async def stop_managed_sprite_reconciler() -> None:
                managed_sprite_reconciler_task.cancel()
                await asyncio.gather(managed_sprite_reconciler_task, return_exceptions=True)

            cleanup_error = await _attempt_shutdown_cleanup(
                stop_managed_sprite_reconciler,
                cleanup_error,
            )
        if managed_backup_manager is not None:
            cleanup_error = await _attempt_shutdown_cleanup(
                managed_backup_manager.aclose,
                cleanup_error,
            )
        if managed_runtime_manager is not None:
            cleanup_error = await _attempt_shutdown_cleanup(
                managed_runtime_manager.aclose,
                cleanup_error,
            )
        if provider_http_client is not None:
            cleanup_error = await _attempt_shutdown_cleanup(
                provider_http_client.aclose,
                cleanup_error,
            )
        if relay_process_lock is not None:

            async def release_relay_process_lock() -> None:
                relay_process_lock.release()

            cleanup_error = await _attempt_shutdown_cleanup(
                release_relay_process_lock,
                cleanup_error,
            )
        if cleanup_error is not None:
            raise cleanup_error
    logger.info("Shutdown complete")


def _configure_middleware(
    application: FastAPI,
    app_settings: Settings,
    *,
    mode: AppMode,
    worker_principal: WorkerPrincipal | None,
) -> None:
    """Attach the shared hosted middleware stack to one application instance."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI instance")
    if not isinstance(app_settings, Settings):
        raise TypeError("app_settings must be Settings")

    if mode == "worker":
        if worker_principal is None:
            raise ValueError("worker mode requires a WorkerPrincipal")
        application.add_middleware(
            RequestBodyLimitMiddleware,
            body_bytes_max=app_settings.request_body_max_bytes,
        )
        application.add_middleware(
            SecurityHeadersMiddleware,
            content_security_policy=_API_CONTENT_SECURITY_POLICY,
        )
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=app_settings.trusted_host_list,
        )
        application.add_middleware(
            WorkerPrincipalMiddleware,
            principal=worker_principal,
        )
        return

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
    if mode == "desktop":
        desktop_tenant = TenantContext(
            user_id="desktop-local",
            email="desktop-local@yinshi.invalid",
            data_dir=str(Path(app_settings.user_data_dir).resolve()),
            db_path=str(Path(app_settings.db_path).resolve()),
        )
        application.add_middleware(DesktopTenantMiddleware, tenant=desktop_tenant)
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
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


def _include_routes(
    application: FastAPI,
    app_settings: Settings,
    *,
    mode: AppMode,
) -> None:
    """Attach only the route groups allowed for one application mode."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI instance")
    if not isinstance(app_settings, Settings):
        raise TypeError("app_settings must be Settings")
    if mode not in ("desktop", "hosted", "worker"):
        raise ValueError(f"Unsupported application mode: {mode}")

    shared_execution_routers: tuple[APIRouter, ...] = (
        auth_routes.provider_router,
        settings.router,
    )
    local_execution_routers: tuple[APIRouter, ...] = (
        catalog.router,
        repos.router,
        workspaces.router,
        workspace_files.router,
        terminals.router,
        terminal_channels.router,
        sessions.router,
        prompt_runs.router,
        runtime_uploads.router,
        stream.router,
    )
    execution_routers: tuple[APIRouter, ...] = (
        *shared_execution_routers,
        *local_execution_routers,
    )
    control_routers: tuple[APIRouter, ...] = (
        auth_routes.router,
        datadog_proxy.router,
        desktop_devices.router,
        github.router,
        runner_relay.router,
        runners.router,
        managed_runtime.router,
    )
    selected_routers = list(execution_routers)
    if mode == "hosted":
        hosted_execution_routers = execution_routers
        if app_settings.managed_runtime_provider == "fly_sprites":
            hosted_execution_routers = ()
        selected_routers = [*control_routers, *hosted_execution_routers]
    if (
        mode == "hosted"
        and app_settings.managed_runtime_provider == "fly_sprites"
        and getattr(app_settings, "managed_recovery_drill_enabled", False)
        and getattr(app_settings, "deployment_environment", "local") == "staging"
        and bool(getattr(app_settings, "managed_recovery_operator_token_hash", ""))
    ):
        selected_routers.append(managed_recovery_drills.router)
    for router in selected_routers:
        application.include_router(router)


async def health() -> dict[str, str]:
    """Return process liveness without exposing runtime details."""
    return {"status": "ok"}


def create_app(
    *,
    mode: AppMode = "hosted",
    desktop_asset_dir: str | Path | None = None,
    worker_principal: WorkerPrincipal | None = None,
) -> FastAPI:
    """Build one independently configured Yinshi application mode."""
    if mode not in ("desktop", "hosted", "worker"):
        raise ValueError(f"Unsupported application mode: {mode}")
    if desktop_asset_dir is not None and mode != "desktop":
        raise ValueError("desktop_asset_dir is only valid in desktop mode")
    if mode == "worker" and worker_principal is None:
        raise ValueError("worker mode requires a WorkerPrincipal")
    if mode != "worker" and worker_principal is not None:
        raise ValueError("worker_principal is only valid in worker mode")
    if worker_principal is not None:
        prepare_worker_principal_storage(worker_principal)
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
    application.state.encrypted_upload_manager = EncryptedUploadManager()
    application.state.managed_backup_manager = None
    application.state.managed_backup_store = None
    application.state.managed_runtime_manager = None
    application.state.managed_recovery_drill_controller = None
    application.state.managed_recovery_operator_token_hash = (
        app_settings.managed_recovery_operator_token_hash
    )
    application.state.sprites_public_launch_enabled = False
    application.state.prompt_journal = PromptJournal()
    application.state.terminal_journal = TerminalJournal()
    application.add_exception_handler(
        RateLimitExceeded,
        cast(Any, _rate_limit_exceeded_handler),
    )
    _configure_middleware(
        application,
        app_settings,
        mode=mode,
        worker_principal=worker_principal,
    )
    _include_routes(application, app_settings, mode=mode)
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
