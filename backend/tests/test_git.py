"""Tests for git service operations."""

import os

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
