"""Strict model-facing adapters for the backend-owned thread lifecycle."""

import json
from typing import Annotated, Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from yinshi.exceptions import SidecarNotConnectedError
from yinshi.models import (
    ThreadChildCreate,
    ThreadResultReportCreate,
    ThreadResultReportTest,
)
from yinshi.services.orchestration_bridge import (
    THREAD_ERROR_MESSAGES,
    OrchestrationProtocolError,
    ThreadOrchestrationHandler,
    VerifiedThreadCaller,
)
from yinshi.services.thread_git_ownership import ThreadGitOwnershipError
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationError,
    ThreadOrchestrationService,
    ThreadSpawnOutcome,
)
from yinshi.services.thread_queries import ThreadNotFoundError
from yinshi.tenant import TenantDatabaseTemporarilyUnavailable

_CORE_ERROR_CODES = {
    "depth_exceeded": "depth_exceeded",
    "child_limit_exceeded": "child_limit_exceeded",
    "active_thread_limit_exceeded": "active_thread_limit_exceeded",
    "tree_limit_exceeded": "tree_limit_exceeded",
    "spawn_limit_exceeded": "spawn_turn_limit_exceeded",
    "parent_not_authorized": "thread_not_found",
}


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _SpawnInput(_ToolInput):
    title: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=20_000)
    context: str | None = Field(default=None, max_length=20_000)
    role: Literal["general", "research", "implementation", "test", "review", "debug"] = "general"
    model: str | None = Field(default=None, max_length=200)
    thinking: Literal["off", "minimal", "low", "medium", "high", "xhigh"] | None = None

    @field_validator("title", "task")
    @classmethod
    def require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("thread text must not be empty")
        return value


class _ListInput(_ToolInput):
    include_terminal: bool = True


class _GetInput(_ToolInput):
    thread_id: str = Field(min_length=1, max_length=128)
    include_result: bool = True


class _WaitInput(_ToolInput):
    thread_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=20
    )
    timeout_seconds: int = Field(default=60, ge=0, le=60)

    @field_validator("thread_ids")
    @classmethod
    def require_unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("thread IDs must be distinct")
        return value


class _CancelInput(_ToolInput):
    thread_id: str = Field(min_length=1, max_length=128)
    cascade: bool = True


class _ToolTest(ThreadResultReportTest):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str | None = Field(default=None, max_length=4000)


class _ReportInput(_ToolInput):
    summary: str = Field(min_length=1, max_length=20_000)
    tests: list[_ToolTest] = Field(default_factory=list, max_length=50)
    warnings: list[Annotated[str, Field(max_length=4000)]] = Field(
        default_factory=list, max_length=20
    )


class _AgentReportBody(ThreadResultReportCreate):
    """Keep the manual report limit unchanged while accepting the tool warning bound."""

    model_config = ConfigDict(extra="forbid", strict=True)
    warnings: list[Annotated[str, Field(max_length=4000)]] = Field(
        default_factory=list, max_length=20
    )


_INPUTS: dict[str, type[_ToolInput]] = {
    "spawn_thread": _SpawnInput,
    "list_children": _ListInput,
    "get_thread": _GetInput,
    "wait_for_threads": _WaitInput,
    "cancel_thread": _CancelInput,
    "report_thread_result": _ReportInput,
}


def build_thread_handlers(
    request: Request,
    service: ThreadOrchestrationService,
) -> dict[str, ThreadOrchestrationHandler]:
    """Bind strict tools to one backend request without repeating domain authority."""

    def bind(operation: str) -> ThreadOrchestrationHandler:
        async def handler(
            arguments: dict[str, Any], *, caller: VerifiedThreadCaller
        ) -> dict[str, Any]:
            try:
                body = _INPUTS[operation].model_validate(arguments)
                return await _dispatch(request, service, caller, operation, body)
            except ValidationError as exc:
                raise OrchestrationProtocolError(
                    "invalid_arguments", "Invalid thread tool arguments."
                ) from exc
            except ThreadNotFoundError as exc:
                raise OrchestrationProtocolError(
                    "thread_not_found", THREAD_ERROR_MESSAGES["thread_not_found"]
                ) from exc
            except ThreadOrchestrationError as exc:
                code = _CORE_ERROR_CODES.get(exc.code)
                if code is None:
                    raise
                raise OrchestrationProtocolError(code, THREAD_ERROR_MESSAGES[code]) from exc
            except (TenantDatabaseTemporarilyUnavailable, SidecarNotConnectedError) as exc:
                raise OrchestrationProtocolError(
                    "runtime_unavailable", THREAD_ERROR_MESSAGES["runtime_unavailable"]
                ) from exc
            except ThreadGitOwnershipError as exc:
                if operation != "spawn_thread":
                    raise
                raise OrchestrationProtocolError(
                    "workspace_provisioning_failed",
                    THREAD_ERROR_MESSAGES["workspace_provisioning_failed"],
                ) from exc

        return handler

    return {operation: bind(operation) for operation in _INPUTS}


