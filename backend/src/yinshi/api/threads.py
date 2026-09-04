"""Read-only thread hierarchy endpoints.

Threads are projections over existing sessions. Parentage comes from
``thread_delegations``. Every endpoint performs the same session-ownership
check as the existing session APIs before returning data.
"""

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import (
    check_session_owner,
    get_db_for_request,
    get_tenant,
    get_user_email,
)
from yinshi.config import get_settings
from yinshi.models import (
    ThreadLimitsOut,
    ThreadOut,
    ThreadResultOut,
    ThreadTreeOut,
)
from yinshi.services.thread_queries import (
    ThreadNotFoundError,
    get_thread,
    get_thread_limits,
    get_thread_result,
    get_tree,
    list_children,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["threads"])

_QueryResult = TypeVar("_QueryResult")


def _resolve_authorized_thread(
    db: Any,
    session_id: str,
    request: Request,
) -> None:
    """Fail with 404 when the session is missing or not owned."""
    session = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    check_session_owner(db, session_id, request)


def _hierarchy_enabled() -> bool:
    """Return whether the thread hierarchy feature flag is enabled."""
    return get_settings().thread_hierarchy_enabled


def _legacy_owner_email(request: Request) -> str | None:
    """Return the legacy per-repo owner filter for this request, when any."""
    if get_tenant(request) is not None:
        return None
    return get_user_email(request)


def _run_visible_query(operation: Callable[[], _QueryResult]) -> _QueryResult:
    """Map hidden ancestry to the standard not-found response."""
    try:
        return operation()
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc


@router.get("/api/threads/{session_id}", response_model=ThreadOut)
def get_thread_route(session_id: str, request: Request) -> dict[str, Any]:
    """Return one thread projection."""
    if not _hierarchy_enabled():
        raise HTTPException(status_code=404, detail="Thread hierarchy is disabled")
    with get_db_for_request(request) as db:
        _resolve_authorized_thread(db, session_id, request)
        return _run_visible_query(
            lambda: get_thread(db, session_id, owner_email=_legacy_owner_email(request))
        )


@router.get(
    "/api/threads/{session_id}/children",
    response_model=list[ThreadOut],
)
def get_thread_children_route(session_id: str, request: Request) -> list[dict[str, Any]]:
    """Return the direct children of a thread."""
    if not _hierarchy_enabled():
        raise HTTPException(status_code=404, detail="Thread hierarchy is disabled")
    with get_db_for_request(request) as db:
        _resolve_authorized_thread(db, session_id, request)
        return _run_visible_query(
            lambda: list_children(db, session_id, owner_email=_legacy_owner_email(request))
        )


@router.get("/api/threads/{session_id}/limits", response_model=ThreadLimitsOut)
def get_thread_limits_route(session_id: str, request: Request) -> dict[str, Any]:
    """Return thread limits plus current usage and remaining allowances."""
    if not _hierarchy_enabled():
        raise HTTPException(status_code=404, detail="Thread hierarchy is disabled")
    with get_db_for_request(request) as db:
        _resolve_authorized_thread(db, session_id, request)
        return _run_visible_query(
            lambda: get_thread_limits(db, session_id, owner_email=_legacy_owner_email(request))
        )


@router.get("/api/threads/{session_id}/result", response_model=ThreadResultOut)
def get_thread_result_route(session_id: str, request: Request) -> dict[str, Any]:
    """Return the sealed stored result for a delegated child thread."""
    if not _hierarchy_enabled():
        raise HTTPException(status_code=404, detail="Thread hierarchy is disabled")
    with get_db_for_request(request) as db:
        _resolve_authorized_thread(db, session_id, request)
        owner_email = _legacy_owner_email(request)
        result = _run_visible_query(
            lambda: get_thread_result(db, session_id, owner_email=owner_email)
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Thread result not found")
    return result


@router.get("/api/threads/{session_id}/tree", response_model=ThreadTreeOut)
def get_thread_tree_route(session_id: str, request: Request) -> dict[str, Any]:
    """Return the bounded root thread tree containing a session."""
    if not _hierarchy_enabled():
        raise HTTPException(status_code=404, detail="Thread hierarchy is disabled")
    with get_db_for_request(request) as db:
        _resolve_authorized_thread(db, session_id, request)
        return _run_visible_query(
            lambda: get_tree(db, session_id, owner_email=_legacy_owner_email(request))
        )
