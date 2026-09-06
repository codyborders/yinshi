"""Exercise all six tools across the real Node/Python transport without credentials."""

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_workspaces import run_git
from yinshi.api.stream import PromptRequest
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import generate_orchestration_capability
from yinshi.services.prompt_journal import PromptJournal, get_active_prompt_run_id
from yinshi.services.sidecar import SidecarClient
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_tool_handlers import build_thread_handlers

_NODE_PEER = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const { YinshiSidecar } = await import(pathToFileURL(process.argv[1]));
const sidecar = new YinshiSidecar();
sidecar._createPiSession = async (...args) => {
  const tools = args.at(-1);
  const cwd = args[3];
  let emit;
  async function call(name, id, input) {
    emit({ type: 'tool_execution_start', toolCallId: id, toolName: name, args: input });
    const result = await tools.find(tool => tool.name === name).execute(id, input);
    emit({ type: 'tool_execution_end', toolCallId: id, result });
    return JSON.parse(result.content[0].text);
  }
  return {
    session: {
      subscribe(listener) { emit = listener; return () => {}; },
      async prompt(prompt) {
        if (prompt === 'E2E_ROOT') {
          assert.equal(tools.length, 5);
          assert.ok(!tools.some(tool => tool.name === 'report_thread_result'));
          const first = await call('spawn_thread', 'spawn-success', { title: 'Success', task: 'E2E_SUCCESS' });
          const replay = await call('spawn_thread', 'spawn-success', { title: 'Success', task: 'E2E_SUCCESS' });
          assert.equal(first.thread_id, replay.thread_id);
          const second = await call('spawn_thread', 'spawn-failure', { title: 'Failure', task: 'E2E_FAILURE' });
          const listed = await call('list_children', 'list', {});
          assert.equal(listed.children.length, 2);
          const waited = await call('wait_for_threads', 'wait', { thread_ids: [first.thread_id, second.thread_id], timeout_seconds: 20 });
          assert.equal(waited.complete, true);
          assert.deepEqual(waited.threads.map(thread => thread.state).sort(), ['completed', 'failed']);
          const inspected = await call('get_thread', 'get', { thread_id: first.thread_id });
          assert.equal(inspected.thread.state, 'completed');
          const cancelled = await call('cancel_thread', 'cancel-terminal', { thread_id: first.thread_id });
          assert.equal(cancelled.state, 'completed');
        } else {
          assert.equal(tools.length, 6);
          const failed = prompt.includes('E2E_FAILURE');
          fs.writeFileSync(path.join(cwd, failed ? 'partial.txt' : 'success.txt'), 'Child output\n');
          const report = { summary: failed ? 'Partial result' : 'Completed result', tests: [{ command: 'fake check', status: failed ? 'failed' : 'passed' }], warnings: failed ? ['Partial result'] : [] };
          const first = await call('report_thread_result', 'report', report);
          assert.deepEqual(await call('report_thread_result', 'report', report), first);
          if (failed) throw new Error('Injected fake provider failure');
        }
      },
      abortCompaction() {}, abortRetry() {}, async abort() {}, dispose() {},
    },
    model: { provider: 'test', id: 'fake' }, piSessionFile: null,
  };
};
await sidecar.start();
"""


async def test_two_child_six_tool_node_python_round_trip(db, git_repo, tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the cross-runtime integration test")
    seed_parent_stack(db, git_repo)
    parent_id = "1" * 32
    db.execute("UPDATE sessions SET id = ? WHERE id = 'parent-session'", (parent_id,))
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    Path(git_repo, "dirty.txt").write_text("Parent uncommitted content\n")
    before = tuple(
        run_git(*args, cwd=git_repo)
        for args in (
            ("rev-parse", "HEAD"),
            ("status", "--porcelain=v1", "--untracked-files=all"),
            ("ls-files", "--stage"),
        )
    )
    socket_path = f"/tmp/yinshi-e2e-{uuid.uuid4().hex[:12]}.sock"
    module = Path(__file__).resolve().parents[2] / "sidecar/src/sidecar.js"
    process = await asyncio.create_subprocess_exec(
        node,
        "--input-type=module",
        "-e",
        _NODE_PEER,
        str(module),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SIDECAR_SOCKET_PATH": socket_path},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    events = []
    capabilities = []
    executions = []
    database_path = db.execute("PRAGMA database_list").fetchone()[2]

    async def executor(request, session_id, body):
        executions.append(session_id)
        run_id = get_active_prompt_run_id()
        operations = await service.query_operations(request, session_id=session_id, run_id=run_id)
        capability = generate_orchestration_capability(
            session_id,
            run_id=run_id,
            allowed_operations=operations,
            database_path=database_path,
        )
        capabilities.append(capability.token)
        client = SidecarClient()
        await client.connect(socket_path)
        workspace = db.execute(
            "SELECT w.path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()[0]
        try:
            async for event in client.query(
                session_id,
                body.prompt,
                cwd=workspace,
                orchestration_capability=capability,
                orchestration_handlers=build_thread_handlers(request, service),
            ):
                events.append(event)
                yield event.get("data", event) if event.get("type") == "message" else event
        finally:
            await client.disconnect()

    journal = PromptJournal(executor=executor, terminal_observer=service.observe_terminal)
    request.app.state.prompt_journal = journal
    try:
        assert process.stdout is not None
        assert (
            await asyncio.wait_for(process.stdout.readline(), timeout=10)
        ).decode().strip() == f"SOCKET_PATH={socket_path}"
        root = await journal.start(
            request=request,
            session_id=parent_id,
            idempotency_key=str(uuid.uuid4()),
            body=PromptRequest(prompt="E2E_ROOT"),
        )
        async with asyncio.timeout(40):
            while True:
                status = db.execute(
                    "SELECT status FROM prompt_runs WHERE id = ?", (root.id,)
                ).fetchone()[0]
                if status not in {"starting", "running", "stopping"}:
                    break
                await asyncio.sleep(0.05)
        assert status == "completed", events
        await service.reconcile(request)
        rows = db.execute("SELECT * FROM thread_delegations ORDER BY title").fetchall()
        assert len(rows) == 2
        assert {row["status"] for row in rows} == {"completed", "failed"}
        assert sorted(executions) == sorted([parent_id, *(row["child_session_id"] for row in rows)])
        assert db.execute("SELECT COUNT(*) FROM prompt_runs").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM thread_report_calls").fetchone()[0] == 2
        results = db.execute("SELECT * FROM thread_results ORDER BY summary").fetchall()
        assert len(results) == 2
        assert all(
            row["sealed"] == 1 and row["version"] == 1 and row["source"] == "reported"
            for row in results
        )
        assert {row["summary"] for row in results} == {"Completed result", "Partial result"}
        for row in results:
            assert run_git("rev-parse", row["result_ref"], cwd=git_repo) == row["result_commit"]
            assert len(json.loads(row["changed_files_json"])) == 1
        assert (before[0], before[2]) == tuple(
            run_git(*args, cwd=git_repo)
            for args in (
                ("rev-parse", "HEAD"),
                ("ls-files", "--stage"),
            )
        )
        assert all(row["git_artifacts_claimed"] == 1 for row in rows)
        status_lines = set(
            run_git("status", "--porcelain=v1", "--untracked-files=all", cwd=git_repo).splitlines()
        )
        assert status_lines == set(before[1].splitlines()) | {
            f"?? .worktrees/yinshi/thread-{row['id'][:8]}/" for row in rows
        }
        serialized = json.dumps(events)
        assert not any(token in serialized for token in capabilities)
        assert "orchestration_request" not in serialized
    finally:
        await journal.close()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.communicate(), timeout=10)
        Path(socket_path).unlink(missing_ok=True)
