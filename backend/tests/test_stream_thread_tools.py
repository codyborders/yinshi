"""Bind durable prompts to backend-selected root and child tool permissions."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.api.stream import ExecutionContext, PromptRequest
from yinshi.config import get_settings
from yinshi.models import ThreadChildCreate
from yinshi.services.sidecar import SidecarClient
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)


@pytest.mark.parametrize("is_child,enabled", [(False, True), (True, True), (False, False)])
async def test_durable_prompt_receives_only_backend_selected_tools(
    db, git_repo, monkeypatch, is_child, enabled
):
    from yinshi.main import create_app

    seed_parent_stack(db, git_repo)
    session_id = "1" * 32
    db.execute("UPDATE sessions SET id = ? WHERE id = 'parent-session'", (session_id,))
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()
    app = create_app()
    request = _orchestration_request()
    request.scope["app"] = app
    key = str(uuid.uuid4())
    if is_child:
        child = await ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id=session_id,
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Child",
                task="Inspect",
                start_immediately=False,
            ),
        )
        session_id = child.child_session_id
        key = initial_run_idempotency_key(child.delegation_id)
    captured = []

    class Recorder(SidecarClient):
        async def warmup(self, *args, **kwargs):
            return None

        async def query(self, session_id, prompt, **kwargs):
            captured.append(kwargs)
            yield {"type": "result", "usage": {}}

    context = ExecutionContext(
        sidecar_socket=None,
        effective_cwd=git_repo,
        key_source="platform",
        provider="test",
        provider_auth=None,
        provider_config=None,
    )
    monkeypatch.setattr(
        "yinshi.api.stream.create_sidecar_connection", AsyncMock(return_value=Recorder())
    )
    monkeypatch.setattr(
        "yinshi.api.stream._resolve_execution_context", AsyncMock(return_value=context)
    )
    journal = app.state.prompt_journal
    try:
        await journal.start(
            request=request,
            session_id=session_id,
            idempotency_key=key,
            body=PromptRequest(prompt="Inspect"),
        )
        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)
        assert captured
        operations = captured[0]["orchestration_capability"].allowed_operations
        expected = {
            "spawn_thread",
            "list_children",
            "get_thread",
            "wait_for_threads",
            "cancel_thread",
        }
        if is_child:
            expected.add("report_thread_result")
        if not enabled:
            expected = {"ping_thread_bridge"}
        assert operations == expected
        if enabled:
            assert expected <= set(captured[0]["orchestration_handlers"])
            assert (
                captured[0]["orchestration_capability"].database_path
                == db.execute("PRAGMA database_list").fetchone()[2]
            )
    finally:
        await journal.close()
