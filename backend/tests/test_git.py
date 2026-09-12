"""Tests for git service operations."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import GitError


def test_generate_branch_name():
    """Branch names should follow adjective-noun-suffix pattern."""
    from yinshi.services.git import generate_branch_name

    name = generate_branch_name()
    parts = name.split("-")
    assert len(parts) == 3
    assert len(parts[2]) == 4


def test_generate_branch_name_with_username():
    """Branch names should be prefixed with username/ when provided."""
    from yinshi.services.git import generate_branch_name

    name = generate_branch_name(username="codyborders")
    assert name.startswith("codyborders/")
    # The part after the prefix should still be adjective-noun-suffix
    bare = name.split("/", 1)[1]
    parts = bare.split("-")
    assert len(parts) == 3
    assert len(parts[2]) == 4


def test_generate_branch_name_unique():
    """Branch names should be unique across calls."""
    from yinshi.services.git import generate_branch_name

    names = {generate_branch_name() for _ in range(50)}
    assert len(names) == 50


def test_validate_clone_url_rejects_ext_scheme():
    """ext:: URLs should be rejected."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError, match="URL scheme not allowed"):
        _validate_clone_url("ext::sh -c evil")


def test_validate_clone_url_rejects_file_scheme():
    """file:// URLs should be rejected."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError, match="URL scheme not allowed"):
        _validate_clone_url("file:///etc/passwd")


def test_validate_clone_url_rejects_argument_injection():
    """URLs starting with - should be rejected."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError, match="Invalid repository URL"):
        _validate_clone_url("--upload-pack=evil")


def test_validate_clone_url_rejects_unknown_scheme():
    """Unknown URL schemes should be rejected."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError, match="GitHub HTTPS"):
        _validate_clone_url("ftp://example.com/repo.git")


def test_validate_clone_url_allows_https():
    """https:// URLs should be allowed."""
    from yinshi.services.git import _validate_clone_url

    _validate_clone_url("https://github.com/user/repo.git")


