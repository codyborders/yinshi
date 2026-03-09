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

    with pytest.raises(GitError, match="URL must start with"):
        _validate_clone_url("ftp://example.com/repo.git")


def test_validate_clone_url_allows_https():
    """https:// URLs should be allowed."""
    from yinshi.services.git import _validate_clone_url

    _validate_clone_url("https://github.com/user/repo.git")


def test_validate_clone_url_allows_ssh():
    """ssh:// URLs should be allowed."""
    from yinshi.services.git import _validate_clone_url

    _validate_clone_url("ssh://git@github.com/user/repo.git")


def test_validate_clone_url_allows_git_at():
    """git@ URLs should be allowed."""
    from yinshi.services.git import _validate_clone_url

    _validate_clone_url("git@github.com:user/repo.git")


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
async def test_create_worktree_has_files(git_repo, tmp_path):
    """Worktree should contain the repo's files."""
    from yinshi.services.git import create_worktree

    wt_path = str(tmp_path / "worktrees" / "file-test")
    await create_worktree(git_repo, wt_path, "file-test")
    assert os.path.isfile(os.path.join(wt_path, "README.md"))