async def _dispatch(
    request: Request,
    service: ThreadOrchestrationService,
    caller: VerifiedThreadCaller,
    operation: str,
    body: _ToolInput,
) -> dict[str, Any]:
    if isinstance(body, _SpawnInput):
        # The legacy request type requires a UUID. Core replaces this value with trusted call identity.
        spawn = ThreadChildCreate(
            idempotency_key="00000000-0000-0000-0000-000000000000",
            start_immediately=True,
            **body.model_dump(),
        )
        outcome = await service.spawn_child(
            request, parent_session_id=caller.session_id, body=spawn, caller=caller
        )
        return _outcome(outcome)
    if isinstance(body, _GetInput):
        value = await service.get_agent_thread(request, caller=caller, **body.model_dump())
        return _read_preview({"thread": _snapshot(value["thread"]), "result": value["result"]})
    if isinstance(body, _WaitInput):
        value = await service.wait_for_threads(request, caller=caller, **body.model_dump())
        return _read_preview(
            {
                "threads": [_snapshot(item) for item in value["threads"]],
                "complete": value["all_terminal"],
                "timed_out": value["timed_out"],
            }
        )
    if isinstance(body, _CancelInput):
        outcome = await service.cancel_child(request, caller=caller, **body.model_dump())
        return _outcome(outcome)
    if isinstance(body, _ReportInput):
        report = _AgentReportBody(expected_version=0, **body.model_dump())
        return await service.report_agent_result(request, caller=caller, body=report)
    if operation == "list_children":
        value = await service.list_agent_children(request, caller=caller, **body.model_dump())
        return _read_preview(
            {
                "children": [_snapshot(item) for item in value["children"]],
                "children_total": value["children_total"],
                "limits": value["limits"],
                "truncated": value["truncated"],
            }
        )
    raise OrchestrationProtocolError("unknown_operation", "The thread operation is not allowed.")


def _snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Publish only documented metadata, without exposing internal actor fields."""
    fields = (
        "delegation_id",
        "parent_id",
        "title",
        "role",
        "model",
        "started_at",
        "completed_at",
        "summary",
        "changed_files_count",
        "result_available",
        "result_pending",
        "truncated",
    )
    return {
        "thread_id": value["id"],
        "state": value["status"],
        **{key: value.get(key) for key in fields},
    }


def _read_preview(value: dict[str, Any]) -> dict[str, Any]:
    """Budget ASCII-escaped JSON and keep every selected identity and count."""
    rows = value.get("children", value.get("threads", []))
    if "thread" in value:
        rows = [value["thread"]]
    result = value.get("result")
    value["truncated"] = (
        bool(value.get("truncated"))
        or any(bool(row.get("truncated")) for row in rows)
        or bool(result and result.get("truncated"))
    )
    text_fields = ("summary", "title", "model", "role", "started_at", "completed_at")
    while len(json.dumps(value, ensure_ascii=True).encode()) > 150_000:
        candidates = [
            (len(row[key]), index, key)
            for index, row in enumerate(rows)
            for key in text_fields
            if isinstance(row.get(key), str) and len(row[key]) > 1
        ]
        if not candidates:
            raise OrchestrationProtocolError(
                "response_too_large", "Thread preview exceeds its output budget."
            )
        length, index, key = max(candidates)
        rows[index][key] = rows[index][key][: max(1, length // 2)]
        rows[index]["truncated"] = value["truncated"] = True
    return value


def _outcome(outcome: ThreadSpawnOutcome) -> dict[str, Any]:
    return {
        "thread_id": outcome.child_session_id or outcome.delegation_id,
        "delegation_id": outcome.delegation_id,
        "state": outcome.status,
        "error_code": outcome.error_code,
    }