@pytest.mark.parametrize(
    "remote_url",
    [
        "ssh://git@github.com/user/repo.git",
        "git@github.com:user/repo.git",
        "https://127.0.0.1/repo.git",
        "https://example.com/user/repo.git",
        "https://token@github.com/user/repo.git",
        "https://github.com:8443/user/repo.git",
    ],
)
def test_validate_clone_url_rejects_noncanonical_destinations(remote_url):
    """Host-side cloning should accept only canonical GitHub HTTPS remotes."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError, match="GitHub HTTPS"):
        _validate_clone_url(remote_url)


@pytest.mark.asyncio
async def test_run_git_uses_immutable_binary_without_ambient_credentials(monkeypatch):
    """Host Git must not inherit service credentials or user-level Git state."""
    from yinshi.services import git as git_service

    captured: dict[str, object] = {}

    class FakeProcess:
        """Return a successful subprocess result without executing Git."""

        returncode = 0

        async def communicate(self):
            return b"ok\n", b""

    async def fake_create_subprocess_exec(*command, **options):
        captured["command"] = command
        captured["environment"] = options["env"]
        return FakeProcess()

    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ambient-agent.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-provider-secret")
    monkeypatch.setenv("HOME", "/tmp/ambient-home")
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    output = await git_service._run_git(["status", "--short"])

    assert output == "ok"
    assert captured["command"][:2] == ("/usr/bin/git", "status")
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["HOME"] == "/nonexistent"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "SSH_AUTH_SOCK" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.asyncio
async def test_run_git_cancellation_reaps_child_before_returning(monkeypatch):
    """Cancelling Git should kill and reap its child before cancellation returns."""
    from yinshi.services import git as git_service

    communication_started = asyncio.Event()
    calls: list[str] = []

    class FakeProcess:
        """Keep one child active until cancellation cleanup kills it."""

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

    task = asyncio.create_task(git_service._run_git(["status"]))
    await communication_started.wait()
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert calls == ["communicate", "kill", "communicate", "drained"]


@pytest.mark.asyncio
async def test_run_git_timeout_kills_and_drains_child(monkeypatch):
    """Timed-out Git should kill and drain its child before reporting failure."""
    from yinshi.services import git as git_service

    calls: list[str] = []

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
        await git_service._run_git(["status"])

    assert calls == ["communicate", "kill", "communicate", "drained"]


@pytest.mark.asyncio
async def test_validate_local_repo(git_repo):
    """Should validate a real git repo."""
    from yinshi.services.git import validate_local_repo

    assert await validate_local_repo(git_repo) is True


@pytest.mark.asyncio
async def test_validate_local_repo_invalid(tmp_path):
    """Should reject a non-git directory."""
    from yinshi.services.git import validate_local_repo

    assert await validate_local_repo(str(tmp_path)) is False


@pytest.mark.asyncio
async def test_restore_worktree_populates_owned_empty_directory(git_repo, tmp_path):
    """Worktree restore must preserve a publication owner's precreated stage inode."""
    from yinshi.services.git import restore_worktree

    target = tmp_path / "target-worktree"
    target.mkdir(mode=0o700)
    identity = target.stat()

    await restore_worktree(git_repo, str(target), "feature-owned-stage")

    resulting_identity = target.stat()
    assert (resulting_identity.st_dev, resulting_identity.st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    assert (target / ".git").is_file()


@pytest.mark.asyncio
async def test_clone_local_repo_rejects_preexisting_empty_destination(git_repo, tmp_path):
    """General local clone must not adopt storage created by another owner."""
    from yinshi.services.git import clone_local_repo

    target = tmp_path / "foreign-empty"
    target.mkdir()
    with pytest.raises(GitError, match="Destination already exists"):
        await clone_local_repo(git_repo, str(target))


@pytest.mark.asyncio
async def test_clone_repo_rejects_preexisting_empty_destination(tmp_path):
    """General remote clone must not adopt storage created by another owner."""
    from yinshi.services.git import clone_repo

    target = tmp_path / "foreign-empty"
    target.mkdir()
    with pytest.raises(GitError, match="Destination already exists"):
        await clone_repo("https://github.com/acme/repo", str(target))


@pytest.mark.asyncio
async def test_owned_clone_refuses_preexisting_valid_destination(git_repo, tmp_path):
    """Managed storage acquisition cannot adopt an existing clone."""
    from yinshi.services.git import clone_local_repo

    target = tmp_path / "existing"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", git_repo, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(GitError, match="Clone destination already exists"):
        await clone_local_repo(
            git_repo,
            str(target),
            destination_must_be_absent=True,
        )
    assert (target / ".git").is_dir()


@pytest.mark.asyncio
async def test_clone_repo_reuses_existing_clone_when_git_suffix_differs(
    tmp_path,
    monkeypatch,
):
    """Existing clones should be reused when URLs differ only by an optional .git suffix."""
    from yinshi.services import git as git_service

    dest_path = tmp_path / "existing-clone"
    dest_path.mkdir()

    async def fake_validate_local_repo(path: str) -> bool:
        assert path == str(dest_path)
        return True

    async def fake_run_git(args, cwd=None, env=None):
        del env
        assert cwd == str(dest_path)
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/yinshi.git"
        if args == ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"]:
            return "refs/remotes/origin/main\n"
        if args == ["fetch", "--all"]:
            return ""
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(git_service, "validate_local_repo", fake_validate_local_repo)
    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    result = await git_service.clone_repo(
        "https://github.com/acme/yinshi",
        str(dest_path),
    )

    assert result == str(dest_path)


@pytest.mark.asyncio
async def test_clone_repo_rejects_incomplete_existing_clone_when_refresh_fails(
    tmp_path,
    monkeypatch,
):
    """Incomplete existing clones should not be silently reused after a failed refresh."""
    from yinshi.services import git as git_service

    dest_path = tmp_path / "partial-clone"
    dest_path.mkdir()

    async def fake_validate_local_repo(path: str) -> bool:
        assert path == str(dest_path)
        return True

    async def fake_run_git(args, cwd=None, env=None):
        del env
        assert cwd == str(dest_path)
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/yinshi.git"
        if args == ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"]:
            return ""
        if args == ["fetch", "--all"]:
            raise GitError("git fetch failed")
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(git_service, "validate_local_repo", fake_validate_local_repo)
    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    with pytest.raises(GitError, match="incomplete"):
        await git_service.clone_repo(
            "https://github.com/acme/yinshi",
            str(dest_path),
        )


@pytest.mark.asyncio
async def test_clone_repo_accepts_existing_clone_of_empty_remote(
    tmp_path,
    monkeypatch,
):
    """A fetched clone whose matching origin has no refs is a valid empty remote."""
    from yinshi.services import git as git_service

    dest_path = tmp_path / "empty-clone"
    dest_path.mkdir()

    async def fake_validate_local_repo(path: str) -> bool:
        assert path == str(dest_path)
        return True

    async def fake_run_git(args, cwd=None, env=None):
        del env
        assert cwd == str(dest_path)
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/yinshi.git"
        if args == ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"]:
            return ""
        if args == ["fetch", "--all"]:
            return ""
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(git_service, "validate_local_repo", fake_validate_local_repo)
    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    result = await git_service.clone_repo(
        "https://github.com/acme/yinshi",
        str(dest_path),
    )

    assert result == str(dest_path)


@pytest.mark.asyncio
async def test_create_and_delete_worktree(git_repo, tmp_path):
    """Should create and delete a worktree."""
    from yinshi.services.git import create_worktree, delete_worktree

    wt_path = str(tmp_path / "worktrees" / "test-branch")
    result = await create_worktree(git_repo, wt_path, "test-branch")
    assert result == wt_path
    assert os.path.isdir(wt_path)

    await delete_worktree(git_repo, wt_path)
    assert not os.path.isdir(wt_path)


@pytest.mark.asyncio
async def test_cleanup_repository_worktrees_removes_metadata_and_branches(
    git_repo,
    tmp_path,
):
    """Committed repository cleanup should remove every selected worktree and branch."""
    from yinshi.services import git as git_service

    worktrees: list[tuple[str, str]] = []
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    for index in range(3):
        branch = f"cleanup-branch-{index}"
        worktree_path = tmp_path / "worktrees" / branch
        await git_service.create_worktree(git_repo, str(worktree_path), branch)
        os.rename(worktree_path, quarantine / branch)
        worktrees.append((str(worktree_path), branch))

    await git_service.cleanup_repository_worktrees(git_repo, worktrees)

    listed_worktrees = await git_service._run_git(
        ["worktree", "list", "--porcelain"],
        cwd=git_repo,
    )
    listed_branches = await git_service._run_git(
        ["for-each-ref", "--format=%(refname)", "refs/heads"],
        cwd=git_repo,
    )
    assert all(worktree_path not in listed_worktrees for worktree_path, _branch in worktrees)
    assert all(f"refs/heads/{branch}" not in listed_branches for _path, branch in worktrees)


@pytest.mark.asyncio
async def test_create_worktree_has_files(git_repo, tmp_path):
    """Worktree should contain the repo's files."""
    from yinshi.services.git import create_worktree

    wt_path = str(tmp_path / "worktrees" / "file-test")
    await create_worktree(git_repo, wt_path, "file-test")
    assert os.path.isfile(os.path.join(wt_path, "README.md"))


@pytest.mark.asyncio
async def test_resolve_remote_base_ref_prefers_origin_head(git_repo, monkeypatch):
    """Remote worktrees should branch from the fetched origin HEAD when available."""
    from yinshi.services import git as git_service

    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []

    async def fake_run_git(args, cwd=None, env=None):
        calls.append((args, cwd, env))
        if args == ["fetch", "origin"]:
            return ""
        if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    result = await git_service.resolve_remote_base_ref(git_repo)

    assert result == "origin/main"
    assert calls[0][0] == ["fetch", "origin"]
    assert calls[1][0] == ["symbolic-ref", "refs/remotes/origin/HEAD"]


@pytest.mark.asyncio
async def test_create_worktree_uses_explicit_base_ref(git_repo, tmp_path, monkeypatch):
    """Remote worktree creation should pass the resolved base ref to git worktree add."""
    from yinshi.services import git as git_service

    recorded_args: list[str] = []

    async def fake_run_git(args, cwd=None, env=None):
        del env
        assert cwd == git_repo
        recorded_args[:] = args
        return ""

    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    wt_path = str(tmp_path / "worktrees" / "remote-base")
    await git_service.create_worktree(
        git_repo,
        wt_path,
        "remote-base",
        base_ref="origin/main",
    )

    assert recorded_args == [
        "worktree",
        "add",
        "-b",
        "remote-base",
        wt_path,
        "origin/main",
    ]


@pytest.mark.asyncio
async def test_run_git_binds_cwd_before_a_name_replacement(tmp_path, monkeypatch):
    """A replaced cwd name must not redirect Git into the replacement directory."""
    from yinshi.services import git as git_service

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    accepted = checkout.stat()
    moved = tmp_path / "accepted-checkout"

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok\n", b""

    async def fake_create_subprocess_exec(*_command, **options):
        checkout.rename(moved)
        checkout.mkdir()
        descriptors = options["pass_fds"]
        assert len(descriptors) == 1
        opened = os.fstat(descriptors[0])
        assert (opened.st_dev, opened.st_ino) == (accepted.st_dev, accepted.st_ino)
        assert options["cwd"] == f"/proc/self/fd/{descriptors[0]}"
        effect = os.open("effect", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=descriptors[0])
        os.close(effect)
        return FakeProcess()

    monkeypatch.setattr(git_service.sys, "platform", "linux")
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(GitError, match="working directory changed"):
        await git_service._run_git(["status"], cwd=str(checkout))

    assert (moved / "effect").is_file()
    assert not (checkout / "effect").exists()


@pytest.mark.asyncio
async def test_clone_repo_populates_its_retained_destination(tmp_path, monkeypatch):
    """A destination name replacement must not redirect clone output."""
    from yinshi.services import git as git_service

    destination = tmp_path / "checkout"
    moved = tmp_path / "accepted-checkout"

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command, **options):
        assert command[-3:] == ("clone", "https://github.com/acme/repo", ".")
        destination.rename(moved)
        destination.mkdir()
        cwd_descriptor = options["pass_fds"][0]
        effect = os.open("clone-output", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=cwd_descriptor)
        os.close(effect)
        return FakeProcess()

    monkeypatch.setattr(git_service.sys, "platform", "linux")
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(GitError, match="working directory changed"):
        await git_service.clone_repo("https://github.com/acme/repo", str(destination))

    assert (moved / "clone-output").is_file()
    assert not (destination / "clone-output").exists()


