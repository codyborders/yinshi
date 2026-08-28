"""SSE streaming endpoint for agent interaction.

Tests: test_prompt_session_not_found, test_prompt_streams_sidecar_events,
       test_prompt_saves_partial_on_sidecar_error, test_cancel_session_not_found,
       test_cancel_no_active_stream in tests/test_api.py
"""

import asyncio
import json
import logging
import sqlite3
import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from yinshi.api.deps import (
    check_owner,
    get_tenant,
    get_user_email,
    run_db_operation_for_request,
)
from yinshi.auth import get_session_identity
from yinshi.config import get_settings
from yinshi.exceptions import (
    ContainerNotReadyError,
    ContainerStartError,
    GitError,
    KeyNotFoundError,
    RepoNotFoundError,
    SidecarError,
    WorkspaceNotFoundError,
)
from yinshi.model_catalog import get_provider_metadata, normalize_model_ref
from yinshi.rate_limit import limiter
from yinshi.services.container import (
    ContainerActivityReservation,
    ContainerManager,
)
from yinshi.services.desktop_devices import desktop_device_is_active
from yinshi.services.git_runtime import resolve_git_runtime_auth
from yinshi.services.keys import record_usage
from yinshi.services.live_auth_sessions import (
    LiveAuthSessionRegistration,
    register_live_auth_session,
    register_live_desktop_device,
)
from yinshi.services.provider_connections import (
    resolve_provider_connection,
    update_provider_connection_secret,
)
from yinshi.services.repository_lifecycle import (
    repository_lifecycle,
    repository_lifecycle_root,
)
from yinshi.services.run_coordinator import CancelOutcome, get_run_coordinator
from yinshi.services.sidecar import SidecarClient, create_sidecar_connection
from yinshi.services.sidecar_runtime import (
    local_pi_session_file,
    remap_path_for_container,
    resolve_tenant_sidecar_context,
    touch_tenant_container,
)
from yinshi.services.workspace import (
    WorkspaceCheckoutState,
    apply_workspace_checkout_preparation,
    load_workspace_checkout_state,
    prepare_workspace_checkout_for_tenant,
)
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
router = APIRouter()

# Batch DB writes every N chunks to reduce I/O
_PERSIST_BATCH_SIZE = 10
_STORED_TURN_SCHEMA = "yinshi.assistant_turn.v1"
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]

