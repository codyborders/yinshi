"""In-process HTTP adapter for the restricted worker application contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from yinshi.tenant import TenantContext
from yinshi.worker_auth import WorkerPrincipal

_WORKER_RESPONSE_BYTES_MAX = 1_048_576
_WORKER_PATH_LENGTH_MAX = 2_048
_WORKER_METHODS = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT"})


@dataclass(frozen=True, slots=True)
class WorkerHttpResponse:
    """Bounded JSON response returned by the internal worker application."""

    status_code: int
    body: Any
    content_type: str | None


class WorkerHttpDispatcher:
    """Invoke execution routes through ASGI without opening a network listener."""

    def __init__(self, *, app: FastAPI, principal: WorkerPrincipal) -> None:
        if not isinstance(app, FastAPI):
            raise TypeError("app must be FastAPI")
        if app.state.mode != "worker":
            raise ValueError("app must use worker mode")
        if not isinstance(principal, WorkerPrincipal):
            raise TypeError("principal must be WorkerPrincipal")
        self._app = app
        self._principal = principal

    @property
    def app(self) -> FastAPI:
        """Return the worker application for runner lifecycle management."""
        return self._app

    @property
    def user_id(self) -> str:
        """Return the single tenant identity bound to this dispatcher."""
        return self._principal.tenant.user_id

    @property
    def tenant(self) -> TenantContext:
        """Return immutable account storage coordinates without bearer authority."""
        return self._principal.tenant

    @property
    def data_directory(self) -> str:
        """Return the runner-local opaque tenant directory."""
        return self._principal.tenant.data_dir

    @property
    def database_path(self) -> str:
        """Return the runner-local encrypted tenant database path."""
        return self._principal.tenant.db_path

    async def request(
        self,
        *,
        method: str,
        path: str,
        body: Any,
        query: dict[str, str] | None = None,
    ) -> WorkerHttpResponse:
        """Dispatch one normalized HTTP-like request and return bounded JSON."""
        normalized_method = self._validate_method(method)
        normalized_path = self._validate_path(path)
        if normalized_method in {"GET", "DELETE"} and body is not None:
            raise ValueError(f"{normalized_method} worker request body must be null")
        request_kwargs: dict[str, Any] = {}
        normalized_query = self._validate_query(query)
        if normalized_query:
            request_kwargs["params"] = normalized_query
        if body is not None:
            try:
                json.dumps(body, separators=(",", ":"), sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("worker request body must be JSON-serializable") from exc
            request_kwargs["json"] = body

        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        headers = {
            "Authorization": f"Bearer {self._principal.bearer_token}",
            "X-Requested-With": "XMLHttpRequest",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
        ) as client:
            response = await client.request(
                normalized_method,
                normalized_path,
                **request_kwargs,
            )
        content = response.content
        if len(content) > _WORKER_RESPONSE_BYTES_MAX:
            raise RuntimeError("worker response exceeded the size limit")
        content_type = response.headers.get("content-type")
        normalized_content_type = (
            content_type.split(";", maxsplit=1)[0].strip().lower()
            if content_type is not None
            else None
        )
        if response.status_code == 204:
            if content:
                raise RuntimeError("worker 204 response included a body")
            return WorkerHttpResponse(
                status_code=204,
                body=None,
                content_type=normalized_content_type,
            )
        if normalized_content_type != "application/json":
            raise RuntimeError("worker response must use application/json")
        try:
            response_body = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("worker response contained invalid JSON") from exc
        return WorkerHttpResponse(
            status_code=response.status_code,
            body=response_body,
            content_type=normalized_content_type,
        )

    @staticmethod
    def _validate_query(query: dict[str, str] | None) -> dict[str, str]:
        """Return bounded scalar query data for internal ASGI dispatch."""
        if query is None:
            return {}
        if not isinstance(query, dict) or len(query) > 16:
            raise ValueError("worker request query must be a bounded object")
        normalized: dict[str, str] = {}
        for key, value in query.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("worker request query key is invalid")
            if not key.replace("_", "").isalnum():
                raise ValueError("worker request query key is invalid")
            if not isinstance(value, str) or len(value) > 2_048:
                raise ValueError("worker request query value is invalid")
            normalized[key] = value
        return normalized

    @staticmethod
    def _validate_method(method: str) -> str:
        """Return one allowlisted uppercase HTTP method."""
        if not isinstance(method, str) or method not in _WORKER_METHODS:
            raise ValueError("worker request method is not allowed")
        return method

    @staticmethod
    def _validate_path(path: str) -> str:
        """Return one relative normalized application path without query data."""
        if not isinstance(path, str) or not path or len(path) > _WORKER_PATH_LENGTH_MAX:
            raise ValueError("worker request path has an invalid length")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("worker request path must be a relative application path")
        if "?" in path or "#" in path or "\\" in path or "\x00" in path:
            raise ValueError("worker request path must be normalized")
        if any(segment in {"", ".", ".."} for segment in path.split("/")[1:]):
            raise ValueError("worker request path must be normalized")
        return path
