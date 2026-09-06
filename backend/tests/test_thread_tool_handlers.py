"""Check strict tool input conversion before core operations receive arguments."""

import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_thread_orchestration import _orchestration_request
from yinshi.services.orchestration_bridge import (
    OrchestrationProtocolError,
    VerifiedThreadCaller,
)
from yinshi.services.thread_orchestration import ThreadSpawnOutcome


@pytest.mark.parametrize(
    "operation,method,flag",
    [
        ("list_children", "list_agent_children", "include_terminal"),
        ("get_thread", "get_agent_thread", "include_result"),
    ],
)
async def test_read_tool_options_default_true_and_reject_transcript_fields(operation, method, flag):
    module = importlib.import_module("yinshi.services.thread_tool_handlers")
    core = AsyncMock(
        return_value={
            "thread": {"id": "child", "status": "running"},
            "result": None,
            "children": [],
            "children_total": 0,
            "limits": {},
            "truncated": False,
        }
    )
    handlers = module.build_thread_handlers(
        _orchestration_request(), SimpleNamespace(**{method: core})
    )
    caller = VerifiedThreadCaller(
        session_id="caller",
        run_id="run",
        tenant_id=None,
        runtime_id=None,
        tool_call_id="sdk-call",
        expires_at=time.monotonic() + 60,
        database_path="/backend/tenant/yinshi.db",
    )
    arguments = {} if operation == "list_children" else {"thread_id": "child"}
    await handlers[operation](arguments, caller=caller)
    assert core.call_args.kwargs[flag] is True
    await handlers[operation]({**arguments, flag: False}, caller=caller)
    assert core.call_args.kwargs[flag] is False
    with pytest.raises(OrchestrationProtocolError) as error:
        await handlers[operation]({**arguments, "include_messages": True}, caller=caller)
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize(
    "operation,valid,invalid,method",
    [
        (
            "spawn_thread",
            {"title": "Child", "task": "Inspect"},
            {"title": "Child", "task": "Inspect", "parent_session_id": "forged"},
            "spawn_child",
        ),
        ("list_children", {}, {"parent_id": "forged"}, "list_agent_children"),
        (
            "get_thread",
            {"thread_id": "child"},
            {"thread_id": "child", "include_messages": "true"},
            "get_agent_thread",
        ),
        (
            "wait_for_threads",
            {"thread_ids": ["child"]},
            {"thread_ids": ["child"], "timeout_seconds": 61},
            "wait_for_threads",
        ),
        (
            "cancel_thread",
            {"thread_id": "child"},
            {"thread_id": "child", "cascade": "false"},
            "cancel_child",
        ),
        (
            "report_thread_result",
            {"summary": "Done", "warnings": ["x" * 4000]},
            {"summary": "Done", "expected_version": 0},
            "report_agent_result",
        ),
    ],
)
async def test_thread_tool_input_is_strict_before_core_dispatch(operation, valid, invalid, method):
    module = importlib.import_module("yinshi.services.thread_tool_handlers")
    methods = {
        name: AsyncMock(return_value={"status": "accepted"})
        for name in (
            "spawn_child",
            "list_agent_children",
            "get_agent_thread",
            "wait_for_threads",
            "cancel_child",
            "report_agent_result",
        )
    }
    outcome = ThreadSpawnOutcome(
        delegation_id="delegation", status="running", child_session_id="child"
    )
    methods["spawn_child"].return_value = outcome
    methods["cancel_child"].return_value = outcome
    methods["list_agent_children"].return_value = {
        "children": [],
        "children_total": 0,
        "limits": {},
        "truncated": False,
    }
    methods["get_agent_thread"].return_value = {
        "thread": {"id": "child", "status": "running"},
        "result": None,
    }
    methods["wait_for_threads"].return_value = {
        "threads": [{"id": "child", "status": "running"}],
        "all_terminal": False,
        "timed_out": True,
    }
    handlers = module.build_thread_handlers(_orchestration_request(), SimpleNamespace(**methods))
    caller = VerifiedThreadCaller("caller", "run", None, None, "sdk-call", time.monotonic() + 60)
    with pytest.raises(OrchestrationProtocolError) as error:
        await handlers[operation](invalid, caller=caller)
    assert error.value.code == "invalid_arguments"
    methods[method].assert_not_awaited()
    response = await handlers[operation](valid, caller=caller)
    assert isinstance(response, dict)
    assert methods[method].await_count == 1
    assert methods[method].call_args.kwargs["caller"] is caller