_AUTH_SESSION_RECHECK_INTERVAL_S = 1.0
_STREAM_LIFETIME_S_MAX = 2 * 60 * 60
_THINKING_LEVEL_DEFAULT: ThinkingLevel = "medium"
_THINKING_LEVEL_OFF: ThinkingLevel = "off"
_THINKING_LEVEL_ORDER: tuple[ThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
_THINKING_LEVELS = frozenset(_THINKING_LEVEL_ORDER)
_STANDARD_THINKING_LEVELS = _THINKING_LEVEL_ORDER[:-1]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Resolved sidecar execution inputs for a single prompt request."""

    sidecar_socket: str | None
    effective_cwd: str
    key_source: str
    provider: str
    provider_auth: dict[str, object] | None
    provider_config: dict[str, object] | None
    git_auth: dict[str, object] | None = None
    agent_dir: str | None = None
    settings_payload: dict[str, object] | None = None
    model_ref: str = ""
    runtime_id: str | None = None
    pi_session_file: str | None = None


@dataclass(frozen=True, slots=True)
class _ContainerActivity:
    """Bind one reservation to the manager that issued it."""

    manager: ContainerManager
    reservation: ContainerActivityReservation


async def _acquire_tenant_container_activity(
    request: Request,
    tenant: Any,
    *,
    runtime_id: str | None,
    required: bool,
) -> _ContainerActivity | None:
    """Acquire the current tenant runtime before any sidecar operation."""
    if tenant is None:
        return None
    manager = cast(
        ContainerManager | None,
        getattr(request.app.state, "container_manager", None),
    )
    if manager is None:
        if required:
            raise ContainerNotReadyError("Container manager is not initialized")
        return None
    reservation = await manager.acquire_activity(
        tenant.user_id,
        runtime_id=runtime_id,
    )
    if reservation is None:
        if required:
            raise ContainerNotReadyError("Container runtime is no longer available")
        return None
    return _ContainerActivity(manager, reservation)


async def _release_tenant_container_activity(
    activity: _ContainerActivity | None,
) -> None:
    """Release the exact container reservation acquired by this caller."""
    if activity is not None:
        await activity.manager.release_activity(activity.reservation)


class _AuthSessionRevoked(Exception):
    """Signal that an active stream's originating auth session was revoked."""


class _StreamLifetimeReached(Exception):
    """Signal that a response reached its independent connection deadline."""


async def _authority_bound_events(
    *,
    events: AsyncIterator[dict[str, Any]],
    sidecar: SidecarClient,
    session_id: str,
    registration: LiveAuthSessionRegistration,
    authority_is_active: Callable[[], bool],
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield sidecar events while one durable authority remains active."""
    if not session_id:
        raise ValueError("session_id must not be empty")
    if not authority_is_active():
        registration.close()
        await sidecar.cancel(session_id)
        raise _AuthSessionRevoked

    loop = asyncio.get_running_loop()
    connection_deadline = loop.time() + _STREAM_LIFETIME_S_MAX
    event_iterator = aiter(events)
    next_event_task: asyncio.Future[dict[str, Any]] | None = None
    revocation_task = asyncio.create_task(registration.event.wait())
    try:
        while True:
            if loop.time() >= connection_deadline:
                await sidecar.cancel(session_id)
                raise _StreamLifetimeReached
            next_event_task = asyncio.ensure_future(anext(event_iterator))
            while not next_event_task.done():
                remaining_lifetime_s = connection_deadline - loop.time()
                if remaining_lifetime_s <= 0:
                    next_event_task.cancel()
                    await sidecar.cancel(session_id)
                    raise _StreamLifetimeReached
                done, _ = await asyncio.wait(
                    {next_event_task, revocation_task},
                    timeout=min(_AUTH_SESSION_RECHECK_INTERVAL_S, remaining_lifetime_s),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if revocation_task in done:
                    next_event_task.cancel()
                    await sidecar.cancel(session_id)
                    raise _AuthSessionRevoked
                if next_event_task in done:
                    break
                if loop.time() >= connection_deadline:
                    next_event_task.cancel()
                    await sidecar.cancel(session_id)
                    raise _StreamLifetimeReached
                if not authority_is_active():
                    next_event_task.cancel()
                    await sidecar.cancel(session_id)
                    raise _AuthSessionRevoked
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                return
            finally:
                next_event_task = None
            if registration.event.is_set() or not authority_is_active():
                await sidecar.cancel(session_id)
                raise _AuthSessionRevoked
            if loop.time() >= connection_deadline:
                await sidecar.cancel(session_id)
                raise _StreamLifetimeReached
            yield event
    finally:
        registration.close()
        revocation_task.cancel()
        if next_event_task is not None and not next_event_task.done():
            next_event_task.cancel()


async def _session_bound_events(
    events: AsyncIterator[dict[str, Any]],
    session_token: str,
    sidecar: SidecarClient,
    session_id: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield sidecar events until browser-session revocation occurs."""
    if not session_token:
        raise ValueError("session_token must not be empty")
    identity = get_session_identity(session_token)
    if identity is None:
        await sidecar.cancel(session_id)
        raise _AuthSessionRevoked
    user_id, auth_session_id = identity
    registration = register_live_auth_session(
        user_id=user_id,
        auth_session_id=auth_session_id,
    )
    async for event in _authority_bound_events(
        events=events,
        sidecar=sidecar,
        session_id=session_id,
        registration=registration,
        authority_is_active=lambda: get_session_identity(session_token) is not None,
    ):
        yield event


async def _desktop_device_bound_events(
    events: AsyncIterator[dict[str, Any]],
    *,
    user_id: str,
    device_id: str,
    sidecar: SidecarClient,
    session_id: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield sidecar events until desktop-device revocation occurs."""
    registration = register_live_desktop_device(
        user_id=user_id,
        device_id=device_id,
    )
    async for event in _authority_bound_events(
        events=events,
        sidecar=sidecar,
        session_id=session_id,
        registration=registration,
        authority_is_active=lambda: desktop_device_is_active(
            user_id=user_id,
            device_id=device_id,
        ),
    ):
        yield event


class PromptRequest(BaseModel):
    prompt: str = Field(..., max_length=100_000)
    model: str | None = None
    thinking: ThinkingLevel | None = None

    @field_validator("thinking", mode="before")
    @classmethod
    def validate_thinking(cls, value: object) -> str | None:
        """Normalize legacy booleans and explicit thinking levels."""
        if value is None:
            return None
        if isinstance(value, bool):
            return _THINKING_LEVEL_DEFAULT if value else _THINKING_LEVEL_OFF
        if not isinstance(value, str):
            raise ValueError("thinking must be a valid thinking level")
        normalized_value = value.strip().lower()
        if normalized_value in _THINKING_LEVELS:
            return normalized_value
        valid_levels = ", ".join(_THINKING_LEVEL_ORDER)
        raise ValueError(f"thinking must be one of {valid_levels}")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        """Normalize optional model values into canonical refs."""
        if value is None:
            return None
        return normalize_model_ref(value)


def _catalog_model_thinking_levels(
    model_payload: dict[str, Any],
) -> tuple[ThinkingLevel, ...] | None:
    """Return model-specific thinking levels from one catalog row."""
    raw_levels = model_payload.get("thinking_levels")
    if isinstance(raw_levels, list):
        thinking_levels: list[ThinkingLevel] = []
        for raw_level in raw_levels:
            if raw_level not in _THINKING_LEVELS:
                continue
            level = cast(ThinkingLevel, raw_level)
            if level not in thinking_levels:
                thinking_levels.append(level)
        if thinking_levels:
            if _THINKING_LEVEL_OFF not in thinking_levels:
                thinking_levels.insert(0, _THINKING_LEVEL_OFF)
            return tuple(thinking_levels)

    reasoning_value = model_payload.get("reasoning")
    if isinstance(reasoning_value, bool):
        if reasoning_value:
            return _STANDARD_THINKING_LEVELS
        return (_THINKING_LEVEL_OFF,)
    return None


def _catalog_thinking_levels(
    catalog_payload: dict[str, Any],
    model_ref: str,
) -> tuple[ThinkingLevel, ...] | None:
    """Return available thinking levels for one catalog model."""
    if not isinstance(catalog_payload, dict):
        raise TypeError("catalog_payload must be a dictionary")
    if not isinstance(model_ref, str):
        raise TypeError("model_ref must be a string")
    normalized_model_ref = model_ref.strip()
    if not normalized_model_ref:
        raise ValueError("model_ref must not be empty")

    models_payload = catalog_payload.get("models")
    if not isinstance(models_payload, list):
        return None

    for model_payload in models_payload:
        if not isinstance(model_payload, dict):
            continue
        if model_payload.get("ref") != normalized_model_ref:
            continue

        thinking_levels = _catalog_model_thinking_levels(model_payload)
        if thinking_levels is None:
            logger.warning(
                "Catalog thinking metadata missing for model %s",
                normalized_model_ref,
            )
        return thinking_levels

    logger.warning("Requested catalog entry is unavailable")
    return None


def _clamp_thinking_level(
    requested_level: ThinkingLevel,
    available_levels: tuple[ThinkingLevel, ...],
) -> ThinkingLevel:
    """Clamp one requested thinking level to model-supported levels."""
    if requested_level in available_levels:
        return requested_level
    available_level_set = set(available_levels)
    requested_index = _THINKING_LEVEL_ORDER.index(requested_level)

    for candidate in _THINKING_LEVEL_ORDER[requested_index:]:
        if candidate in available_level_set:
            return candidate
    for candidate in reversed(_THINKING_LEVEL_ORDER[:requested_index]):
        if candidate in available_level_set:
            return candidate
    return available_levels[0] if available_levels else _THINKING_LEVEL_OFF


def _build_effective_settings(
    settings_payload: dict[str, object] | None,
    thinking_override: ThinkingLevel | None,
    available_levels: tuple[ThinkingLevel, ...] | None,
) -> dict[str, object] | None:
    """Merge one prompt-scoped thinking level into Pi-compatible settings."""
    if thinking_override is None:
        return settings_payload
    if available_levels == (_THINKING_LEVEL_OFF,):
        return settings_payload

    effective_settings = dict(settings_payload or {})
    effective_level = thinking_override
    if available_levels is not None:
        effective_level = _clamp_thinking_level(thinking_override, available_levels)
    effective_settings["defaultThinkingLevel"] = effective_level
    return effective_settings


def _stored_turn_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return one JSON-safe turn event for persisted assistant history."""
    if not isinstance(event, dict):
        raise TypeError("event must be a dictionary")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise ValueError("event type must be a string")
    safe_event = json.loads(json.dumps(event))
    assert isinstance(safe_event, dict), "serialized event must remain an object"
    return cast(dict[str, Any], safe_event)


def _serialize_stored_turn(events: list[dict[str, Any]]) -> str | None:
    """Serialize turn events using an explicit replay schema."""
    if not events:
        return None
    payload = {
        "schema": _STORED_TURN_SCHEMA,
        "events": events,
    }
    return json.dumps(payload)


_FILLER_PREFIXES = [
    "please ",
    "can you ",
    "could you ",
    "would you ",
    "i want you to ",
    "i need you to ",
    "help me ",
    "i'd like you to ",
    "i would like you to ",
    "go ahead and ",
    "let's ",
    "we need to ",
    "we should ",
]

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "this",
        "that",
        "it",
        "its",
        "my",
        "your",
        "our",
        "their",
        "some",
        "all",
        "any",
        "so",
        "up",
        "out",
        "about",
        "into",
        "me",
        "him",
        "her",
        "us",
        "them",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "just",
        "also",
        "very",
        "really",
        "actually",
        "basically",
        "need",
        "needs",
        "want",
        "make",
        "sure",
        "there",
        "using",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "new",
        "now",
    }
)


def _summarize_prompt(prompt: str, max_words: int = 3) -> str:
    """Derive a 2-3 word workspace name from a user prompt."""
    text = prompt.strip()
    if not text:
        return ""

    lower = text.lower()
    for prefix in _FILLER_PREFIXES:
        if lower.startswith(prefix):
            text = text[len(prefix) :]
            break

    words = [w.strip(".,;:!?-\"'()[]{}") for w in text.split()]
    words = [w for w in words if w]
    significant = [w for w in words if w.lower() not in _STOP_WORDS]

    if not significant:
        collapsed_text = "-".join(text.split())
        significant = words[:max_words] if words else [collapsed_text[:30]]

    result = significant[:max_words]
    summary = "-".join(w.lower() for w in result)

    if len(summary) > 50:
        summary = summary[:50].rsplit("-", 1)[0]
    if not summary:
        summary = "-".join(text.lower().split())[:30]
    return summary


def _workspace_path_is_trusted(tenant: Any, workspace_path: str) -> bool:
    """Return whether a workspace path is inside tenant-managed storage."""
    assert workspace_path, "workspace_path must not be empty"
    if is_path_inside(workspace_path, tenant.data_dir):
        return True

    settings = get_settings()
    if settings.container_enabled:
        return False
    if settings.allowed_repo_base and is_path_inside(workspace_path, settings.allowed_repo_base):
        return True
    return False


def _validate_workspace_path(tenant: Any, workspace_path: str) -> None:
    """Reject workspace paths that are outside trusted directories."""
    if _workspace_path_is_trusted(tenant, workspace_path):
        return

    raise HTTPException(
        status_code=403,
        detail="Workspace path outside allowed directories",
    )


def _remap_path(
    host_path: str,
    data_dir: str,
    mount: str = "/data",
) -> str:
    """Translate a host workspace path to the container's mount namespace."""
    return remap_path_for_container(host_path, data_dir, mount_path=mount)


def _session_pi_context_version(session: sqlite3.Row) -> int:
    """Return durable Pi context version for one session row."""
    if "pi_context_version" not in session.keys():
        return 0
    try:
        return int(session["pi_context_version"] or 0)
    except (TypeError, ValueError):
        return 0


def _message_count_for_session(db: sqlite3.Connection, session_id: str) -> int:
    """Return stored transcript message count for one session."""
    assert session_id, "session_id must not be empty"
    row = db.execute(
        "SELECT COUNT(*) AS message_count FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None, "message count query must return one row"
    return int(row["message_count"])


def _ensure_promptable_pi_context(db: sqlite3.Connection, session: sqlite3.Row) -> None:
    """Reject legacy transcript-only sessions that cannot resume exact Pi context.

    The session row must come from the caller's current transaction snapshot so
    the context version and the transcript count cannot disagree after a
    concurrent reservation commits between the two reads. The caller owns the
    surrounding transaction and performs the commit.
    """
    session_id = session["id"]
    assert isinstance(session_id, str), "session id must be a string"
    if _session_pi_context_version(session) >= 1:
        return

    if _message_count_for_session(db, session_id) > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "legacy_pi_context",
                "message": (
                    "This session predates durable Pi context and cannot continue "
                    "with exact model context. Start a new session in this workspace."
                ),
            },
        )

    db.execute(
        "UPDATE sessions SET pi_context_version = 1 WHERE id = ?",
        (session_id,),
    )


