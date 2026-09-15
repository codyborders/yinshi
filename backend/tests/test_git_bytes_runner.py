"""Raw-bytes Git runner behavior: timeout, reaping, and NUL preservation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from yinshi.exceptions import GitError


def init_repo(tmp_path):
    """Create one tiny git repository with a repository-local test identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Yinshi Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@yinshi.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "init"],
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


@pytest.mark.asyncio
async def test_run_git_bytes_enforces_stdout_bound(tmp_path) -> None:
    """Bounded mode returns exact bytes and rejects one-byte overflow."""
    from yinshi.services import git as git_service

    repo = init_repo(tmp_path)
    expected = await git_service.run_git_bytes(["rev-parse", "HEAD"], cwd=str(repo))
    assert (
        await git_service.run_git_bytes(
            ["rev-parse", "HEAD"],
            cwd=str(repo),
            stdout_bytes_max=len(expected),
        )
        == expected
    )
    with pytest.raises(GitError, match="git rev-parse output exceeded limit"):
        await git_service.run_git_bytes(
            ["rev-parse", "HEAD"],
            cwd=str(repo),
            stdout_bytes_max=len(expected) - 1,
        )


@pytest.mark.asyncio
async def test_run_git_bytes_enforces_stderr_bound(tmp_path) -> None:
    """Bounded mode rejects diagnostic overflow before returning Git failure."""
    from yinshi.services import git as git_service

    repo = init_repo(tmp_path)
    with pytest.raises(GitError, match="git show output exceeded limit"):
        await git_service.run_git_bytes(
            ["show", "missing-ref"],
            cwd=str(repo),
            stdout_bytes_max=1024,
            stderr_bytes_max=0,
        )


async def wait_for_pid(path) -> int:
    """Wait until a test child publishes its process ID."""
    for _ in range(200):
        if path.exists():
            return int(path.read_text(encoding="ascii"))
        await asyncio.sleep(0.005)
    raise AssertionError("child did not publish its process ID")