@pytest.mark.asyncio
async def test_create_worktree_binds_target_before_git_effect(tmp_path, monkeypatch):
    """Worktree creation must populate the accepted target, not its replacement name."""
    from yinshi.services import git as git_service

    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "feature"
    moved = tmp_path / "accepted-worktree"

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command, **options):
        assert command[-1] == "origin/main"
        target.rename(moved)
        target.mkdir()
        descriptor_paths = [value for value in command if value.startswith("/proc/self/fd/")]
        assert len(descriptor_paths) == 1
        target_descriptor = int(descriptor_paths[0].rsplit("/", 1)[1])
        assert target_descriptor in options["pass_fds"]
        effect = os.open(
            "worktree-output",
            os.O_WRONLY | os.O_CREAT,
            0o600,
            dir_fd=target_descriptor,
        )
        os.close(effect)
        return FakeProcess()

    monkeypatch.setattr(git_service.sys, "platform", "linux")
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(GitError, match="target changed"):
        await git_service.create_worktree(
            str(repo),
            str(target),
            "feature",
            base_ref="origin/main",
        )

    assert (moved / "worktree-output").is_file()
    assert not (target / "worktree-output").exists()


@pytest.mark.asyncio
async def test_ensure_remote_retains_repo_across_read_and_write(tmp_path, monkeypatch):
    """Remote update must stay bound to the repository accepted before its read."""
    from yinshi.services import git as git_service

    repo = tmp_path / "repo"
    repo.mkdir()
    accepted = tmp_path / "accepted-repo"

    class FakeProcess:
        returncode = 0

        def __init__(self, stdout: bytes):
            self.stdout = stdout

        async def communicate(self):
            return self.stdout, b""

    async def replace_during_remote_read(repo_path, remote_name="origin"):
        assert repo_path == str(repo)
        assert remote_name == "origin"
        repo.rename(accepted)
        repo.mkdir()
        return "https://github.com/acme/old"

    async def fake_create_subprocess_exec(*command, **options):
        descriptor = options["pass_fds"][0]
        effect = os.open("remote-updated", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=descriptor)
        os.close(effect)
        assert command[-4:] == ("remote", "set-url", "origin", "https://github.com/acme/new")
        return FakeProcess(b"")

    monkeypatch.setattr(git_service.sys, "platform", "linux")
    monkeypatch.setattr(git_service, "get_remote_url", replace_during_remote_read)
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(GitError, match="working directory changed"):
        await git_service.ensure_remote_url(
            str(repo),
            "https://github.com/acme/new",
        )

    assert (accepted / "remote-updated").is_file()
    assert not (repo / "remote-updated").exists()


