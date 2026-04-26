"""SSE streaming endpoint for agent interaction.

Tests: test_prompt_session_not_found, test_prompt_streams_sidecar_events,
       test_prompt_saves_partial_on_sidecar_error, test_cancel_session_not_found,
       test_cancel_no_active_stream in tests/test_api.py
"""

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from yinshi.api.deps import check_owner, get_db_for_request, get_tenant, get_user_email
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
from yinshi.services.git_runtime import resolve_git_runtime_auth
from yinshi.services.keys import record_usage
from yinshi.services.provider_connections import (
    resolve_provider_connection,
    update_provider_connection_secret,
)
from yinshi.services.run_coordinator import get_run_coordinator
from yinshi.services.sidecar import SidecarClient, create_sidecar_connection
from yinshi.services.sidecar_runtime import (
    begin_tenant_container_activity,
    end_tenant_container_activity,
    remap_path_for_container,
    resolve_tenant_sidecar_context,
    touch_tenant_container,
)
from yinshi.services.workspace import ensure_workspace_checkout_for_tenant
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)
router = APIRouter()

# Batch DB writes every N chunks to reduce I/O
_PERSIST_BATCH_SIZE = 10
_STORED_TURN_SCHEMA = "yinshi.assistant_turn.v1"
_THINKING_LEVEL_DEFAULT = "medium"
_THINKING_LEVEL_OFF = "off"
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})


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


class PromptRequest(BaseModel):
    prompt: str = Field(..., max_length=100_000)
    model: str | None = None
    thinking: bool | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        """Normalize optional model values into canonical refs."""
        if value is None:
            return None
        return normalize_model_ref(value)


def _catalog_reasoning_support(
    catalog_payload: dict[str, Any],
    model_ref: str,
) -> bool | None:
    """Return whether one catalog model supports reasoning."""
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

        reasoning_value = model_payload.get("reasoning")
        if isinstance(reasoning_value, bool):
            return reasoning_value
        logger.warning("Catalog reasoning flag missing for model %s", normalized_model_ref)
        return None

    logger.warning("Catalog entry missing for model %s", normalized_model_ref)
    return None