def _lookup_session(
    db: sqlite3.Connection,
    session_id: str,
    request: Request,
) -> sqlite3.Row | None:
    """Look up a session with workspace info, including owner_email in legacy mode."""
    tenant = get_tenant(request)
    if tenant:
        row = db.execute(
            "SELECT s.*, w.path as workspace_path, w.id as workspace_id, "
            "w.name as workspace_name, w.branch as workspace_branch, "
            "r.remote_url, r.installation_id, r.agents_md, r.root_path as repo_root_path "
            "FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "JOIN repos r ON w.repo_id = r.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    row = db.execute(
        "SELECT s.*, w.path as workspace_path, w.id as workspace_id, "
        "w.name as workspace_name, w.branch as workspace_branch, "
        "r.owner_email, r.remote_url, r.installation_id, r.agents_md, r.root_path as repo_root_path "
        "FROM sessions s "
        "JOIN workspaces w ON s.workspace_id = w.id "
        "JOIN repos r ON w.repo_id = r.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


async def _resolve_execution_context(
    request: Request,
    tenant: Any,
    runtime_session_id: str,
    workspace_id: str,
    workspace_path: str,
    model: str,
    repo_root_path: str | None = None,
    remote_url: str | None = None,
    installation_id: int | None = None,
    agents_md: str | None = None,
) -> ExecutionContext:
    """Resolve all sidecar execution inputs for the current request."""
    if not tenant:
        return ExecutionContext(
            sidecar_socket=None,
            effective_cwd=workspace_path,
            key_source="platform",
            provider="",
            provider_auth=None,
            provider_config=None,
            git_auth=None,
            model_ref=model,
            runtime_id=None,
            pi_session_file=local_pi_session_file(runtime_session_id),
        )

    _validate_workspace_path(tenant, workspace_path)

    try:
        tenant_sidecar_context = await resolve_tenant_sidecar_context(
            request,
            tenant,
            runtime_session_id=runtime_session_id,
            repo_agents_md=agents_md,
            repo_root_path=repo_root_path,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
        )
    except (ContainerStartError, ContainerNotReadyError):
        logger.error("Container start failed")
        raise HTTPException(
            status_code=503,
            detail="Agent environment temporarily unavailable",
        ) from None
    sidecar_socket = tenant_sidecar_context.socket_path
    effective_cwd = workspace_path
    agent_dir = tenant_sidecar_context.agent_dir
    settings_payload = tenant_sidecar_context.settings_payload

    if sidecar_socket is not None:
        try:
            effective_cwd = _remap_path(workspace_path, tenant.data_dir)
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail="Workspace path outside allowed directories",
            ) from exc

    sidecar_tmp = None
    try:
        activity = await _acquire_tenant_container_activity(
            request,
            tenant,
            runtime_id=tenant_sidecar_context.runtime_id,
            required=sidecar_socket is not None,
        )
    except (ContainerStartError, ContainerNotReadyError):
        logger.error("Container activity acquisition failed")
        raise HTTPException(
            status_code=503,
            detail="Agent environment temporarily unavailable",
        ) from None
    try:
        sidecar_tmp = await create_sidecar_connection(sidecar_socket)
        resolved = await sidecar_tmp.resolve_model(model, agent_dir=agent_dir)
        provider: str | None = resolved["provider"]
        if not provider:
            raise HTTPException(
                status_code=400,
                detail="Could not determine provider for model",
            )
        provider_metadata = get_provider_metadata(provider)
        if not provider_metadata.supported:
            raise HTTPException(
                status_code=400,
                detail=f"Provider {provider} is not supported in Yinshi yet",
            )
        model_ref = cast(str, resolved["model"])
        connection = resolve_provider_connection(tenant.user_id, provider)
        provider_auth: dict[str, object] = {
            "provider": provider,
            "authStrategy": connection["auth_strategy"],
            "secret": cast(object, connection["secret"]),
        }
        provider_config = cast(dict[str, object], connection["config"])
        auth_resolved = await sidecar_tmp.resolve_provider_auth(
            provider=provider,
            model=model_ref,
            provider_auth=cast(dict[str, Any], provider_auth),
            provider_config=provider_config,
            agent_dir=agent_dir,
        )
        refreshed_auth = auth_resolved.get("auth")
        if refreshed_auth is not None and refreshed_auth != connection["secret"]:
            update_provider_connection_secret(
                tenant.user_id,
                connection["id"],
                connection["auth_strategy"],
                cast(str | dict[str, object], refreshed_auth),
            )
            provider_auth["secret"] = cast(object, refreshed_auth)
        resolved_model_ref = cast(str, auth_resolved.get("model_ref") or model_ref)
        resolved_provider_config = cast(
            dict[str, object] | None,
            auth_resolved.get("model_config"),
        )
        git_runtime_auth = await resolve_git_runtime_auth(
            tenant.user_id,
            remote_url,
            installation_id,
        )
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    finally:
        try:
            if sidecar_tmp is not None:
                await sidecar_tmp.disconnect()
        finally:
            await _release_tenant_container_activity(activity)

    return ExecutionContext(
        sidecar_socket=sidecar_socket,
        effective_cwd=effective_cwd,
        key_source=connection["auth_strategy"],
        provider=provider,
        provider_auth=provider_auth,
        provider_config=resolved_provider_config or provider_config,
        git_auth=None if git_runtime_auth is None else git_runtime_auth.as_sidecar_payload(),
        agent_dir=agent_dir,
        settings_payload=settings_payload,
        model_ref=resolved_model_ref,
        runtime_id=tenant_sidecar_context.runtime_id,
        pi_session_file=tenant_sidecar_context.pi_session_file,
    )


async def _prompt_database_operation(
    request: Request,
    operation: Callable[[sqlite3.Connection], _T],
    *,
    background: bool = False,
) -> _T:
    """Run prompt persistence with foreground or per-operation retry budget."""
    return await run_db_operation_for_request(
        request,
        operation,
        shared_request_budget=not background,
    )


def _release_prompt_session_if_owned(
    database: sqlite3.Connection,
    session_id: str,
    turn_id: str,
) -> None:
    """Release a session only while its latest user message owns this turn."""
    database.execute(
        """UPDATE sessions SET status = 'idle'
           WHERE id = ? AND status = 'running'
             AND ? = (
                 SELECT turn_id FROM messages
                 WHERE session_id = ? AND role = 'user'
                 ORDER BY rowid DESC LIMIT 1
             )""",
        (session_id, turn_id, session_id),
    )


async def _set_prompt_session_idle(
    request: Request,
    session_id: str,
    turn_id: str,
    *,
    background: bool = False,
) -> None:
    """Idempotently release one owned prompt session reservation."""

    def release(database: sqlite3.Connection) -> None:
        _release_prompt_session_if_owned(database, session_id, turn_id)
        database.commit()

    await _prompt_database_operation(request, release, background=background)


async def _cleanup_cancelled_prompt_reservation(
    request: Request,
    session_id: str,
    turn_id: str,
) -> None:
    """Release an owned reservation without replacing caller cancellation."""
    try:
        await _set_prompt_session_idle(request, session_id, turn_id, background=True)
    except Exception:
        logger.exception("Prompt reservation cleanup failed during cancellation")


async def _persist_assistant_turn(
    request: Request,
    *,
    message_id: str,
    session_id: str,
    turn_id: str,
    content: str,
    full_message: str | None = None,
    turn_status: str | None = None,
    finalize_session: bool = False,
) -> None:
    """Upsert one assistant turn and optionally release its session atomically."""

    def persist(database: sqlite3.Connection) -> None:
        row = database.execute(
            "SELECT session_id, role, turn_id FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            database.execute(
                """INSERT INTO messages
                   (id, session_id, role, content, full_message, turn_id, turn_status)
                   VALUES (?, ?, 'assistant', ?, ?, ?, ?)""",
                (message_id, session_id, content, full_message, turn_id, turn_status),
            )
        else:
            if row["session_id"] != session_id or row["role"] != "assistant":
                raise RuntimeError("assistant message identity conflict")
            if row["turn_id"] != turn_id:
                raise RuntimeError("assistant message turn conflict")
            database.execute(
                """UPDATE messages SET content = ?, full_message = COALESCE(?, full_message),
                   turn_status = COALESCE(?, turn_status) WHERE id = ?""",
                (content, full_message, turn_status, message_id),
            )
        if finalize_session:
            _release_prompt_session_if_owned(database, session_id, turn_id)
        database.commit()

    await _prompt_database_operation(request, persist, background=True)


async def _prepare_prompt_workspace_checkout(
    request: Request,
    tenant: TenantContext,
    workspace_id: str,
) -> dict[str, Any]:
    """Prepare and apply prompt checkout state under its repository lock."""

    def load_checkout(database: sqlite3.Connection) -> WorkspaceCheckoutState:
        return load_workspace_checkout_state(database, workspace_id)

    checkout_state = await _prompt_database_operation(request, load_checkout)
    lock_root = await _prompt_database_operation(
        request,
        lambda database: repository_lifecycle_root(database, tenant),
    )
    async with repository_lifecycle(checkout_state.repo_id, lock_root):
        locked_checkout_state = await _prompt_database_operation(request, load_checkout)
        if locked_checkout_state.repo_id != checkout_state.repo_id:
            raise WorkspaceNotFoundError("Workspace repository changed during preparation")
        checkout_preparation = await prepare_workspace_checkout_for_tenant(
            tenant,
            locked_checkout_state,
        )
        return await _prompt_database_operation(
            request,
            lambda database: apply_workspace_checkout_preparation(
                database,
                checkout_preparation,
            ),
        )


@router.post("/api/sessions/{session_id}/prompt")
@limiter.limit("120/hour")
async def prompt_session(
    session_id: str,
    body: PromptRequest,
    request: Request,
) -> StreamingResponse:
    """Send a prompt and stream agent events as SSE."""
    tenant = get_tenant(request)
    desktop_device_id = getattr(request.state, "desktop_device_id", None)
    if desktop_device_id is not None and (
        not isinstance(desktop_device_id, str) or not desktop_device_id
    ):
        raise RuntimeError("desktop device authority is invalid")
    requires_auth_session = (
        tenant is not None and request.app.state.mode != "worker" and desktop_device_id is None
    )
    auth_session_token = request.cookies.get("yinshi_session") if requires_auth_session else None

    def lookup_session(database: sqlite3.Connection) -> sqlite3.Row | None:
        return _lookup_session(database, session_id, request)

    session = await _prompt_database_operation(request, lookup_session)
    if session and tenant:
        workspace_id = session["workspace_id"]
        try:
            await _prepare_prompt_workspace_checkout(
                request,
                tenant,
                workspace_id,
            )
        except (GitError, RepoNotFoundError, WorkspaceNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        session = await _prompt_database_operation(request, lookup_session)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not tenant:
        check_owner(session["owner_email"], get_user_email(request))

    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Session already has an active stream")

    workspace_path = session["workspace_path"]
    remote_url = session["remote_url"] if "remote_url" in session.keys() else None
    installation_id = session["installation_id"] if "installation_id" in session.keys() else None
    model = normalize_model_ref(body.model or session["model"])
    prompt = body.prompt
    from yinshi.services.prompt_journal import get_active_prompt_run_id

    turn_id = get_active_prompt_run_id() or uuid.uuid4().hex

    # Atomically claim the session and persist one deterministic user turn.
    def reserve_prompt(database: sqlite3.Connection) -> None:
        # One immediate write transaction serializes the reservation. A
        # competing reservation blocks on the write lock instead of entering
        # the context-count decision against a snapshot it can change.
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            """SELECT content FROM messages
               WHERE session_id = ? AND role = 'user' AND turn_id = ?""",
            (session_id, turn_id),
        ).fetchone()
        status_row = database.execute(
            "SELECT id, status, pi_context_version FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if status_row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if existing is not None:
            if existing["content"] != prompt or status_row["status"] != "running":
                raise RuntimeError("prompt reservation identity conflict")
            # Close the idempotent existing-turn check without leaving the
            # immediate transaction open; there is nothing to write.
            database.commit()
            return
        # One current snapshot decides the Pi context gate and the session
        # claim, so a concurrent first prompt can never pair a stale context
        # version with a fresh transcript count.
        _ensure_promptable_pi_context(database, status_row)
        result = database.execute(
            "UPDATE sessions SET status = 'running' WHERE id = ? AND status = 'idle'",
            (session_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="Session already has an active stream")
        database.execute(
            "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
            (session_id, prompt, turn_id),
        )
        if session["workspace_name"] == session["workspace_branch"]:
            database.execute(
                "UPDATE workspaces SET name = ? WHERE id = ?",
                (_summarize_prompt(prompt), session["workspace_id"]),
            )
        database.commit()

    try:
        await _prompt_database_operation(request, reserve_prompt)
    except asyncio.CancelledError:
        await _cleanup_cancelled_prompt_reservation(request, session_id, turn_id)
        raise

    try:
        context = await _resolve_execution_context(
            request,
            tenant,
            session_id,
            session["workspace_id"],
            workspace_path,
            model,
            repo_root_path=(
                session["repo_root_path"] if "repo_root_path" in session.keys() else None
            ),
            remote_url=remote_url,
            installation_id=installation_id,
            agents_md=session["agents_md"] if "agents_md" in session.keys() else None,
        )
    except asyncio.CancelledError:
        await _cleanup_cancelled_prompt_reservation(request, session_id, turn_id)
        raise
    except Exception:
        await _set_prompt_session_idle(request, session_id, turn_id, background=True)
        raise

    logger.info(
        "Prompt received: prompt_len=%d model=%s provider=%s",
        len(prompt),
        model,
        context.provider,
    )

    async def event_stream() -> AsyncGenerator[str, None]:
        sidecar: SidecarClient | None = None
        coordinator = get_run_coordinator()
        accumulated = ""
        assistant_msg_id: str | None = None
        chunk_count = 0
        usage_data: dict[str, Any] = {}
        result_provider = context.provider or ""
        turn_status = "completed"
        turn_events: list[dict[str, Any]] = []
        activity: _ContainerActivity | None = None

        try:
            activity = await _acquire_tenant_container_activity(
                request,
                tenant,
                runtime_id=context.runtime_id,
                required=context.sidecar_socket is not None,
            )
            sidecar = await create_sidecar_connection(context.sidecar_socket)
            await coordinator.register(session_id, sidecar)

            available_thinking_levels: tuple[ThinkingLevel, ...] | None = None
            if body.thinking is not None:
                catalog_payload = await sidecar.get_catalog(agent_dir=context.agent_dir)
                available_thinking_levels = _catalog_thinking_levels(
                    catalog_payload,
                    context.model_ref or model,
                )
                if available_thinking_levels == (_THINKING_LEVEL_OFF,):
                    logger.info(
                        "Ignoring thinking override for non-reasoning model: model=%s",
                        context.model_ref or model,
                    )

            effective_settings = _build_effective_settings(
                context.settings_payload,
                body.thinking,
                available_thinking_levels,
            )

            await sidecar.warmup(
                session_id,
                model=context.model_ref or model,
                cwd=context.effective_cwd,
                provider_auth=cast(dict[str, Any] | None, context.provider_auth),
                provider_config=cast(dict[str, Any] | None, context.provider_config),
                git_auth=cast(dict[str, Any] | None, context.git_auth),
                agent_dir=context.agent_dir,
                settings_payload=effective_settings,
                pi_session_file=context.pi_session_file,
            )

            logger.info("Prompt stream started")

            sidecar_events = sidecar.query(
                session_id,
                prompt,
                model=context.model_ref or model,
                cwd=context.effective_cwd,
                provider_auth=cast(dict[str, Any] | None, context.provider_auth),
                provider_config=cast(dict[str, Any] | None, context.provider_config),
                git_auth=cast(dict[str, Any] | None, context.git_auth),
                agent_dir=context.agent_dir,
                settings_payload=effective_settings,
                pi_session_file=context.pi_session_file,
            )
            if desktop_device_id is not None:
                assert tenant is not None, "desktop authority requires a tenant"
                event_source = _desktop_device_bound_events(
                    sidecar_events,
                    user_id=tenant.user_id,
                    device_id=desktop_device_id,
                    sidecar=sidecar,
                    session_id=session_id,
                )
            elif requires_auth_session:
                if not auth_session_token:
                    raise _AuthSessionRevoked
                event_source = _session_bound_events(
                    sidecar_events,
                    auth_session_token,
                    sidecar,
                    session_id,
                )
            else:
                event_source = sidecar_events

            async for event in event_source:
                event_type = event.get("type")
                if not isinstance(event_type, str):
                    raise SidecarError("Sidecar event type must be a string")
                logger.debug("Sidecar event received")

                if event_type == "cancelled":
                    turn_status = "cancelled"
                    cancel_event = {"type": "cancelled", "reason": "user_stop"}
                    turn_events.append(_stored_turn_event(cancel_event))
                    yield f"data: {json.dumps(cancel_event)}\n\n"
                    break

                if event_type == "message":
                    data = event.get("data", {})
                    if not isinstance(data, dict):
                        raise SidecarError("Sidecar message event must contain object data")
                    logger.debug("Sidecar message event received")
                    turn_events.append(_stored_turn_event(data))

                    # Extract assistant text for persistence
                    if data.get("type") == "assistant":
                        message_payload = data.get("message", {})
                        if not isinstance(message_payload, dict):
                            raise SidecarError("Assistant event message payload must be an object")
                        content_blocks = message_payload.get("content", [])
                        if not isinstance(content_blocks, list):
                            raise SidecarError("Assistant event content must be a list")
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if isinstance(text, str) and text:
                                    accumulated += text
                                    chunk_count += 1

                        # Batched incremental persistence
                        if accumulated and chunk_count % _PERSIST_BATCH_SIZE == 0:
                            if assistant_msg_id is None:
                                assistant_msg_id = uuid.uuid4().hex
                            await _persist_assistant_turn(
                                request,
                                message_id=assistant_msg_id,
                                session_id=session_id,
                                turn_id=turn_id,
                                content=accumulated,
                            )

                    # On result, capture usage and finalize with full_message
                    if data.get("type") == "result":
                        usage_payload = data.get("usage", {})
                        usage_data = usage_payload if isinstance(usage_payload, dict) else {}
                        provider_payload = data.get("provider")
                        if isinstance(provider_payload, str):
                            result_provider = provider_payload
                        stored_turn = _serialize_stored_turn(turn_events)
                        assert (
                            stored_turn is not None
                        ), "result event must be present in stored turn"
                        # Ensure an assistant message row exists even for
                        # short responses (< batch size) or tool-only turns.
                        if assistant_msg_id is None:
                            assistant_msg_id = uuid.uuid4().hex
                        await _persist_assistant_turn(
                            request,
                            message_id=assistant_msg_id,
                            session_id=session_id,
                            turn_id=turn_id,
                            content=accumulated,
                            full_message=stored_turn,
                            turn_status=turn_status,
                        )

                    # Yield the SSE event with the inner data
                    yield f"data: {json.dumps(data)}\n\n"

                elif event_type == "error":
                    turn_status = "failed"
                    error_value = event.get("error", "Unknown sidecar error")
                    error_msg = error_value if isinstance(error_value, str) else str(error_value)
                    error_event = {"type": "error", "error": error_msg}
                    turn_events.append(_stored_turn_event(error_event))
                    yield f"data: {json.dumps(error_event)}\n\n"

                else:
                    # Forward any other event types (content_block_start, tool_use, etc.)
                    turn_events.append(_stored_turn_event(event))
                    yield f"data: {json.dumps(event)}\n\n"

        except _AuthSessionRevoked:
            turn_status = "cancelled"
            logger.info("Prompt stream revoked")
        except _StreamLifetimeReached:
            turn_status = "cancelled"
            logger.info("Prompt stream lifetime reached")
        except (
            ConnectionError,
            ContainerNotReadyError,
            ContainerStartError,
            OSError,
            GitError,
            SidecarError,
            TypeError,
            ValueError,
        ):
            logger.error("Sidecar prompt execution failed")
            error_event = {
                "type": "error",
                "error": "An internal error occurred",
            }
            turn_events.append(_stored_turn_event(error_event))
            yield f"data: {json.dumps(error_event)}\n\n"
            turn_status = "failed"

        finally:
            active_error = sys.exception()
            finalization_error: BaseException | None = None

            try:
                stored_turn = _serialize_stored_turn(turn_events)
                if assistant_msg_id is None and (accumulated or stored_turn is not None):
                    assistant_msg_id = uuid.uuid4().hex
                if assistant_msg_id is None:
                    await _set_prompt_session_idle(
                        request,
                        session_id,
                        turn_id,
                        background=True,
                    )
                else:
                    await _persist_assistant_turn(
                        request,
                        message_id=assistant_msg_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        content=accumulated,
                        full_message=stored_turn,
                        turn_status=turn_status,
                        finalize_session=True,
                    )
            except BaseException as exc:
                logger.error("Prompt persistence finalization failed")
                if active_error is None:
                    finalization_error = exc

            if tenant and usage_data:
                try:
                    record_usage(
                        user_id=tenant.user_id,
                        session_id=session_id,
                        provider=result_provider,
                        model=context.model_ref or model,
                        usage=usage_data,
                        key_source=context.key_source,
                    )
                except Exception:
                    logger.error("Failed to record prompt usage")

            try:
                touch_tenant_container(request, tenant, runtime_id=context.runtime_id)
            except BaseException as exc:
                logger.error("Prompt container touch failed")
                if active_error is None and finalization_error is None:
                    finalization_error = exc

            try:
                await coordinator.release(session_id)
            except BaseException as exc:
                logger.error("Prompt run release failed")
                if active_error is None and finalization_error is None:
                    finalization_error = exc

            if sidecar:
                try:
                    await sidecar.disconnect()
                except BaseException as exc:
                    logger.error("Prompt sidecar disconnect failed")
                    if active_error is None and finalization_error is None:
                        finalization_error = exc

            try:
                await _release_tenant_container_activity(activity)
            except BaseException as exc:
                logger.error("Prompt container activity release failed")
                if active_error is None and finalization_error is None:
                    finalization_error = exc

            if finalization_error is not None:
                raise finalization_error

            logger.info(
                "Turn complete: chunks=%d content_len=%d turn_status=%s",
                chunk_count,
                len(accumulated),
                turn_status,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request) -> dict[str, str]:
    """Cancel the active sidecar operation for a session."""
    session = await _prompt_database_operation(
        request,
        lambda database: _lookup_session(database, session_id, request),
    )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not get_tenant(request):
        check_owner(session["owner_email"], get_user_email(request))

    coordinator = get_run_coordinator()
    outcome = await coordinator.request_cancel(session_id)
    if outcome is CancelOutcome.ABSENT:
        raise HTTPException(status_code=409, detail="No active stream for this session")
    if outcome is CancelOutcome.FINISHED:
        logger.info("Prompt run finished before cancellation completed")
        return {"status": "stopped"}

    logger.info("Prompt cancellation requested")
    return {"status": "stopping"}
