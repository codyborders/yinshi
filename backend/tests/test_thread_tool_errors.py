"""Expose fixed domain errors through encoded thread protocol frames."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from tests.test_sidecar_orchestration_lifecycle import BridgePeer
from tests.test_thread_provisioning_cancel import _orchestration_request
from tests.test_thread_workspaces import seed_parent_stack
from yinshi.config import get_settings
from yinshi.exceptions import SidecarNotConnectedError
from yinshi.services.orchestration_bridge import (
    THREAD_ERROR_MESSAGES,
    generate_orchestration_capability,
)
from yinshi.services.thread_git_ownership import ThreadGitOwnershipError
from yinshi.services.thread_orchestration import (
    ThreadActiveDescendantsLimitError,
    ThreadChildLimitError,
    ThreadDepthLimitError,
    ThreadOrchestrationError,
    ThreadOrchestrationService,
    ThreadParentNotAuthorizedError,
    ThreadTreeLimitError,
)
from yinshi.services.thread_queries import ThreadNotFoundError
from yinshi.services.thread_tool_handlers import build_thread_handlers
from yinshi.tenant import TenantDatabaseTemporarilyUnavailable


class FailingService(ThreadOrchestrationService):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def spawn_child(self, request, **kwargs):
        raise self.error

    async def get_agent_thread(self, request, **kwargs):
        raise self.error


async def _wire_error(error, *, operation="spawn_thread"):
    handlers = build_thread_handlers(_orchestration_request(), FailingService(error))
    peer = BridgePeer(orchestration_handlers=handlers)
    peer.capability = generate_orchestration_capability(
        "sess-1",
        run_id="run-1",
        allowed_operations=frozenset({operation}),
        database_path="/backend/selected/yinshi.db",
    )
    query = asyncio.create_task(peer.collect())
    arguments = (
        {"title": "Child", "task": "Inspect"}
        if operation == "spawn_thread"
        else {"thread_id": "target"}
    )
    peer.feed(
        peer.request(
            operation=operation, arguments=arguments, protocol_version=2, tool_call_id="sdk-error"
        )
    )
    try:
        response = (await peer.wait_responses())[0]
        assert response["ok"] is False
        assert peer.capability.token not in json.dumps(response)
        return response["error"]
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ThreadDepthLimitError("private-source"), "depth_exceeded"),
        (ThreadChildLimitError("private-source"), "child_limit_exceeded"),
        (ThreadActiveDescendantsLimitError("private-source"), "active_thread_limit_exceeded"),
        (ThreadTreeLimitError("private-source"), "tree_limit_exceeded"),
        (
            ThreadOrchestrationError("spawn_limit_exceeded", "private-source"),
            "spawn_turn_limit_exceeded",
        ),
    ],
)
async def test_limit_errors_reach_the_wire_without_exception_text(error, code, caplog):
    response = await _wire_error(error)
    assert response["code"] == code
    assert "private-source" not in json.dumps(response) + caplog.text


async def test_missing_and_unauthorized_thread_errors_are_identical_on_the_wire(caplog):
    missing = await _wire_error(ThreadNotFoundError("private-source"), operation="get_thread")
    denied = await _wire_error(
        ThreadParentNotAuthorizedError("private-source"), operation="get_thread"
    )
    assert missing == denied == {"code": "thread_not_found", "message": "Thread not found."}
    assert "private-source" not in caplog.text


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TenantDatabaseTemporarilyUnavailable("private-source"), "runtime_unavailable"),
        (SidecarNotConnectedError("private-source"), "runtime_unavailable"),
        (ThreadGitOwnershipError(), "workspace_provisioning_failed"),
        (ThreadOrchestrationError("private-code", "private-source"), "handler_failed"),
        (RuntimeError("private-source"), "handler_failed"),
    ],
)
async def test_storage_and_unknown_errors_do_not_leak_or_relabel_git_ownership(error, code, caplog):
    response = await _wire_error(error)
    assert response["code"] == code
    assert "private-" not in json.dumps(response) + caplog.text


async def test_real_target_authorization_has_identical_wire_errors_without_recovery_mutation(
    db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    db.execute("UPDATE sessions SET id = 'sess-1' WHERE id = 'parent-session'")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('other-root', 'parent-ws')")
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'sess-1', 'root', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, updated_at) VALUES (?, 'other-root', 'foreign', 'user', 'Private', 'Inspect', 'model', 'provisioning', '2000-01-01')",
        ("3" * 32,),
    )
    db.commit()
    before = dict(db.execute("SELECT * FROM thread_delegations").fetchone())
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    peer = BridgePeer(
        orchestration_handlers=build_thread_handlers(
            _orchestration_request(), ThreadOrchestrationService()
        )
    )
    peer.capability = generate_orchestration_capability(
        "sess-1",
        run_id="1" * 32,
        allowed_operations=frozenset({"get_thread"}),
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    query = asyncio.create_task(peer.collect())
    try:
        for index, target in enumerate(("missing", "3" * 32)):
            peer.feed(
                peer.request(
                    request_id=f"delivery-{index}",
                    operation="get_thread",
                    arguments={"thread_id": target},
                    protocol_version=2,
                    tool_call_id=f"sdk-{index}",
                )
            )
        responses = await peer.wait_responses(2)
        assert {item["request_id"] for item in responses} == {"delivery-0", "delivery-1"}
        assert [item["error"] for item in responses] == [
            {"code": "thread_not_found", "message": "Thread not found."},
        ] * 2
        assert dict(db.execute("SELECT * FROM thread_delegations").fetchone()) == before
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


async def test_python_domain_codes_match_actual_node_tool_error_messages(tmp_path):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is unavailable")
    node = str(Path(executable).resolve(strict=True))
    script = """
import { createOrchestrationRpc } from './src/orchestration_rpc.js';
import { createThreadTools } from './src/orchestration_tools.js';
const messages = {};
for (const code of JSON.parse(process.argv[1])) {
  const rpc = createOrchestrationRpc({
    sessionId: 'parent', capability: 'private-capability', protocolVersion: 2,
    allowedOperations: ['spawn_thread'],
    send: frame => rpc.handleFrame({ type: 'orchestration_response', id: frame.id,
      request_id: frame.request_id, ok: false, error: { code, message: 'private-message' } }),
  });
  const [tool] = createThreadTools({ allowedOperations: ['spawn_thread'], rpcForCall: () => rpc });
  try {
    await tool.execute('sdk-call', { title: 'Child', task: 'Inspect' });
    throw new Error('Expected a rejected tool call');
  } catch (error) {
    const result = JSON.parse(error.message).error;
    if (result.code !== code || error.code !== code) throw new Error('Domain code mismatch');
    messages[code] = result.message;
  } finally {
    rpc.dispose();
  }
}
console.log(JSON.stringify(messages));
"""
    process = await asyncio.create_subprocess_exec(
        node,
        "--input-type=module",
        "-e",
        script,
        json.dumps(list(THREAD_ERROR_MESSAGES)),
        cwd=Path(__file__).resolve().parents[2] / "sidecar",
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            output, errors = await process.communicate()
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
    assert process.returncode == 0, errors.decode(errors="replace")
    assert json.loads(output) == THREAD_ERROR_MESSAGES
