"""Verify bounded transport for index objects absent from committed bundles."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
from yinshi.services.workspace_replica_bundle import create_committed_bundle
from yinshi.services.workspace_replica_object_pack import (
    IndexObjectPackLimits,
    create_index_object_pack,
    verify_index_object_pack,
)


def git(repository: Path, *args: str, input_bytes: bytes | None = None) -> str:
    return (
        subprocess.run(
            ["git", "-C", str(repository), *args],
            input=input_bytes,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        .stdout.decode("ascii")
        .strip()
    )


def repository(tmp_path: Path, object_format: str) -> Path:
    path = tmp_path / object_format
    path.mkdir()
    git(path, "init", "-q", "--initial-branch=main", f"--object-format={object_format}")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "file.txt").write_bytes(b"committed")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "initial")
    return path


@pytest.mark.asyncio
async def test_byte_pack_write_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_object_pack as module

    header = b"PACK\x00\x00\x00\x02\x00\x00\x00\x00"
    content = header + hashlib.sha1(header).digest()
    real_write = os.write
    release = threading.Event()
    calls = 0

    def paused_write(descriptor: int, value: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait(timeout=1)
        return real_write(descriptor, value)

    monkeypatch.setattr(module.os, "write", paused_write)
    timer = threading.Timer(0.25, release.set)
    timer.start()
    started = time.monotonic()
    task = asyncio.create_task(
        verify_index_object_pack(
            content,
            object_format="sha1",
            expected_oids=(),
        )
    )
    await asyncio.sleep(0)
    heartbeat_delay = time.monotonic() - started
    try:
        verified = await task
    finally:
        release.set()
        timer.cancel()

    assert heartbeat_delay < 0.1
    assert verified.objects == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_staged_only_blob_round_trips_for_both_object_formats(
    tmp_path: Path,
    object_format: str,
) -> None:
    path = repository(tmp_path, object_format)
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    (path / "file.txt").write_bytes(b"staged-only")
    git(path, "add", "file.txt")
    staged_oid = git(path, "rev-parse", ":file.txt")
    (path / "file.txt").write_bytes(b"later-worktree-content")
    worktree_oid = git(path, "hash-object", "--stdin", input_bytes=b"later-worktree-content")
    assert worktree_oid != staged_oid
    created = await create_index_object_pack(path, (staged_oid,), bundle.objects)
    assert created.object_format == object_format
    assert tuple(item.object_id for item in created.objects) == (staged_oid,)
    verified = await verify_index_object_pack(
        created.pack_bytes,
        object_format=object_format,
        expected_oids=(staged_oid,),
    )
    assert verified.sha256 == created.sha256
    assert verified.byte_length == created.byte_length
    assert verified.objects == created.objects


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_bundle_reachable_index_object_produces_empty_pack(
    tmp_path: Path,
    object_format: str,
) -> None:
    path = repository(tmp_path, object_format)
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    committed_blob = git(path, "rev-parse", "HEAD:file.txt")
    created = await create_index_object_pack(path, (committed_blob,), bundle.objects)
    assert created.objects == ()
    assert created.pack_bytes[:12] == b"PACK\x00\x00\x00\x02\x00\x00\x00\x00"
    verified = await verify_index_object_pack(
        created.pack_bytes,
        object_format=object_format,
        expected_oids=(),
    )
    assert verified.objects == ()


@pytest.mark.asyncio
async def test_conflict_stage_blobs_are_all_transported(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    oids = tuple(
        git(path, "hash-object", "-w", "--stdin", input_bytes=value)
        for value in (b"base", b"ours", b"theirs")
    )
    created = await create_index_object_pack(path, tuple(reversed(oids)), bundle.objects)
    assert tuple(item.object_id for item in created.objects) == tuple(sorted(oids))


@pytest.mark.asyncio
async def test_generator_rejects_non_blob_required_object(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    tree_oid = git(path, "rev-parse", "HEAD^{tree}")
    with pytest.raises(GitError, match="blob"):
        await create_index_object_pack(path, (tree_oid,), bundle.objects)


@pytest.mark.asyncio
async def test_verifier_rejects_missing_extra_and_tampered_objects(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    first = git(path, "hash-object", "-w", "--stdin", input_bytes=b"first")
    second = git(path, "hash-object", "-w", "--stdin", input_bytes=b"second")
    created = await create_index_object_pack(path, (first, second), bundle.objects)
    with pytest.raises(GitError, match="inventory"):
        await verify_index_object_pack(
            created.pack_bytes,
            object_format="sha1",
            expected_oids=(first,),
        )
    damaged = bytearray(created.pack_bytes)
    damaged[-1] ^= 1
    with pytest.raises(GitError):
        await verify_index_object_pack(
            bytes(damaged),
            object_format="sha1",
            expected_oids=(first, second),
        )


@pytest.mark.asyncio
async def test_exact_pack_byte_limit_passes_then_one_less_rejects(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    staged = git(path, "hash-object", "-w", "--stdin", input_bytes=b"bounded")
    created = await create_index_object_pack(path, (staged,), bundle.objects)
    exact = replace(IndexObjectPackLimits(), max_pack_bytes=created.byte_length)
    assert (
        await verify_index_object_pack(
            created.pack_bytes,
            object_format="sha1",
            expected_oids=(staged,),
            limits=exact,
        )
    ).objects == created.objects
    with pytest.raises(GitError, match="byte limit"):
        await verify_index_object_pack(
            created.pack_bytes,
            object_format="sha1",
            expected_oids=(staged,),
            limits=replace(exact, max_pack_bytes=created.byte_length - 1),
        )


@pytest.mark.asyncio
async def test_all_object_limits_reject_one_beyond_boundary(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    first = git(path, "hash-object", "-w", "--stdin", input_bytes=b"1234567")
    second = git(path, "hash-object", "-w", "--stdin", input_bytes=b"abcdefg")
    created = await create_index_object_pack(path, (first, second), bundle.objects)
    exact = replace(
        IndexObjectPackLimits(),
        max_objects=2,
        max_object_bytes=7,
        max_inflated_bytes=14,
    )
    assert (
        await verify_index_object_pack(
            created.pack_bytes,
            object_format="sha1",
            expected_oids=(first, second),
            limits=exact,
        )
    ).objects == created.objects
    for limits in (
        replace(exact, max_objects=1),
        replace(exact, max_object_bytes=6),
        replace(exact, max_inflated_bytes=13),
        replace(exact, max_listing_bytes=1),
    ):
        with pytest.raises(GitError):
            await verify_index_object_pack(
                created.pack_bytes,
                object_format="sha1",
                expected_oids=(first, second),
                limits=limits,
            )


def make_thin_pack(repository: Path) -> tuple[bytes, tuple[str, ...]]:
    (repository / "file.txt").write_bytes(b"a" * 100_000)
    git(repository, "add", "file.txt")
    git(repository, "commit", "-qm", "large base")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "file.txt").write_bytes(b"a" * 99_999 + b"b")
    git(repository, "add", "file.txt")
    git(repository, "commit", "-qm", "large delta")
    tip = git(repository, "rev-parse", "HEAD")
    expected = tuple(
        git(repository, "rev-list", "--objects", "--no-object-names", tip, f"^{base}").splitlines()
    )
    packed = subprocess.run(
        ["git", "-C", str(repository), "pack-objects", "--stdout", "--thin", "--revs"],
        input=f"{tip}\n^{base}\n".encode("ascii"),
        check=True,
        capture_output=True,
    ).stdout
    return packed, expected


@pytest.mark.asyncio
async def test_verifier_rejects_thin_pack_with_external_delta_base(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    thin_pack, expected = await asyncio.to_thread(make_thin_pack, path)
    assert int.from_bytes(thin_pack[8:12], "big") == len(expected)
    with pytest.raises(GitError, match="index-pack"):
        await verify_index_object_pack(
            thin_pack,
            object_format="sha1",
            expected_oids=expected,
        )


@pytest.mark.asyncio
async def test_every_source_git_call_disables_lazy_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_object_pack as module

    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    staged = git(path, "hash-object", "-w", "--stdin", input_bytes=b"offline")
    original = module.run_git_bytes
    environments: list[dict[str, str]] = []

    async def checked(*args, **kwargs):
        environments.append(dict(kwargs.get("env") or {}))
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "run_git_bytes", checked)
    await create_index_object_pack(path, (staged,), bundle.objects)
    assert environments
    assert all(environment.get("GIT_NO_LAZY_FETCH") == "1" for environment in environments)
    assert all(environment.get("GIT_NO_REPLACE_OBJECTS") == "1" for environment in environments)


@pytest.mark.asyncio
async def test_replacement_objects_do_not_change_packed_identity(tmp_path: Path) -> None:
    path = repository(tmp_path, "sha1")
    bundle = await create_committed_bundle(path, ("refs/heads/main",), include_head=True)
    original = git(path, "hash-object", "-w", "--stdin", input_bytes=b"original")
    replacement = git(path, "hash-object", "-w", "--stdin", input_bytes=b"replacement")
    git(path, "replace", original, replacement)
    created = await create_index_object_pack(path, (original,), bundle.objects)
    assert tuple(item.object_id for item in created.objects) == (original,)