def _build_effective_settings(
    settings_payload: dict[str, object] | None,
    thinking_override: bool | None,
    reasoning_supported: bool | None,
) -> dict[str, object] | None:
    """Merge one prompt-scoped thinking override into Pi-compatible settings."""
    if thinking_override is None:
        return settings_payload
    if reasoning_supported is False:
        return settings_payload

    effective_settings = dict(settings_payload or {})
    current_level = effective_settings.get("defaultThinkingLevel")
    if thinking_override:
        if (
            not isinstance(current_level, str)
            or current_level not in _THINKING_LEVELS
            or current_level == _THINKING_LEVEL_OFF
        ):
            effective_settings["defaultThinkingLevel"] = _THINKING_LEVEL_DEFAULT
    else:
        effective_settings["defaultThinkingLevel"] = _THINKING_LEVEL_OFF
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
        )
    except (ContainerStartError, ContainerNotReadyError):
        logger.exception("Container start failed for user %s", tenant.user_id[:8])
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
    begin_tenant_container_activity(request, tenant)
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
        end_tenant_container_activity(request, tenant)
        if sidecar_tmp is not None:
            await sidecar_tmp.disconnect()

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
    with get_db_for_request(request) as db:
        session = _lookup_session(db, session_id, request)
        if session and tenant:
            try:
                await ensure_workspace_checkout_for_tenant(
                    db,
                    tenant,
                    session["workspace_id"],
                )
            except (GitError, RepoNotFoundError, WorkspaceNotFoundError) as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            session = _lookup_session(db, session_id, request)

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
    turn_id = uuid.uuid4().hex

    # Atomically claim the session for this stream. The WHERE clause
    # ensures only one concurrent request can transition idle -> running.
    with get_db_for_request(request) as db:
        result = db.execute(
            "UPDATE sessions SET status = 'running' WHERE id = ? AND status = 'idle'",
            (session_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="Session already has an active stream")

        db.execute(
            "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
            (session_id, prompt, turn_id),
        )
        # Update workspace name on first prompt (when name == branch)
        if session["workspace_name"] == session["workspace_branch"]:
            display_name = _summarize_prompt(prompt)
            db.execute(
                "UPDATE workspaces SET name = ? WHERE id = ?",
                (display_name, session["workspace_id"]),
            )
        db.commit()

    try:
        context = await _resolve_execution_context(
            request,
            tenant,
            session_id,
            workspace_path,
            model,
            repo_root_path=(
                session["repo_root_path"] if "repo_root_path" in session.keys() else None
            ),
            remote_url=remote_url,
            installation_id=installation_id,
            agents_md=session["agents_md"] if "agents_md" in session.keys() else None,
        )
    except Exception:
        with get_db_for_request(request) as db:
            db.execute(
                "UPDATE sessions SET status = 'idle' WHERE id = ?",
                (session_id,),
            )
            db.commit()
        raise

    logger.info(
        "Prompt received: session=%s prompt_len=%d model=%s provider=%s key_source=%s",
        session_id,
        len(prompt),
        model,
        context.provider,
        context.key_source,
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

        begin_tenant_container_activity(request, tenant)

        try:
            sidecar = await create_sidecar_connection(context.sidecar_socket)
            await coordinator.register(session_id, sidecar)

            reasoning_supported: bool | None = None
            if body.thinking is not None:
                catalog_payload = await sidecar.get_catalog(agent_dir=context.agent_dir)
                reasoning_supported = _catalog_reasoning_support(
                    catalog_payload,
                    context.model_ref or model,
                )
                if reasoning_supported is False:
                    logger.info(
                        "Ignoring thinking override for non-reasoning model: "
                        "session=%s model=%s",
                        session_id,
                        context.model_ref or model,
                    )

            effective_settings = _build_effective_settings(
                context.settings_payload,
                body.thinking,
                reasoning_supported,
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
            )

            logger.info("Streaming started: session=%s turn_id=%s", session_id, turn_id)

            async for event in sidecar.query(
                session_id,
                prompt,
                model=context.model_ref or model,
                cwd=context.effective_cwd,
                provider_auth=cast(dict[str, Any] | None, context.provider_auth),
                provider_config=cast(dict[str, Any] | None, context.provider_config),
                git_auth=cast(dict[str, Any] | None, context.git_auth),
                agent_dir=context.agent_dir,
                settings_payload=effective_settings,
            ):
                event_type = event.get("type")
                if not isinstance(event_type, str):
                    raise SidecarError("Sidecar event type must be a string")
                logger.debug(
                    "Sidecar event: type=%s keys=%s",
                    event_type,
                    list(event.keys()),
                )

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
                    logger.debug(
                        "SSE data: type=%s keys=%s",
                        data.get("type"),
                        list(data.keys()),
                    )
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
                            with get_db_for_request(request) as db:
                                if assistant_msg_id is None:
                                    assistant_msg_id = uuid.uuid4().hex
                                    db.execute(
                                        (
                                            "INSERT INTO messages "
                                            "(id, session_id, role, content, turn_id) "
                                            "VALUES (?, ?, 'assistant', ?, ?)"
                                        ),
                                        (assistant_msg_id, session_id, accumulated, turn_id),
                                    )
                                else:
                                    db.execute(
                                        "UPDATE messages SET content = ? WHERE id = ?",
                                        (accumulated, assistant_msg_id),
                                    )
                                db.commit()

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
                        with get_db_for_request(request) as db:
                            if assistant_msg_id is None:
                                assistant_msg_id = uuid.uuid4().hex
                                db.execute(
                                    (
                                        "INSERT INTO messages "
                                        "(id, session_id, role, content, turn_id) "
                                        "VALUES (?, ?, 'assistant', ?, ?)"
                                    ),
                                    (assistant_msg_id, session_id, accumulated, turn_id),
                                )
                            db.execute(
                                (
                                    "UPDATE messages SET full_message = ?, "
                                    "turn_status = ? WHERE id = ?"
                                ),
                                (stored_turn, turn_status, assistant_msg_id),
                            )
                            db.commit()

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

        except (ConnectionError, OSError, GitError, SidecarError, TypeError, ValueError) as e:
            logger.error(
                "Sidecar error: session=%s turn_id=%s error=%s",
                session_id,
                turn_id,
                e,
                exc_info=True,
            )
            error_event = {
                "type": "error",
                "error": "An internal error occurred",
            }
            turn_events.append(_stored_turn_event(error_event))
            yield f"data: {json.dumps(error_event)}\n\n"
            turn_status = "failed"

        finally:
            with get_db_for_request(request) as db:
                stored_turn = _serialize_stored_turn(turn_events)
                if assistant_msg_id is None:
                    if accumulated or stored_turn is not None:
                        assistant_msg_id = uuid.uuid4().hex
                        db.execute(
                            (
                                "INSERT INTO messages "
                                "(id, session_id, role, content, full_message, "
                                "turn_id, turn_status) "
                                "VALUES (?, ?, 'assistant', ?, ?, ?, ?)"
                            ),
                            (
                                assistant_msg_id,
                                session_id,
                                accumulated,
                                stored_turn,
                                turn_id,
                                turn_status,
                            ),
                        )
                else:
                    db.execute(
                        (
                            "UPDATE messages SET content = ?, full_message = ?, "
                            "turn_status = ? WHERE id = ?"
                        ),
                        (accumulated, stored_turn, turn_status, assistant_msg_id),
                    )
                # Reset session status
                db.execute(
                    "UPDATE sessions SET status = 'idle' WHERE id = ?",
                    (session_id,),
                )
                db.commit()

            # Record usage if in tenant mode
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
                    logger.exception("Failed to record usage: session=%s", session_id)

            # Keep container alive after activity
            end_tenant_container_activity(request, tenant)
            touch_tenant_container(request, tenant)

            await coordinator.release(session_id)
            if sidecar:
                await sidecar.disconnect()

            logger.info(
                "Turn complete: session=%s turn_id=%s chunks=%d content_len=%d turn_status=%s",
                session_id,
                turn_id,
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
    with get_db_for_request(request) as db:
        session = _lookup_session(db, session_id, request)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not get_tenant(request):
        check_owner(session["owner_email"], get_user_email(request))

    coordinator = get_run_coordinator()
    found = await coordinator.request_cancel(session_id)
    if not found:
        raise HTTPException(status_code=409, detail="No active stream for this session")

    logger.info("Cancel requested: session=%s", session_id)
    return {"status": "stopping"}
