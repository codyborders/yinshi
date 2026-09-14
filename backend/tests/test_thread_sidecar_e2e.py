"""Exercise all six tools across the real Node/Python transport without credentials."""

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from tests.test_thread_orchestration import seed_parent_stack
from tests.test_thread_workspaces import run_git
from yinshi.api.deps import get_tenant, request_database_identity
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
let markPartialReady;
let markRestartReady;
const partialReady = new Promise(resolve => { markPartialReady = resolve; });
const restartReady = new Promise(resolve => { markRestartReady = resolve; });
sidecar._createPiSession = async (...args) => {
  const tools = Array.isArray(args.at(-1)) ? args.at(-1) : [];
  const cwd = args[3];
  let emit;
  let rejectBlockedPrompt;
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
        if (prompt === 'E2E_DISABLED') {
          assert.equal(tools.length, 1);
          assert.equal(tools[0].name, 'thread_bridge_ping');
        } else if (prompt === 'E2E_RESTART') {
          assert.equal(tools.length, 5);
          await call('spawn_thread', 'spawn-restart', { title: 'Restarted', task: 'E2E_RESTART_CHILD' });
          await restartReady;
          await new Promise((resolve, reject) => { rejectBlockedPrompt = reject; });
        } else if (prompt === 'E2E_SMOKE') {
          assert.equal(tools.length, 5);
          const child = await call('spawn_thread', 'spawn-smoke', { title: 'Smoke', task: 'E2E_SMOKE_CHILD' });
          const waited = await call('wait_for_threads', 'wait-smoke', { thread_ids: [child.thread_id], timeout_seconds: 20 });
          assert.equal(waited.complete, true);
          assert.equal(waited.threads[0].state, 'completed');
        } else if (prompt === 'E2E_ROOT') {
          assert.equal(tools.length, 5);
          assert.ok(!tools.some(tool => tool.name === 'report_thread_result'));
          const [first, second] = await Promise.all([
            call('spawn_thread', 'spawn-success', { title: 'Success', task: 'E2E_SUCCESS' }),
            call('spawn_thread', 'spawn-cancel', { title: 'Cancelled', task: 'E2E_CANCEL' }),
          ]);
          const replay = await call('spawn_thread', 'spawn-success', { title: 'Success', task: 'E2E_SUCCESS' });
          assert.equal(first.thread_id, replay.thread_id);
          await partialReady;
          const listed = await call('list_children', 'list', {});
          assert.equal(listed.children.length, 2);
          const cancelled = await call('cancel_thread', 'cancel-running', { thread_id: second.thread_id });
          assert.ok(['cancellation_requested', 'cancelled'].includes(cancelled.state));
          const waited = await call('wait_for_threads', 'wait', { thread_ids: [first.thread_id, second.thread_id], timeout_seconds: 20 });
          assert.equal(waited.complete, true);
          assert.deepEqual(waited.threads.map(thread => thread.state).sort(), ['cancelled', 'completed']);
          const inspected = await call('get_thread', 'get', { thread_id: first.thread_id });
          assert.equal(inspected.thread.state, 'completed');
        } else {
          assert.equal(tools.length, 6);
          const cancelled = prompt.includes('E2E_CANCEL');
          const restarting = prompt.includes('E2E_RESTART_CHILD');
          const outputName = cancelled ? 'partial.txt' : restarting ? 'restart-partial.txt' : 'success.txt';
          fs.writeFileSync(path.join(cwd, outputName), 'Child output\n');
          if (restarting) {
            const report = { summary: 'Restart partial', tests: [], warnings: [] };
            await call('report_thread_result', 'report-restart', report);
            markRestartReady();
            await new Promise((resolve, reject) => { rejectBlockedPrompt = reject; });
            return;
          }
          if (cancelled) {
            markPartialReady();
            await new Promise((resolve, reject) => { rejectBlockedPrompt = reject; });
            return;
          }
          const report = { summary: 'Completed result', tests: [{ command: 'fake check', status: 'passed' }], warnings: [] };
          const first = await call('report_thread_result', 'report', report);
          assert.deepEqual(await call('report_thread_result', 'report', report), first);
        }
      },
      abortCompaction() {},
      abortRetry() {},
      async abort() {
        if (rejectBlockedPrompt) rejectBlockedPrompt(new Error('Cancelled by parent'));
      },
      dispose() {},
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
    from yinshi.main import create_app

    application = create_app(mode="desktop")
    service = ThreadOrchestrationService()
    events = []
    capabilities = []
    executions = []

    async def executor(request, session_id, body):
        executions.append(session_id)
        run_id = get_active_prompt_run_id()
        operations = await service.query_operations(request, session_id=session_id, run_id=run_id)
        tenant = get_tenant(request)
        capability = generate_orchestration_capability(
            session_id,
            run_id=run_id,
            tenant_id=tenant.user_id if tenant is not None else None,
            allowed_operations=operations,
            database_path=request_database_identity(request),
        )
        capabilities.append(capability.token)
        client = SidecarClient()
        await client.connect(socket_path)
        workspace = db.execute(
            "SELECT w.path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()[0]
        try:
            handlers = None
            if operations != frozenset({"ping_thread_bridge"}):
                handlers = build_thread_handlers(
                    request,
                    service,
                    runtime_ready=lambda: client.connected,
                )
            async for event in client.query(
                session_id,
                body.prompt,
                cwd=workspace,
                orchestration_capability=capability,
                orchestration_handlers=handlers,
            ):
                events.append(event)
                yield event.get("data", event) if event.get("type") == "message" else event
        except Exception as exc:
            events.append({"executor_error": repr(exc)})
            raise
        finally:
            await client.disconnect()

    journal = PromptJournal(executor=executor, terminal_observer=service.observe_terminal)
    application.state.prompt_journal = journal
    try:
        assert process.stdout is not None
        assert (
            await asyncio.wait_for(process.stdout.readline(), timeout=10)
        ).decode().strip() == f"SOCKET_PATH={socket_path}"
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            started = await client.post(
                f"/api/sessions/{parent_id}/runs",
                json={"prompt": "E2E_ROOT", "idempotency_key": str(uuid.uuid4())},
            )
            assert started.status_code == 202, started.text
            root_id = started.json()["id"]
            async with asyncio.timeout(40):
                while True:
                    response = await client.get(
                        f"/api/sessions/{parent_id}/runs/{root_id}/events/0"
                    )
                    assert response.status_code == 200, response.text
                    status = response.json()["status"]
                    if status not in {"starting", "running", "stopping"}:
                        break
                    await asyncio.sleep(0.05)
            assert status == "completed", events
            monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "false")
            get_settings.cache_clear()
            disabled = await client.post(
                f"/api/sessions/{parent_id}/runs",
                json={"prompt": "E2E_DISABLED", "idempotency_key": str(uuid.uuid4())},
            )
            assert disabled.status_code == 202, disabled.text
            disabled_id = disabled.json()["id"]
            async with asyncio.timeout(20):
                while True:
                    response = await client.get(
                        f"/api/sessions/{parent_id}/runs/{disabled_id}/events/0"
                    )
                    assert response.status_code == 200, response.text
                    disabled_body = response.json()
                    disabled_status = disabled_body["status"]
                    if disabled_status not in {"starting", "running", "stopping"}:
                        break
                    await asyncio.sleep(0.05)
            assert disabled_status == "completed", (disabled_body, events[-2:])
        assert status == "completed", (
            events,
            [dict(row) for row in db.execute("SELECT * FROM thread_delegations").fetchall()],
        )
        rows = db.execute(
            "SELECT d.*, w.path AS workspace_path FROM thread_delegations d "
            "JOIN sessions s ON s.id = d.child_session_id "
            "JOIN workspaces w ON w.id = s.workspace_id ORDER BY d.title"
        ).fetchall()
        assert len(rows) == 2
        assert {row["status"] for row in rows} == {"cancelled", "completed"}
        assert sorted(executions) == sorted(
            [parent_id, parent_id, *(row["child_session_id"] for row in rows)]
        )
        assert db.execute("SELECT COUNT(*) FROM prompt_runs").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM thread_report_calls").fetchone()[0] == 1
        results = db.execute("SELECT * FROM thread_results ORDER BY summary").fetchall()
        assert len(results) == 2
        results_by_delegation = {row["delegation_id"]: row for row in results}
        assert all(row["sealed"] == 1 and row["version"] == 1 for row in results)
        assert (
            results_by_delegation[next(row["id"] for row in rows if row["title"] == "Success")][
                "source"
            ]
            == "reported"
        )
        assert (
            results_by_delegation[next(row["id"] for row in rows if row["title"] == "Success")][
                "summary"
            ]
            == "Completed result"
        )
        assert (
            results_by_delegation[next(row["id"] for row in rows if row["title"] == "Cancelled")][
                "source"
            ]
            == "derived"
        )
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
        by_title = {row["title"]: row for row in rows}
        success_path = Path(by_title["Success"]["workspace_path"])
        partial_path = Path(by_title["Cancelled"]["workspace_path"])
        assert (success_path / "success.txt").read_text() == "Child output\n"
        assert not (success_path / "partial.txt").exists()
        assert (partial_path / "partial.txt").read_text() == "Child output\n"
        assert not (partial_path / "success.txt").exists()
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

        monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
        get_settings.cache_clear()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            restart = await client.post(
                f"/api/sessions/{parent_id}/runs",
                json={"prompt": "E2E_RESTART", "idempotency_key": str(uuid.uuid4())},
            )
            assert restart.status_code == 202, restart.text
            async with asyncio.timeout(30):
                while True:
                    restart_child = db.execute(
                        "SELECT d.*, s.workspace_id, w.path AS workspace_path "
                        "FROM thread_delegations d "
                        "JOIN sessions s ON s.id = d.child_session_id "
                        "JOIN workspaces w ON w.id = s.workspace_id "
                        "WHERE d.title = 'Restarted'"
                    ).fetchone()
                    if restart_child is not None:
                        child_run = db.execute(
                            "SELECT id, status FROM prompt_runs WHERE session_id = ?",
                            (restart_child["child_session_id"],),
                        ).fetchone()
                        restart_file = Path(restart_child["workspace_path"]) / "restart-partial.txt"
                        if (
                            child_run is not None
                            and child_run["status"] == "running"
                            and restart_file.exists()
                            and db.execute(
                                "SELECT COUNT(*) FROM thread_report_calls WHERE delegation_id = ?",
                                (restart_child["id"],),
                            ).fetchone()[0]
                            == 1
                        ):
                            break
                    await asyncio.sleep(0.05)
            assert restart_child is not None
            assert child_run is not None
            before_restart = await client.get(
                f"/api/sessions/{restart_child['child_session_id']}/runs/{child_run['id']}/events/0"
            )
            assert before_restart.status_code == 200, before_restart.text
            before_restart_events = before_restart.json()["events"]
            assert before_restart_events

        await journal.close()
        restarted_journal = PromptJournal(
            executor=executor,
            terminal_observer=service.observe_terminal,
        )
        journal = restarted_journal
        application.state.prompt_journal = restarted_journal
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            recovered = await client.get(
                f"/api/sessions/{restart_child['child_session_id']}/runs/{child_run['id']}/events/0"
            )
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["status"] == "interrupted"
            assert recovered.json()["events"][: len(before_restart_events)] == before_restart_events
            assert (
                db.execute(
                    "SELECT status FROM thread_delegations WHERE id = ?",
                    (restart_child["id"],),
                ).fetchone()[0]
                == "interrupted"
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM prompt_runs WHERE session_id = ?",
                    (restart_child["child_session_id"],),
                ).fetchone()[0]
                == 1
            )
            assert executions.count(restart_child["child_session_id"]) == 1
            child_refs = run_git(
                "for-each-ref",
                "--format=%(refname)",
                f"refs/yinshi/*/{restart_child['id']}",
                cwd=git_repo,
            ).splitlines()
            assert child_refs
            first_delete = await client.delete(f"/api/workspaces/{restart_child['workspace_id']}")
            second_delete = await client.delete(f"/api/workspaces/{restart_child['workspace_id']}")
            assert first_delete.status_code == 204, (
                first_delete.text,
                run_git(
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    f"refs/yinshi/*/{restart_child['id']}",
                    cwd=git_repo,
                ),
            )
            assert second_delete.status_code == 404, second_delete.text
        assert not Path(restart_child["workspace_path"]).exists()
        assert (
            run_git(
                "for-each-ref",
                "--format=%(refname)",
                f"refs/yinshi/*/{restart_child['id']}",
                cwd=git_repo,
            )
            == ""
        )
        assert (before[0], before[2]) == tuple(
            run_git(*args, cwd=git_repo)
            for args in (("rev-parse", "HEAD"), ("ls-files", "--stage"))
        )
    finally:
        await journal.close()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.communicate(), timeout=10)
        Path(socket_path).unlink(missing_ok=True)
