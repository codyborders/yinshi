"""Raw-bytes Git runner behavior: timeout, reaping, and NUL preservation."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from yinshi.exceptions import GitError


def init_repo(tmp_path):
    """Create one tiny git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    return repo


@pytest.mark.asyncio
async def test_run_git_bytes_timeout_kills_and_drains_child(monkeypatch, tmp_path):
    """Timed-out bytes runner should kill and drain before failing."""
    from yinshi.services import git as git_service

    calls: list[str] = []
    init_repo(tmp_path)

    class FakeProcess:
        returncode: int | None = None
        communication_count = 0

        async def communicate(self):
            self.communication_count += 1
            calls.append("communicate")
            if self.communication_count == 1:
                await asyncio.Event().wait()
            calls.append("drained")
            return b"", b""

        def kill(self) -> None:
            calls.append("kill")
            self.returncode = -9

    async def fake_create_subprocess_exec(*_command, **_options):
        return FakeProcess()

    monkeypatch.setattr(git_service, "_GIT_COMMAND_TIMEOUT_S", 0.01)
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(GitError, match="git status timed out"):
        await git_service.run_git_bytes(["status"], cwd=str(tmp_path))

    assert calls == ["communicate", "kill", "communicate", "drained"]


@pytest.mark.asyncio
async def test_run_git_bytes_cancellation_reaps_child(monkeypatch, tmp_path):
    """Cancelling the bytes runner should reap its child before propagating."""
    from yinshi.services import git as git_service

    init_repo(tmp_path)
    communication_started = asyncio.Event()
    calls: list[str] = []

    class FakeProcess:
        returncode: int | None = None
        communication_count = 0

        async def communicate(self):
            self.communication_count += 1
            calls.append("communicate")
            if self.communication_count == 1:
                communication_started.set()
                await asyncio.Event().wait()
            calls.append("drained")
            return b"", b""

        def kill(self) -> None:
            calls.append("kill")
            self.returncode = -9

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_command, **_options):
        return process

    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    task = asyncio.create_task(git_service.run_git_bytes(["status"]))
    await communication_started.wait()
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert calls == ["communicate", "kill", "communicate", "drained"]
