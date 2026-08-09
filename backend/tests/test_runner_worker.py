"""Verify runner-local worker storage and tenant binding.

The manager derives stable local secrets from the runner identity, creates one
opaque account directory, and refuses capabilities for another account.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from yinshi.runner_worker import RunnerWorkerManager


@pytest.mark.asyncio
async def test_runner_worker_manager_reuses_one_encrypted_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Worker dispatch persists repository state under an opaque account directory."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "stale-hosted-setting")
    manager = RunnerWorkerManager(
        data_directory=tmp_path / "runner",
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )

    first = manager.dispatcher("account/with/path-characters")
    second = manager.dispatcher("account/with/path-characters")
    response = await first.request(method="GET", path="/api/repos", body=None)

    assert first is second
    assert response.status_code == 200
    assert response.body == []
    assert first.user_id == "account/with/path-characters"
    assert Path(first.data_directory).is_relative_to(tmp_path / "runner" / "users")
    assert "account" not in Path(first.data_directory).name
    assert Path(first.database_path).is_file()
    assert Path(first.database_path).stat().st_mode & 0o777 == 0o600


def test_runner_worker_manager_separates_sqlite_and_shared_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Configured storage profiles place databases and repositories in distinct roots."""
    manager = RunnerWorkerManager(
        data_directory=tmp_path / "state",
        database_directory=tmp_path / "sqlite",
        user_data_directory=tmp_path / "shared" / "users",
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )

    dispatcher = manager.dispatcher("account-1")

    assert Path(dispatcher.database_path).is_relative_to(tmp_path / "sqlite")
    assert Path(dispatcher.data_directory).is_relative_to(tmp_path / "shared" / "users")
    assert not Path(dispatcher.database_path).is_relative_to(Path(dispatcher.data_directory))


def test_runner_worker_manager_persists_account_binding_across_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """A restarted runner remains bound to the first account identity."""
    data_directory = tmp_path / "runner"
    first_manager = RunnerWorkerManager(
        data_directory=data_directory,
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )
    first_manager.dispatcher("account-1")

    restarted_manager = RunnerWorkerManager(
        data_directory=data_directory,
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )
    restarted_dispatcher = restarted_manager.dispatcher("account-1")
    binding_path = data_directory / "account.binding"

    assert restarted_dispatcher.user_id == "account-1"
    assert binding_path.read_text(encoding="ascii") != "account-1"
    assert binding_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="different account"):
        RunnerWorkerManager(
            data_directory=data_directory,
            runner_static_private_key=b"r" * 32,
            environment_setter=monkeypatch.setenv,
        ).dispatcher("account-2")


def test_runner_worker_manager_recovers_interrupted_prompt_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Runner restart marks orphaned runs interrupted and releases sessions."""
    from yinshi.tenant import get_user_db

    data_directory = tmp_path / "runner-recovery"
    manager = RunnerWorkerManager(
        data_directory=data_directory,
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )
    dispatcher = manager.dispatcher("account-1")
    repository_id = "a" * 32
    workspace_id = "b" * 32
    session_id = "c" * 32
    run_id = "d" * 32
    with get_user_db(dispatcher.tenant) as worker_db:
        worker_db.execute(
            "INSERT INTO repos (id, name, root_path) VALUES (?, 'repo', '/tmp/repo')",
            (repository_id,),
        )
        worker_db.execute(
            """INSERT INTO workspaces (id, repo_id, name, branch, path)
               VALUES (?, ?, 'workspace', 'branch', '/tmp/workspace')""",
            (workspace_id, repository_id),
        )
        worker_db.execute(
            """INSERT INTO sessions (id, workspace_id, status)
               VALUES (?, ?, 'running')""",
            (session_id, workspace_id),
        )
        worker_db.execute(
            """INSERT INTO prompt_runs
               (id, session_id, idempotency_key, status)
               VALUES (?, ?, '22222222-2222-4222-8222-222222222222', 'running')""",
            (run_id, session_id),
        )
        worker_db.commit()

    restarted_manager = RunnerWorkerManager(
        data_directory=data_directory,
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )
    restarted_dispatcher = restarted_manager.dispatcher("account-1")
    with get_user_db(restarted_dispatcher.tenant) as worker_db:
        run = worker_db.execute(
            "SELECT status FROM prompt_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        session = worker_db.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        event = worker_db.execute(
            "SELECT sequence, event_json FROM prompt_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert run["status"] == "interrupted"
    assert session["status"] == "idle"
    assert event["sequence"] == 0
    assert '"type":"error"' in event["event_json"]


def test_runner_worker_manager_rejects_a_second_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """One registered runner cannot be multiplexed across account identities."""
    manager = RunnerWorkerManager(
        data_directory=tmp_path / "runner",
        runner_static_private_key=b"r" * 32,
        environment_setter=monkeypatch.setenv,
    )
    manager.dispatcher("account-1")

    with pytest.raises(ValueError, match="different account"):
        manager.dispatcher("account-2")
