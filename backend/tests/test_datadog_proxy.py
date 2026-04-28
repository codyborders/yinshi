"""Tests for the Datadog browser-intake reverse proxy.

The proxy lets our frontend send RUM and session-replay data through our own
origin so DNS-level blockers (NextDNS, Pi-hole, etc.) that intercept
`browser-intake-datadoghq.com` do not break telemetry with cert errors. These
tests use httpx's MockTransport to stub the upstream Datadog endpoint so we
never hit the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from yinshi.api import datadog_proxy


@contextmanager
def _patched_upstream(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> Iterator[list[httpx.Request]]:
    """Replace httpx.AsyncClient used by the proxy with a mock transport."""
    captured_requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return handler(request)

    mock_transport = httpx.MockTransport(recording_handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = mock_transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(datadog_proxy.httpx, "AsyncClient", make_client)
    yield captured_requests


def test_proxy_forwards_rum_payload(
    noauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUM batch is forwarded to browser-intake with query string preserved."""
    upstream_body = b'{"ok":true}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == datadog_proxy.DATADOG_INTAKE_HOST
        assert request.url.path == "/api/v2/rum"
        assert request.url.query == b"ddsource=browser&dd-api-key=pub_token"
        assert request.content == b'{"hello":"world"}'
        return httpx.Response(202, content=upstream_body)

    with _patched_upstream(monkeypatch, handler) as captured:
        response = noauth_client.post(
            "/rum/api/v2/rum?ddsource=browser&dd-api-key=pub_token",
            content=b'{"hello":"world"}',
            headers={"Content-Type": "text/plain;charset=UTF-8"},
        )

    assert response.status_code == 202
    assert response.content == upstream_body
    assert len(captured) == 1


def test_proxy_accepts_replay_and_logs_paths(
    noauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All allow-listed intake paths are proxied."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    for intake_path in ("replay", "logs"):
        with _patched_upstream(monkeypatch, handler):
            response = noauth_client.post(
                f"/rum/api/v2/{intake_path}?dd-api-key=pub_token",
                content=b"payload",
            )
        assert response.status_code == 200


def test_proxy_rejects_unknown_intake_path(
    noauth_client: TestClient,
) -> None:
    """Unknown intake paths return 404 so the proxy cannot reach arbitrary URLs."""
    response = noauth_client.post(
        "/rum/api/v2/secrets?dd-api-key=pub_token",
        content=b"payload",
    )
    assert response.status_code == 404


def test_proxy_is_open_when_auth_enabled(
    auth_client_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RUM SDK cannot send auth cookies, so the path must be public."""
    from fastapi.testclient import TestClient as FastAPIClient

    from yinshi.main import app

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    # Bare TestClient with no session cookie and no CSRF header.
    with _patched_upstream(monkeypatch, handler):
        with FastAPIClient(app) as anonymous_client:
            response = anonymous_client.post(
                "/rum/api/v2/rum?dd-api-key=pub_token",
                content=b"payload",
            )
    assert response.status_code == 202


def test_proxy_rejects_oversized_declared_body(
    noauth_client: TestClient,
) -> None:
    """A Content-Length above the cap is rejected before buffering."""
    oversize_bytes = datadog_proxy.MAX_BODY_BYTES + 1
    response = noauth_client.post(
        "/rum/api/v2/rum?dd-api-key=pub_token",
        content=b"x",
        headers={"Content-Length": str(oversize_bytes)},
    )
    assert response.status_code == 413


def test_proxy_maps_upstream_timeout_to_504(
    noauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeouts from the upstream intake surface as gateway timeouts."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream slow", request=request)

    with _patched_upstream(monkeypatch, handler):
        response = noauth_client.post(
            "/rum/api/v2/rum?dd-api-key=pub_token",
            content=b"payload",
        )
    assert response.status_code == 504


def test_proxy_strips_cookie_and_host_headers(
    noauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App cookies must never leak to Datadog."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "cookie" not in {name.lower() for name in request.headers.keys()}
        # httpx always sets Host to the target, not the incoming request host.
        assert request.headers.get("host") == datadog_proxy.DATADOG_INTAKE_HOST
        return httpx.Response(202)

    with _patched_upstream(monkeypatch, handler):
        noauth_client.cookies.set("session", "secret-value")
        response = noauth_client.post(
            "/rum/api/v2/rum?dd-api-key=pub_token",
            content=b"payload",
        )
    assert response.status_code == 202
