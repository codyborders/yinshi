"""Verify bounded committed-ref Git bundle creation and validation."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
from yinshi.services.workspace_replica_bundle import (
    BundleLimits,
    BundleRef,
    HeadCapture,
    create_committed_bundle,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def repo(tmp_path: Path, *, object_format: str = "sha1") -> Path:
    path = tmp_path / object_format
    path.mkdir()
    git(
        path,
        "init",
        "-q",
        "--initial-branch=main",
        f"--object-format={object_format}",
    )
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "file.txt").write_text("one", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "initial")
    git(path, "tag", "-a", "v1", "-m", "tag")
    return path


@pytest.mark.asyncio
async def test_bundle_records_exact_refs_and_symbolic_head(tmp_path: Path) -> None:
    path = repo(tmp_path)
    result = await create_committed_bundle(
        path,
        ("refs/tags/v1", "refs/heads/main"),
        include_head=True,
    )
    assert result.byte_length == len(result.bundle_bytes)
    assert result.sha256 == hashlib.sha256(result.bundle_bytes).hexdigest()
    assert result.refs == tuple(sorted(result.refs, key=lambda item: item.name))
    assert result.head == HeadCapture(
        kind="symbolic",
        target="refs/heads/main",
        object_id=git(path, "rev-parse", "HEAD"),
    )
    bundle = tmp_path / "result.bundle"
    bundle.write_bytes(result.bundle_bytes)
    git(path, "bundle", "verify", str(bundle))


@pytest.mark.asyncio
async def test_bundle_preserves_annotated_tag_object(tmp_path: Path) -> None:
    path = repo(tmp_path)
    git(path, "tag", "-a", "nested", "-m", "nested", "refs/tags/v1")
    result = await create_committed_bundle(
        path,
        ("refs/tags/v1", "refs/tags/nested"),
        include_head=False,
    )
    assert result.refs[0].object_id == git(path, "rev-parse", "refs/tags/nested")
    assert result.refs[1].object_id == git(path, "rev-parse", "refs/tags/v1")
    assert result.refs[1].object_id != git(path, "rev-parse", "refs/tags/v1^{}")
    assert result.head is None


@pytest.mark.asyncio
async def test_bundle_rejects_tag_that_does_not_peel_to_commit(tmp_path: Path) -> None:
    path = repo(tmp_path)
    blob = git(path, "hash-object", "-w", "file.txt")
    git(path, "tag", "-a", "blob-tag", "-m", "blob", blob)
    with pytest.raises(GitError):
        await create_committed_bundle(path, ("refs/tags/blob-tag",), include_head=False)


@pytest.mark.asyncio
async def test_bundle_preserves_nonbranch_symbolic_head(tmp_path: Path) -> None:
    path = repo(tmp_path)
    head = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/yinshi/head", head)
    git(path, "symbolic-ref", "refs/yinshi/alias", "refs/yinshi/head")
    git(path, "symbolic-ref", "HEAD", "refs/yinshi/alias")
    (path / ".git" / "index").write_bytes(b"invalid-index-is-irrelevant")
    result = await create_committed_bundle(path, (), include_head=True)
    assert result.head == HeadCapture(
        kind="symbolic",
        target="refs/yinshi/alias",
        object_id=head,
    )


@pytest.mark.asyncio
async def test_bundle_supports_detached_head_and_sha256(tmp_path: Path) -> None:
    path = repo(tmp_path, object_format="sha256")
    head = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "--detach", "-q", head)
    result = await create_committed_bundle(path, (), include_head=True)
    assert result.head == HeadCapture(kind="detached", target=None, object_id=head)
    assert len(result.refs[0].object_id) == 64
    assert result.object_format == "sha256"


@pytest.mark.asyncio
async def test_bundle_rejects_empty_unborn_repository(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.mkdir()
    git(path, "init", "-q")
    with pytest.raises(GitError, match="no committed refs"):
        await create_committed_bundle(path, (), include_head=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["HEAD", "main", "refs/heads/", "refs/heads/a..b", "refs/heads/a@{b", "-bad"],
)
async def test_bundle_rejects_unsafe_or_nonfull_refs(tmp_path: Path, name: str) -> None:
    path = repo(tmp_path)
    with pytest.raises((ValueError, GitError)):
        await create_committed_bundle(path, (name,), include_head=False)


@pytest.mark.asyncio
async def test_bundle_rejects_duplicate_and_excess_refs(tmp_path: Path) -> None:
    path = repo(tmp_path)
    with pytest.raises(ValueError, match="duplicated"):
        await create_committed_bundle(
            path,
            ("refs/heads/main", "refs/heads/main"),
            include_head=False,
        )
    with pytest.raises(ValueError, match="count"):
        await create_committed_bundle(
            path,
            ("refs/heads/main", "refs/tags/v1"),
            include_head=False,
            limits=replace(BundleLimits(), max_refs=1),
        )


@pytest.mark.asyncio
async def test_bundle_enforces_exact_byte_limit(tmp_path: Path) -> None:
    path = repo(tmp_path)
    result = await create_committed_bundle(path, ("refs/heads/main",), include_head=False)
    exact = replace(BundleLimits(), max_bundle_bytes=result.byte_length)
    assert (
        await create_committed_bundle(
            path,
            ("refs/heads/main",),
            include_head=False,
            limits=exact,
        )
    ).bundle_bytes
    with pytest.raises(GitError, match="output exceeded limit"):
        await create_committed_bundle(
            path,
            ("refs/heads/main",),
            include_head=False,
            limits=replace(exact, max_bundle_bytes=result.byte_length - 1),
        )


@pytest.mark.asyncio
async def test_bundle_detects_ref_change_during_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yinshi.services import workspace_replica_bundle as module

    path = repo(tmp_path)
    original = module._verify_bundle

    async def changing(
        content: bytes,
        limits: BundleLimits,
        object_format: str,
    ):
        advertised = await original(content, limits, object_format)
        (path / "file.txt").write_text("changed", encoding="utf-8")
        git(path, "add", "file.txt")
        git(path, "commit", "-qm", "race")
        return advertised

    monkeypatch.setattr(module, "_verify_bundle", changing)
    with pytest.raises(GitError, match="changed during bundle creation"):
        await create_committed_bundle(path, ("refs/heads/main",), include_head=False)


@pytest.mark.asyncio
async def test_bundle_ignores_replacement_objects(tmp_path: Path) -> None:
    path = repo(tmp_path)
    original = git(path, "rev-parse", "HEAD")
    (path / "file.txt").write_text("replacement", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "replacement")
    replacement = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/heads/main", original)
    git(path, "replace", original, replacement)
    result = await create_committed_bundle(path, ("refs/heads/main",), include_head=False)
    assert result.refs == (BundleRef("refs/heads/main", original),)


@pytest.mark.fail_closed_isolation
@pytest.mark.asyncio
async def test_bundle_creation_keeps_trusted_git_gate_fail_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    with pytest.raises(Exception, match="trusted_git"):
        await create_committed_bundle(path, ("refs/heads/main",), include_head=False)