@pytest.mark.asyncio
async def test_bounded_overflow_reaps_live_process(monkeypatch, tmp_path) -> None:
    """Overflow stops and reaps a live process that writes beyond pipe capacity."""
    from yinshi.services import git as git_service

    pid_path = tmp_path / "overflow.pid"
    program = (
        "import os,sys\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True: os.write(1, b'x' * 65536)\n"
    )
    monkeypatch.setattr(git_service, "_GIT_EXECUTABLE_PATH", sys.executable)
    with pytest.raises(GitError, match="output exceeded limit"):
        await git_service.run_git_bytes(
            ["-c", program, str(pid_path)],
            stdout_bytes_max=1024,
        )
    pid = await wait_for_pid(pid_path)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_repeated_cancellation_reaps_bounded_live_process(monkeypatch, tmp_path) -> None:
    """Repeated cancellation cannot interrupt bounded child cleanup."""
    from yinshi.services import git as git_service

    pid_path = tmp_path / "cancel.pid"
    program = (
        "import os,sys,time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    monkeypatch.setattr(git_service, "_GIT_EXECUTABLE_PATH", sys.executable)
    task = asyncio.create_task(
        git_service.run_git_bytes(
            ["-c", program, str(pid_path)],
            stdout_bytes_max=1024,
        )
    )
    pid = await wait_for_pid(pid_path)
    task.cancel()
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_bounded_stdin_cancellation_reaps_child_that_does_not_read(
    monkeypatch, tmp_path
) -> None:
    """Cancellation stops a child while its stdin writer is blocked."""
    from yinshi.services import git as git_service

    pid_path = tmp_path / "stdin-cancel.pid"
    program = (
        "import os,sys,time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    monkeypatch.setattr(git_service, "_GIT_EXECUTABLE_PATH", sys.executable)
    task = asyncio.create_task(
        git_service.run_git_bytes(
            ["-c", program, str(pid_path)],
            stdin_bytes=b"x" * (8 * 1024 * 1024),
            stdout_bytes_max=1024,
        )
    )
    pid = await wait_for_pid(pid_path)
    task.cancel()
    assert isinstance(
        (await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError
    )
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_output_overflow_reaps_child_with_blocked_stdin(monkeypatch, tmp_path) -> None:
    """Output overflow stops a child even while caller input remains blocked."""
    from yinshi.services import git as git_service

    pid_path = tmp_path / "stdin-overflow.pid"
    program = (
        "import os,sys,time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "os.write(1, b'y' * 65536)\n"
        "while True: time.sleep(1)\n"
    )
    monkeypatch.setattr(git_service, "_GIT_EXECUTABLE_PATH", sys.executable)
    with pytest.raises(GitError, match="output exceeded limit"):
        await git_service.run_git_bytes(
            ["-c", program, str(pid_path)],
            stdin_bytes=b"x" * (8 * 1024 * 1024),
            stdout_bytes_max=1024,
        )
    pid = await wait_for_pid(pid_path)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_explicit_stderr_bound_applies_without_stdout_bound(monkeypatch) -> None:
    """Explicit stderr limits work while stdout remains intentionally unbounded."""
    from yinshi.services import git as git_service

    monkeypatch.setattr(git_service, "_GIT_EXECUTABLE_PATH", sys.executable)
    with pytest.raises(GitError, match="output exceeded limit"):
        await git_service.run_git_bytes(
            ["-c", "import os; os.write(2, b'x' * 65536)"],
            stderr_bytes_max=1024,
        )


@pytest.mark.asyncio
async def test_run_git_bytes_sends_exact_stdin(tmp_path) -> None:
    """Binary stdin reaches Git unchanged in bounded and unbounded modes."""
    from yinshi.services import git as git_service

    repo = init_repo(tmp_path)
    expected = await git_service.run_git_bytes(
        ["hash-object", "--stdin"],
        cwd=str(repo),
        stdin_bytes=b"binary\x00content",
    )
    assert (
        await git_service.run_git_bytes(
            ["hash-object", "--stdin"],
            cwd=str(repo),
            stdin_bytes=b"binary\x00content",
            stdout_bytes_max=len(expected),
        )
        == expected
    )


@pytest.mark.asyncio
async def test_run_git_bytes_accepts_declared_nonzero_status(tmp_path) -> None:
    """Callers can inspect commands with one declared nonzero outcome."""
    from yinshi.services import git as git_service

    repo = init_repo(tmp_path)
    git_head = (
        (await git_service.run_git_bytes(["rev-parse", "HEAD"], cwd=str(repo)))
        .decode("ascii")
        .strip()
    )
    await git_service.run_git_bytes(
        ["checkout", "--detach", "-q", git_head],
        cwd=str(repo),
    )
    assert (
        await git_service.run_git_bytes(
            ["symbolic-ref", "--quiet", "HEAD"],
            cwd=str(repo),
            stdout_bytes_max=128,
            accepted_returncodes=(0, 1),
        )
        == b""
    )


@pytest.mark.asyncio
async def test_run_git_bytes_rejects_invalid_output_bounds() -> None:
    """Output limits require exact nonnegative integers."""
    from yinshi.services import git as git_service

    for value in (-1, True):
        with pytest.raises(ValueError):
            await git_service.run_git_bytes(["status"], stdout_bytes_max=value)
        with pytest.raises(ValueError):
            await git_service.run_git_bytes(["status"], stderr_bytes_max=value)
    for value in ("bytes", bytearray(b"bytes"), memoryview(b"bytes")):
        with pytest.raises(TypeError):
            await git_service.run_git_bytes(["status"], stdin_bytes=value)  # type: ignore[arg-type]
    for codes in ((), (True,), (-1,), (256,)):
        with pytest.raises(ValueError):
            await git_service.run_git_bytes(["status"], accepted_returncodes=codes)