@pytest.mark.asyncio
async def test_cleanup_skips_absent_registration_without_recreating_parent(
    git_repo,
    tmp_path,
):
    """Missing metadata does not block branch cleanup or recreate path parents."""
    from yinshi.services import git as git_service

    target = tmp_path / "removed" / "worktrees" / "feature"
    await git_service._run_git(["branch", "feature"], cwd=git_repo)

    await git_service.cleanup_repository_worktrees(
        git_repo,
        [(str(target), "feature")],
    )

    assert not target.parent.exists()
    branch = await git_service._run_git(
        ["branch", "--list", "feature"],
        cwd=git_repo,
    )
    assert branch == ""


@pytest.mark.asyncio
async def test_cleanup_binds_absent_worktree_name_before_git_effect(tmp_path, monkeypatch):
    """Cleanup must claim an absent worktree name before asking Git to remove it."""
    from yinshi.services import git as git_service

    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "feature"
    registration = repo / ".git" / "worktrees" / "feature"
    registration.mkdir(parents=True)
    (registration / "gitdir").write_text(f"{target / '.git'}\n", encoding="utf-8")
    raced = False
    real_lexists = git_service.os.path.lexists

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"refs/heads/main\n", b""

    def replace_after_absence_check(path):
        nonlocal raced
        if os.fspath(path) == str(target) and not raced:
            raced = True
            target.mkdir(parents=True)
            (target / "foreign").write_text("keep\n", encoding="utf-8")
            return False
        return real_lexists(path)

    async def fake_create_subprocess_exec(*command, **_options):
        assert command[-3:] == (
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
        )
        return FakeProcess()

    monkeypatch.setattr(git_service.os.path, "lexists", replace_after_absence_check)
    monkeypatch.setattr(
        git_service.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    await git_service.cleanup_repository_worktrees(
        str(repo),
        [(str(target), "feature")],
    )

    assert (target / "foreign").read_text(encoding="utf-8") == "keep\n"
    assert not registration.exists()


@pytest.mark.asyncio
async def test_delete_worktree_retains_one_handle_across_branch_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch discovery and removal must use one retained worktree object."""
    from yinshi.services import git as git_service

    repository = tmp_path / "repo"
    repository.mkdir(mode=0o700)
    worktree = tmp_path / "worktrees" / "feature"
    worktree.mkdir(parents=True, mode=0o700)
    accepted = worktree.stat()
    moved = tmp_path / "accepted-worktree"
    calls = 0

    async def fake_run_git(arguments, cwd=None, env=None):
        nonlocal calls
        del env
        calls += 1
        if arguments == ["rev-parse", "--abbrev-ref", "HEAD"]:
            assert isinstance(cwd, git_service._StableDirectory)
            worktree.rename(moved)
            worktree.mkdir(mode=0o700)
            return "feature"
        if arguments[:3] == ["worktree", "remove", "--force"]:
            retained = arguments[3]
            assert isinstance(retained, git_service._StableDirectory)
            observed = os.fstat(retained.descriptor)
            assert (observed.st_dev, observed.st_ino) == (accepted.st_dev, accepted.st_ino)
            assert retained is cwd or isinstance(cwd, git_service._StableDirectory)
            return ""
        if arguments == ["branch", "-D", "feature"]:
            return ""
        raise AssertionError(f"Unexpected Git command: {arguments}")

    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    await git_service.delete_worktree(str(repository), str(worktree))

    assert calls == 3


def test_cleanup_unlink_stays_under_retained_parent_after_name_replacement(
    tmp_path: Path,
) -> None:
    """File cleanup must not unlink from a replacement parent namespace."""
    from yinshi.services import git as git_service

    parent = tmp_path / "metadata"
    parent.mkdir(mode=0o700)
    (parent / "gitdir").write_text("owned\n", encoding="utf-8")
    descriptor = os.open(parent, git_service._directory_open_flags())
    moved = tmp_path / "accepted-metadata"
    parent.rename(moved)
    parent.mkdir(mode=0o700)
    foreign = parent / "gitdir"
    foreign.write_text("foreign\n", encoding="utf-8")
    try:
        git_service._remove_directory_contents(descriptor)
    finally:
        os.close(descriptor)

    assert not (moved / "gitdir").exists()
    assert foreign.read_text(encoding="utf-8") == "foreign\n"


def test_cleanup_rmdir_stays_under_retained_parent_after_name_replacement(
    tmp_path: Path,
) -> None:
    """Directory cleanup must not remove from a replacement parent namespace."""
    from yinshi.services import git as git_service

    parent = tmp_path / "metadata"
    (parent / "registration").mkdir(parents=True, mode=0o700)
    descriptor = os.open(parent, git_service._directory_open_flags())
    moved = tmp_path / "accepted-metadata"
    parent.rename(moved)
    (parent / "registration").mkdir(parents=True, mode=0o700)
    try:
        git_service._remove_directory_contents(descriptor)
    finally:
        os.close(descriptor)

    assert not (moved / "registration").exists()
    assert (parent / "registration").is_dir()


def test_cleanup_rejects_nonexclusive_parent_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must not unlink from a namespace writable by another identity."""
    from yinshi.services import git as git_service

    parent = tmp_path / "metadata"
    parent.mkdir(mode=0o700)
    child = parent / "gitdir"
    child.write_text("owned\n", encoding="utf-8")
    parent.chmod(0o733)
    called = False

    def forbidden_unlink(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unlink reached")

    monkeypatch.setattr(git_service.os, "unlink", forbidden_unlink)
    descriptor = os.open(parent, git_service._directory_open_flags())
    try:
        with pytest.raises(GitError, match="exclusive ownership"):
            git_service._remove_directory_contents(descriptor)
    finally:
        os.close(descriptor)

    assert called is False
    assert child.read_text(encoding="utf-8") == "owned\n"


def test_cleanup_rejects_nonexclusive_parent_before_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must not remove a directory from a shared parent namespace."""
    from yinshi.services import git as git_service

    parent = tmp_path / "metadata"
    child = parent / "registration"
    child.mkdir(parents=True, mode=0o700)
    parent.chmod(0o733)
    called = False

    def forbidden_rmdir(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("rmdir reached")

    monkeypatch.setattr(git_service.os, "rmdir", forbidden_rmdir)
    descriptor = os.open(parent, git_service._directory_open_flags())
    try:
        with pytest.raises(GitError, match="exclusive ownership"):
            git_service._remove_directory_contents(descriptor)
    finally:
        os.close(descriptor)

    assert called is False
    assert child.is_dir()


@pytest.mark.asyncio
async def test_cleanup_preserves_recreated_branch_generation(git_repo, tmp_path) -> None:
    """Durable cleanup cannot delete a branch that moved to another commit."""
    from yinshi.services import git as git_service

    branch = "recreated-generation"
    await git_service._run_git(["branch", branch], cwd=git_repo)
    original_oid = await git_service.read_local_branch_oid(git_repo, branch)
    assert original_oid is not None
    marker = Path(git_repo) / "generation.txt"
    marker.write_text("new", encoding="utf-8")
    await git_service._run_git(["add", "generation.txt"], cwd=git_repo)
    await git_service._run_git(["commit", "-m", "new generation"], cwd=git_repo)
    await git_service._run_git(["branch", "-f", branch, "HEAD"], cwd=git_repo)

    with pytest.raises(git_service.GitError):
        await git_service.cleanup_repository_worktrees(
            git_repo,
            [(str(tmp_path / "missing-worktree"), branch, original_oid)],
        )

    assert await git_service.read_local_branch_oid(git_repo, branch) != original_oid
