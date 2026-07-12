"""Connection-scoped authentication for the in-process restricted worker app."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from yinshi.tenant import TenantContext


@dataclass(frozen=True, slots=True)
class WorkerPrincipal:
    """One tenant and high-entropy bearer accepted by a worker application."""

    tenant: TenantContext
    bearer_token: str
    database_root: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, TenantContext):
            raise TypeError("worker tenant must be TenantContext")
        if not self.tenant.user_id or not self.tenant.email:
            raise ValueError("worker tenant identity must not be empty")
        data_directory = Path(self.tenant.data_dir)
        database_path = Path(self.tenant.db_path)
        if not data_directory.is_absolute() or not database_path.is_absolute():
            raise ValueError("worker tenant paths must be absolute")
        if database_path.name != "yinshi.db":
            raise ValueError("worker tenant database filename is invalid")
        if self.database_root is None:
            if database_path.parent != data_directory:
                raise ValueError("worker tenant database must live directly in its data directory")
        else:
            database_root = Path(self.database_root)
            if not database_root.is_absolute():
                raise ValueError("worker database root must be absolute")
            try:
                database_path.relative_to(database_root)
            except ValueError as exc:
                raise ValueError("worker tenant database must stay under database root") from exc
        if not isinstance(self.bearer_token, str):
            raise TypeError("worker bearer token must be a string")
        if not 32 <= len(self.bearer_token) <= 256:
            raise ValueError("worker bearer token must contain at least 32 characters")
        if not self.bearer_token.isascii() or any(
            character.isspace() for character in self.bearer_token
        ):
            raise ValueError("worker bearer token must be non-whitespace ASCII")


def prepare_worker_principal_storage(principal: WorkerPrincipal) -> None:
    """Create and validate the worker tenant's owner-only data directory."""
    if not isinstance(principal, WorkerPrincipal):
        raise TypeError("principal must be WorkerPrincipal")
    storage_directories = {Path(principal.tenant.data_dir)}
    if principal.database_root is not None:
        storage_directories.add(Path(principal.tenant.db_path).parent)
    for storage_directory in storage_directories:
        storage_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = storage_directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or storage_directory.is_symlink():
            raise RuntimeError("worker tenant storage must be a real directory")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError("worker tenant storage must be owned by the worker user")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("worker tenant storage must have owner-only permissions")


class WorkerPrincipalMiddleware:
    """Authenticate every worker HTTP or WebSocket scope and inject its tenant."""

    def __init__(self, app: ASGIApp, *, principal: WorkerPrincipal) -> None:
        if not callable(app):
            raise TypeError("app must be an ASGI application")
        if not isinstance(principal, WorkerPrincipal):
            raise TypeError("principal must be WorkerPrincipal")
        self._app = app
        self._principal = principal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        bearer_token = self._bearer_token(scope)
        if bearer_token is None or not secrets.compare_digest(
            bearer_token,
            self._principal.bearer_token,
        ):
            await self._reject(scope, send)
            return
        state = scope.setdefault("state", {})
        state["tenant"] = self._principal.tenant
        state["user_email"] = self._principal.tenant.email
        await self._app(scope, receive, send)

    @staticmethod
    def _bearer_token(scope: Scope) -> str | None:
        """Return one exact Authorization bearer, rejecting duplicates and invalid text."""
        authorization_values = [
            value for name, value in scope.get("headers", []) if name.lower() == b"authorization"
        ]
        if len(authorization_values) != 1:
            return None
        raw_authorization = authorization_values[0]
        if not isinstance(raw_authorization, bytes):
            return None
        try:
            authorization = raw_authorization.decode("ascii")
        except UnicodeDecodeError:
            return None
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or separator != " " or not token:
            return None
        if token != token.strip() or " " in token:
            return None
        return token

    @staticmethod
    async def _reject(scope: Scope, send: Send) -> None:
        """Return a minimal protocol-appropriate authentication failure."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
