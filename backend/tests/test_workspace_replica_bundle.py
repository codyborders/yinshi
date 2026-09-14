"""Verify bounded committed-ref Git bundle creation and validation."""

from __future__ import annotations

import asyncio
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
    verify_committed_bundle,
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
    original = module.verify_committed_bundle

    async def changing(
        content: bytes,
        *,
        object_format: str,
        limits: BundleLimits,
    ):
        advertised = await original(
            content,
            object_format=object_format,
            limits=limits,
        )
        (path / "file.txt").write_text("changed", encoding="utf-8")
        git(path, "add", "file.txt")
        git(path, "commit", "-qm", "race")
        return advertised

    monkeypatch.setattr(module, "verify_committed_bundle", changing)
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


@pytest.mark.asyncio
async def test_public_verifier_returns_exact_reachable_inventory(tmp_path: Path) -> None:
    path = repo(tmp_path)
    created = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    verified = await verify_committed_bundle(
        created.bundle_bytes,
        object_format=created.object_format,
    )
    assert verified.refs == created.refs
    assert verified.sha256 == created.sha256
    assert verified.byte_length == created.byte_length
    expected = set(
        git(
            path,
            "rev-list",
            "--objects",
            "--no-object-names",
            "refs/heads/main",
        ).splitlines()
    )
    assert {item.object_id for item in verified.objects} == expected


@pytest.mark.asyncio
async def test_public_verifier_rejects_unadvertised_bundle_object(tmp_path: Path) -> None:
    path = repo(tmp_path)
    head = git(path, "rev-parse", "HEAD")
    closure = git(path, "rev-list", "--objects", "--no-object-names", "HEAD").splitlines()
    extra_result = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(path), "hash-object", "-w", "--stdin"],
        input=b"unadvertised",
        check=True,
        capture_output=True,
    )
    extra = extra_result.stdout.decode("ascii").strip()
    pack_result = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(path), "pack-objects", "--stdout"],
        input=("\n".join([*closure, extra]) + "\n").encode("ascii"),
        check=True,
        capture_output=True,
    )
    packed = pack_result.stdout
    bundle = b"# v2 git bundle\n" + head.encode("ascii") + b" refs/heads/main\n\n" + packed
    with pytest.raises(GitError, match="unreachable"):
        await verify_committed_bundle(bundle, object_format="sha1")


def raw_bundle(path: Path, object_id: str, ref_name: str, objects: list[str]) -> bytes:
    packed = subprocess.run(
        ["git", "-C", str(path), "pack-objects", "--stdout"],
        input=("\n".join(objects) + "\n").encode("ascii"),
        check=True,
        capture_output=True,
    ).stdout
    return (
        b"# v2 git bundle\n"
        + object_id.encode("ascii")
        + b" "
        + ref_name.encode("ascii")
        + b"\n\n"
        + packed
    )


@pytest.mark.asyncio
async def test_public_verifier_rejects_replacement_ref(tmp_path: Path) -> None:
    path = repo(tmp_path)
    head = git(path, "rev-parse", "HEAD")
    objects = git(path, "rev-list", "--objects", "--no-object-names", "HEAD").splitlines()
    bundle = await asyncio.to_thread(raw_bundle, path, head, f"refs/replace/{head}", objects)
    with pytest.raises(GitError, match="replacement ref"):
        await verify_committed_bundle(bundle, object_format="sha1")


@pytest.mark.asyncio
async def test_public_verifier_rejects_advertised_noncommit(tmp_path: Path) -> None:
    path = repo(tmp_path)
    blob = git(path, "rev-parse", "HEAD:file.txt")
    bundle = await asyncio.to_thread(raw_bundle, path, blob, "refs/tags/blob", [blob])
    with pytest.raises(GitError):
        await verify_committed_bundle(bundle, object_format="sha1")


@pytest.mark.asyncio
async def test_public_verifier_rejects_object_format_mismatch(tmp_path: Path) -> None:
    path = repo(tmp_path)
    created = await create_committed_bundle(path, ("refs/heads/main",), include_head=False)
    with pytest.raises(GitError):
        await verify_committed_bundle(created.bundle_bytes, object_format="sha256")
