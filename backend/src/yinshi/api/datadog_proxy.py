"""Proxy Datadog browser RUM/session-replay intake through our own domain.

Purpose: Some DNS-level blockers (e.g. NextDNS, Pi-hole) and ad-blockers target
`browser-intake-datadoghq.com` directly, returning a blockpage certificate that
breaks the browser SDK with `ERR_CERT_AUTHORITY_INVALID`. Proxying the intake
through our own origin keeps telemetry working without exposing the Datadog
hostname to clients.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from yinshi.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["datadog-proxy"])

# Site-specific intake host. This proxy pins to US1 (`datadoghq.com`) because
# that is what `frontend/src/main.tsx` configures. If the frontend site ever
# changes, update this constant in the same commit.
DATADOG_INTAKE_HOST = "browser-intake-datadoghq.com"

# Only forward to well-known intake endpoints so the proxy cannot be abused as
# an open redirect to arbitrary Datadog paths.
ALLOWED_INTAKE_PATHS = frozenset({"rum", "replay", "logs"})

# Upper bound on request body size. RUM batches are small; session replay
# segments can be larger but rarely exceed a few hundred KB.
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MiB

# Connect quickly but allow slower uploads for replay segments on poor links.
INTAKE_TIMEOUT_S = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
_INTAKE_CONCURRENCY_MAX = 16
_INTAKE_SLOT_WAIT_TIMEOUT_S = 0.1
_intake_concurrency = asyncio.Semaphore(_INTAKE_CONCURRENCY_MAX)

# Hop-by-hop and transport-specific headers that must not be relayed to the
# caller. See RFC 7230 section 6.1; `content-encoding`/`content-length` are
# dropped because the downstream framework re-computes them for our response.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _validate_intake_path(intake_path: str) -> str:
    """Return the validated intake path or raise 404."""
    if intake_path not in ALLOWED_INTAKE_PATHS:
        raise HTTPException(status_code=404, detail="Unknown intake path")
    return intake_path


@asynccontextmanager
async def _intake_concurrency_slot() -> AsyncIterator[None]:
    """Bound concurrent body reads and upstream requests across this process."""
    try:
        await asyncio.wait_for(
            _intake_concurrency.acquire(),
            timeout=_INTAKE_SLOT_WAIT_TIMEOUT_S,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Intake concurrency limit reached",
            headers={"Retry-After": "1"},
        ) from None
    try:
        yield
    finally:
        _intake_concurrency.release()


async def _intake_concurrency_dependency() -> AsyncIterator[None]:
    """Hold one global intake slot for the duration of a proxy request."""
    async with _intake_concurrency_slot():
        yield


async def _read_bounded_body(request: Request) -> bytes:
    """Read request chunks while enforcing the intake memory boundary."""
    if not isinstance(request, Request):
        raise TypeError("request must be a Request")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Intake payload too large")
        body.extend(chunk)
    return bytes(body)


@router.post("/rum/api/v2/{intake_path}")
@limiter.limit("120/minute")
async def proxy_datadog_intake(
    intake_path: str,
    request: Request,
    _concurrency_slot: None = Depends(_intake_concurrency_dependency),
) -> Response:
    """Forward a browser SDK intake POST to Datadog and relay the response."""
    validated_intake_path = _validate_intake_path(intake_path)

    # Enforce a hard upper bound before buffering the body in memory.
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            content_length_bytes = int(content_length_header)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
        if content_length_bytes < 0:
            raise HTTPException(status_code=400, detail="Negative Content-Length")
        if content_length_bytes > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Intake payload too large")

    body_bytes = await _read_bounded_body(request)

    # Preserve the original query string -- the SDK signs batches with
    # per-request parameters (e.g. `dd-api-key`, `dd-request-id`, `ddsource`).
    query_string = request.url.query

    target_url = f"https://{DATADOG_INTAKE_HOST}/api/v2/{validated_intake_path}"
    if query_string:
        target_url = f"{target_url}?{query_string}"

    # Only forward headers that Datadog actually uses. In particular, drop
    # `Host`, `Cookie`, and auth headers so we never leak app state upstream.
    content_type_header = request.headers.get("content-type", "text/plain;charset=UTF-8")
    forwarded_headers = {
        "Content-Type": content_type_header,
        "User-Agent": request.headers.get("user-agent", "yinshi-rum-proxy"),
    }

    try:
        async with httpx.AsyncClient(timeout=INTAKE_TIMEOUT_S) as client:
            upstream_response = await client.post(
                target_url,
                content=body_bytes,
                headers=forwarded_headers,
            )
    except httpx.TimeoutException:
        logger.warning("Datadog intake request timed out")
        raise HTTPException(status_code=504, detail="Intake timeout") from None
    except httpx.HTTPError as error:
        logger.warning("Datadog intake transport error")
        raise HTTPException(status_code=502, detail="Intake unreachable") from error

    # Strip hop-by-hop and transport-specific headers before relaying.
    relayed_headers = {
        name: value
        for name, value in upstream_response.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=relayed_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
