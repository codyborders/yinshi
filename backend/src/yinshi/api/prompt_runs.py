"""Reconnectable JSON prompt-run routes for local, hosted, and BYOC transports."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from yinshi.api.deps import check_session_owner, run_db_operation_for_request
from yinshi.api.stream import PromptRequest
from yinshi.services.prompt_journal import (
    PromptEventBatch,
    PromptJournal,
    PromptRun,
    PromptRunConflictError,
    PromptRunNotFoundError,
)

router = APIRouter()
RunStatus = Literal[
    "starting",
    "running",
    "stopping",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class PromptRunStart(PromptRequest):
    """Prompt content plus a client-stable idempotency key."""

    idempotency_key: str = Field(..., max_length=36)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        """Require one canonical UUID so retries cannot alias."""
        try:
            normalized = str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("idempotency_key must be a UUID") from exc
        if normalized != value:
            raise ValueError("idempotency_key must be canonical")
        return normalized


class PromptRunResponse(BaseModel):
    id: str
    session_id: str
    status: RunStatus


class PromptEventBatchResponse(BaseModel):
    run_id: str
    status: RunStatus
    events: list[dict[str, Any]]
    next_sequence: int


def _prompt_journal(request: Request) -> PromptJournal:
    journal = getattr(request.app.state, "prompt_journal", None)
    if not isinstance(journal, PromptJournal):
        raise RuntimeError("prompt journal is unavailable")
    return journal


async def _require_session(request: Request, session_id: str) -> None:
    def require(database: sqlite3.Connection) -> None:
        row = database.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(database, session_id, request)

    await run_db_operation_for_request(request, require)


def _run_response(run: PromptRun) -> PromptRunResponse:
    return PromptRunResponse(
        id=run.id,
        session_id=run.session_id,
        status=cast(RunStatus, run.status),
    )


def _event_response(batch: PromptEventBatch) -> PromptEventBatchResponse:
    return PromptEventBatchResponse(
        run_id=batch.run_id,
        status=cast(RunStatus, batch.status),
        events=list(batch.events),
        next_sequence=batch.next_sequence,
    )


@router.post(
    "/api/sessions/{session_id}/runs",
    response_model=PromptRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_prompt_run(
    session_id: str,
    body: PromptRunStart,
    request: Request,
) -> PromptRunResponse:
    """Start one durable prompt run or return its idempotent predecessor."""
    await _require_session(request, session_id)
    prompt_body = PromptRequest(
        prompt=body.prompt,
        model=body.model,
        thinking=body.thinking,
    )
    try:
        run = await _prompt_journal(request).start(
            request=request,
            session_id=session_id,
            idempotency_key=body.idempotency_key,
            body=prompt_body,
        )
    except PromptRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except PromptRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_response(run)


@router.get(
    "/api/sessions/{session_id}/runs/active",
    response_model=PromptRunResponse | None,
)
async def get_active_prompt_run(
    session_id: str,
    request: Request,
) -> PromptRunResponse | None:
    """Return the current durable run without starting or cancelling it."""
    await _require_session(request, session_id)
    try:
        run = await _prompt_journal(request).active(
            request=request,
            session_id=session_id,
        )
    except PromptRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    return _run_response(run) if run is not None else None


@router.get(
    "/api/sessions/{session_id}/runs/{run_id}/events/{next_sequence}",
    response_model=PromptEventBatchResponse,
)
async def get_prompt_events(
    session_id: str,
    run_id: str,
    next_sequence: int,
    request: Request,
) -> PromptEventBatchResponse:
    """Return one bounded contiguous journal page from the supplied cursor."""
    await _require_session(request, session_id)
    try:
        batch = await _prompt_journal(request).events(
            request=request,
            session_id=session_id,
            run_id=run_id,
            next_sequence=next_sequence,
        )
    except (PromptRunNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Prompt run not found") from exc
    return _event_response(batch)


@router.post(
    "/api/sessions/{session_id}/runs/{run_id}/cancel",
    response_model=PromptRunResponse,
)
async def cancel_prompt_run(
    session_id: str,
    run_id: str,
    request: Request,
) -> PromptRunResponse:
    """Cancel one run idempotently without depending on a live HTTP stream."""
    await _require_session(request, session_id)
    try:
        run = await _prompt_journal(request).cancel(
            request=request,
            session_id=session_id,
            run_id=run_id,
        )
    except (PromptRunNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Prompt run not found") from exc
    return _run_response(run)
