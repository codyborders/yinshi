"""Verify atomic publication of one authenticated three-artifact replica set."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from yinshi.services.workspace_publication import WorkspacePublicationError
from yinshi.services.workspace_replica_artifact import (
    ManifestEntry,
    WorktreeArtifactInput,
    WorktreeEntryKind,
    decode_worktree_artifact,
    encode_worktree_artifact,
)
from yinshi.services.workspace_replica_bundle import create_committed_bundle
from yinshi.services.workspace_replica_object_pack import create_index_object_pack
from yinshi.services.workspace_replica_publication import (
    ReplicaArtifactBinding,
    ReplicaArtifactSetDeclaration,
    ReplicaIdentity,
    ReplicaInspectionReceipt,
    ReplicaPublicationCollisionError,
    ReplicaPublicationRejectedError,
    ReplicaPublicationUnresolvedError,
    ReplicaStoreLimits,
    WorkspaceReplicaPublicationStore,
)


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def artifact_set(tmp_path: Path):
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-q", "--initial-branch=main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "file.txt").write_bytes(b"committed")
    git(repository, "add", "file.txt")
    git(repository, "commit", "-qm", "initial")
    bundle = await create_committed_bundle(
        repository,
        ("refs/heads/main",),
        include_head=True,
    )
    (repository / "file.txt").write_bytes(b"staged")
    git(repository, "add", "file.txt")
    staged_oid = git(repository, "rev-parse", ":file.txt")
    (repository / "file.txt").write_bytes(b"working")
    worktree = encode_worktree_artifact(
        WorktreeArtifactInput(
            object_format="sha1",
            head_state="symbolic",
            head_target=b"refs/heads/main",
            head_oid=bytes.fromhex(git(repository, "rev-parse", "HEAD")),
            index_bytes=(repository / ".git" / "index").read_bytes(),
            entries=(
                ManifestEntry(
                    raw_path=b"file.txt",
                    kind=WorktreeEntryKind.FILE,
                    executable=False,
                    allowed_ignored=False,
                    content=b"working",
                ),
            ),
            root_oids=tuple(
                sorted(
                    (
                        bytes.fromhex(git(repository, "rev-parse", "HEAD")),
                        bytes.fromhex(staged_oid),
                    )
                )
            ),
        )
    )
    object_pack = await create_index_object_pack(repository, (staged_oid,), bundle.objects)
    return bundle, worktree, object_pack


def binding(role: str, media_type: str, artifact_id: str, content: bytes):
    return ReplicaArtifactBinding(
        role=role,
        media_type=media_type,
        artifact_id=artifact_id,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


async def declaration_and_bytes(tmp_path: Path):
    bundle, worktree, object_pack = await artifact_set(tmp_path)
    declaration = ReplicaArtifactSetDeclaration(
        version=2,
        operation_id="0123456789abcdef0123456789abcdef",
        repository_id="repository-000000",
        workspace_id="workspace-0000000",
        identity=ReplicaIdentity(
            physical_target_id="target-000000000",
            replica_generation=1,
            execution_owner_id="yinshi-executor-00",
        ),
        object_format="sha1",
        source_state_sha256=decode_worktree_artifact(worktree).source_state_sha256.hex(),
        reconciliation_fingerprint="a" * 64,
        bundle=binding(
            "committed_bundle",
            "application/vnd.yinshi.git-bundle.v1",
            "bundle-000000000",
            bundle.bundle_bytes,
        ),
        worktree=binding(
            "worktree",
            "application/vnd.yinshi.replica-worktree.v1",
            "worktree-0000000",
            worktree,
        ),
        index_objects=binding(
            "index_objects",
            "application/vnd.yinshi.git-index-objects-pack.v1",
            "objects-00000000",
            object_pack.pack_bytes,
        ),
        limits=ReplicaStoreLimits(),
    )
    return declaration, bundle.bundle_bytes, worktree, object_pack.pack_bytes


def store(tmp_path: Path) -> WorkspaceReplicaPublicationStore:
    root = tmp_path / "published"
    root.mkdir(mode=0o700)
    return WorkspaceReplicaPublicationStore(
        root,
        ceilings=ReplicaStoreLimits(),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )


@pytest.mark.asyncio
async def test_publication_receipt_binds_three_artifacts_and_authority(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    receipt = await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert receipt.artifact_set_sha256
    assert receipt.verification.bundle == declaration.bundle
    assert receipt.verification.worktree == declaration.worktree
    assert receipt.verification.index_objects == declaration.index_objects
    assert receipt.identity == declaration.identity
    assert receipt.source_state_sha256 == declaration.source_state_sha256
    final = tmp_path / "published" / declaration.operation_id
    assert stat.S_IMODE(final.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in final.iterdir())
    assert sorted(path.name for path in final.iterdir()) == [
        "artifact-set.json",
        "committed.bundle",
        "index-objects.pack",
        "worktree.yra",
    ]
    assert not list((tmp_path / "published").glob(".*.pending"))
    inspection = await publication.inspect_published_artifact_set(declaration)
    assert type(inspection) is ReplicaInspectionReceipt
    assert inspection.artifact_set_sha256 == receipt.artifact_set_sha256
    assert not hasattr(inspection, "synchronization_receipt_id")


@pytest.mark.asyncio
async def test_any_binding_mismatch_rejects_before_publication(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    for changed in (
        replace(declaration, bundle=replace(declaration.bundle, sha256="0" * 64)),
        replace(declaration, worktree=replace(declaration.worktree, byte_length=len(worktree) + 1)),
        replace(
            declaration,
            index_objects=replace(declaration.index_objects, role="worktree"),  # type: ignore[arg-type]
        ),
        replace(declaration, source_state_sha256="f" * 64),
        replace(declaration, object_format="sha256"),
    ):
        with pytest.raises(ReplicaPublicationRejectedError):
            await publication.publish_artifact_set(changed, bundle, worktree, objects)
        assert not any((tmp_path / "published").iterdir())


@pytest.mark.asyncio
async def test_preexisting_final_target_is_untouched(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    target = tmp_path / "published" / declaration.operation_id
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ReplicaPublicationCollisionError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_storage_symlink_and_unknown_final_entry_are_rejected(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    receipt = await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    final = tmp_path / "published" / declaration.operation_id
    (final / "unknown").write_text("x", encoding="utf-8")
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.inspect_published_artifact_set(declaration)
    (final / "unknown").unlink()
    (final / "worktree.yra").chmod(0o400)
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.inspect_published_artifact_set(declaration)
    (final / "worktree.yra").chmod(0o600)
    (final / "worktree.yra").unlink()
    (final / "worktree.yra").symlink_to("committed.bundle")
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.inspect_published_artifact_set(declaration)
    assert receipt.publication_receipt_id


@pytest.mark.asyncio
async def test_published_hardlink_and_marker_tamper_are_rejected(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    final = tmp_path / "published" / declaration.operation_id
    hardlink = tmp_path / "foreign-hardlink"
    os.link(final / "committed.bundle", hardlink)
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.inspect_published_artifact_set(declaration)
    hardlink.unlink()
    marker = final / "artifact-set.json"
    marker.write_bytes(marker.read_bytes() + b" ")
    with pytest.raises(ReplicaPublicationRejectedError, match="marker"):
        await publication.inspect_published_artifact_set(declaration)


@pytest.mark.asyncio
async def test_fifo_artifact_is_rejected_without_blocking(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    artifact = tmp_path / "published" / declaration.operation_id / "worktree.yra"
    artifact.unlink()
    os.mkfifo(artifact, mode=0o600)
    with pytest.raises(ReplicaPublicationRejectedError):
        await asyncio.wait_for(
            publication.inspect_published_artifact_set(declaration),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_tampered_published_bytes_are_rejected(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    path = tmp_path / "published" / declaration.operation_id / "index-objects.pack"
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.inspect_published_artifact_set(declaration)


@pytest.mark.asyncio
async def test_post_rename_failure_is_unresolved_and_retains_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original = publication._inspect_final

    async def fail_after_rename(*args, **kwargs):
        raise OSError("post-rename failure")

    monkeypatch.setattr(publication, "_inspect_final", fail_after_rename)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert (tmp_path / "published" / declaration.operation_id).is_dir()
    monkeypatch.setattr(publication, "_inspect_final", original)


@pytest.mark.asyncio
async def test_inspection_rejects_file_replaced_during_await(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    original = publication.verify_artifact_set
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(publication, "verify_artifact_set", paused)
    task = asyncio.create_task(publication.inspect_published_artifact_set(declaration))
    await started.wait()
    final = tmp_path / "published" / declaration.operation_id
    replacement = final / "replacement"
    replacement.write_bytes((final / "worktree.yra").read_bytes())
    replacement.chmod(0o600)
    replacement.replace(final / "worktree.yra")
    release.set()
    with pytest.raises(ReplicaPublicationRejectedError, match="changed"):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_root", [False, True])
async def test_publication_rejects_root_or_final_swap_during_final_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_root: bool,
) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original = publication.verify_artifact_set
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def paused(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(publication, "verify_artifact_set", paused)
    task = asyncio.create_task(
        publication.publish_artifact_set(declaration, bundle, worktree, objects)
    )
    await started.wait()
    root = tmp_path / "published"
    if replace_root:
        root.rename(tmp_path / "old-published")
        root.mkdir(mode=0o700)
    else:
        final = root / declaration.operation_id
        final.rename(root / "old-final")
        (root / declaration.operation_id).mkdir(mode=0o700)
    release.set()
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await task


@pytest.mark.asyncio
async def test_late_directory_mode_change_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    original = publication.verify_artifact_set
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(publication, "verify_artifact_set", paused)
    task = asyncio.create_task(publication.inspect_published_artifact_set(declaration))
    await started.wait()
    final = tmp_path / "published" / declaration.operation_id
    final.chmod(0o777)
    release.set()
    with pytest.raises(ReplicaPublicationRejectedError, match="changed"):
        await task


@pytest.mark.asyncio
async def test_bundled_index_object_must_be_blob(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    decoded = decode_worktree_artifact(worktree)
    repository = tmp_path / "source"
    tree = bytes.fromhex(git(repository, "rev-parse", "HEAD^{tree}"))
    index = bytearray(decoded.index_bytes or b"")
    index[52:72] = tree
    index[-20:] = hashlib.sha1(index[:-20]).digest()
    changed_worktree = encode_worktree_artifact(
        WorktreeArtifactInput(
            object_format=decoded.object_format,
            head_state=decoded.head_state,
            head_target=decoded.head_target,
            head_oid=decoded.head_oid,
            index_bytes=bytes(index),
            entries=decoded.entries,
            root_oids=tuple(sorted((decoded.head_oid, tree))),
        )
    )
    changed = replace(
        declaration,
        worktree=binding(
            "worktree",
            "application/vnd.yinshi.replica-worktree.v1",
            "worktree-tree-000",
            changed_worktree,
        ),
        source_state_sha256=decode_worktree_artifact(changed_worktree).source_state_sha256.hex(),
    )
    with pytest.raises(ReplicaPublicationRejectedError, match="not a blob"):
        await store(tmp_path).verify_artifact_set(changed, bundle, changed_worktree, objects)


@pytest.mark.asyncio
async def test_pre_rename_failure_cleans_owned_stage(tmp_path: Path, monkeypatch) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)

    original = module.atomic_rename_no_replace
    calls = 0

    def fail_before_move(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspacePublicationError("before move")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "atomic_rename_no_replace", fail_before_move)
    with pytest.raises(ReplicaPublicationRejectedError, match="before visibility"):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    names = {path.name for path in (tmp_path / "published").iterdir()}
    assert len(names) == 1
    assert next(iter(names)).startswith(f".{declaration.operation_id}.pending.abandoned.")


@pytest.mark.asyncio
async def test_repeated_previsibility_failures_use_distinct_quarantines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original = module.atomic_rename_no_replace
    calls = 0

    def fail_each_publication(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls % 2 == 1:
            raise WorkspacePublicationError("before move")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "atomic_rename_no_replace", fail_each_publication)
    for _ in range(2):
        with pytest.raises(ReplicaPublicationRejectedError, match="before visibility"):
            await publication.publish_artifact_set(
                declaration,
                bundle,
                worktree,
                objects,
            )
    retained = list(
        (tmp_path / "published").glob(f".{declaration.operation_id}.pending.abandoned.*")
    )
    assert len(retained) == 2
    assert not [
        path
        for path in (tmp_path / "published").glob(f".{declaration.operation_id}.pending.*")
        if len(path.name.split(".")) == 4
    ]


@pytest.mark.asyncio
async def test_retained_legacy_pending_does_not_block_retry(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    retained = tmp_path / "published" / f".{declaration.operation_id}.pending"
    retained.mkdir(mode=0o700)
    receipt = await publication.publish_artifact_set(
        declaration,
        bundle,
        worktree,
        objects,
    )
    assert receipt.artifact_set_sha256
    assert retained.is_dir()


@pytest.mark.asyncio
async def test_unknown_rename_state_lookup_is_unresolved(tmp_path: Path, monkeypatch) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original_stat = module.os.stat

    def move_then_fail(source: Path, target: Path, **kwargs):
        os.rename(source, target)
        raise OSError("root sync failed")

    def uncertain_stat(path, *args, **kwargs):
        if path == declaration.operation_id:
            raise PermissionError("identity unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module, "atomic_rename_no_replace", move_then_fail)
    monkeypatch.setattr(module.os, "stat", uncertain_stat)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)


@pytest.mark.asyncio
async def test_post_move_sync_failure_is_unresolved(tmp_path: Path, monkeypatch) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)

    def move_then_fail(source: Path, target: Path, **kwargs):
        os.rename(source, target)
        raise OSError("root sync failed")

    monkeypatch.setattr(module, "atomic_rename_no_replace", move_then_fail)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert (tmp_path / "published" / declaration.operation_id).is_dir()


@pytest.mark.asyncio
async def test_cleanup_never_deletes_file_swapped_after_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original_rename = module.atomic_rename_no_replace
    original_stat = module.os.stat
    rename_calls = 0
    swapped = False
    abandoned: Path | None = None

    def fail_then_quarantine(*args, **kwargs):
        nonlocal abandoned, rename_calls
        rename_calls += 1
        if rename_calls == 1:
            raise WorkspacePublicationError("before move")
        abandoned = Path(args[1])
        return original_rename(*args, **kwargs)

    def swap_after_check(path, *args, **kwargs):
        nonlocal swapped
        if path == "worktree.yra" and not swapped and abandoned is not None:
            swapped = True
            original = abandoned / "committed.bundle"
            original.unlink()
            original.write_bytes(b"foreign")
            original.chmod(0o600)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module, "atomic_rename_no_replace", fail_then_quarantine)
    monkeypatch.setattr(module.os, "stat", swap_after_check)
    with pytest.raises(ReplicaPublicationRejectedError, match="before visibility"):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert abandoned is not None
    retained = abandoned / "committed.bundle"
    assert retained.read_bytes() == b"foreign"


@pytest.mark.asyncio
async def test_cleanup_swap_retains_foreign_directory(tmp_path: Path, monkeypatch) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original = module.atomic_rename_no_replace
    calls = 0

    def fail_then_swap(source: Path, target: Path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspacePublicationError("before move")
        result = original(source, target, **kwargs)
        target.rename(target.with_name(f"{target.name}.owned"))
        target.mkdir(mode=0o700)
        (target / "foreign").write_bytes(b"keep")
        return result

    monkeypatch.setattr(module, "atomic_rename_no_replace", fail_then_swap)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    retained = [
        path
        for path in (tmp_path / "published").glob(
            f".{declaration.operation_id}.pending.abandoned.*"
        )
        if (path / "foreign").exists()
    ]
    assert len(retained) == 1
    assert (retained[0] / "foreign").read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_pending_tamper_never_becomes_visible(tmp_path: Path, monkeypatch) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    original = publication._inspect_pending

    def tamper(parent: int, pending_name: str, expected: dict[str, bytes]):
        pending = tmp_path / "published" / pending_name
        (pending / "unknown").write_bytes(b"foreign")
        return original(parent, pending_name, expected)

    monkeypatch.setattr(publication, "_inspect_pending", tamper)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert not (tmp_path / "published" / declaration.operation_id).exists()
    retained = list(
        (tmp_path / "published").glob(f".{declaration.operation_id}.pending.abandoned.*")
    )
    assert len(retained) == 1
    assert (retained[0] / "unknown").exists()


@pytest.mark.asyncio
async def test_duplicate_artifact_ids_are_rejected_at_publication(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    duplicate = replace(
        declaration,
        worktree=replace(
            declaration.worktree,
            artifact_id=declaration.bundle.artifact_id,
        ),
    )
    with pytest.raises(ReplicaPublicationRejectedError, match="must be distinct"):
        await store(tmp_path).verify_artifact_set(
            duplicate,
            bundle,
            worktree,
            objects,
        )


@pytest.mark.asyncio
async def test_malformed_declaration_types_use_stable_rejection(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)

    class NeverEqual:
        def __eq__(self, other: object) -> bool:
            raise AssertionError("custom equality must not run")

    for version in (2.0, 2 + 0j, NeverEqual()):
        malformed_version = replace(declaration, version=version)  # type: ignore[arg-type]
        with pytest.raises(ReplicaPublicationRejectedError):
            await publication.verify_artifact_set(
                malformed_version,
                bundle,
                worktree,
                objects,
            )
    malformed = replace(declaration, object_format=1)  # type: ignore[arg-type]
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.verify_artifact_set(malformed, bundle, worktree, objects)
    malformed_binding = replace(
        declaration,
        bundle=replace(declaration.bundle, media_type=1),  # type: ignore[arg-type]
    )
    with pytest.raises(ReplicaPublicationRejectedError):
        await publication.verify_artifact_set(malformed_binding, bundle, worktree, objects)


@pytest.mark.asyncio
async def test_cancellation_after_rename_is_unresolved(tmp_path: Path, monkeypatch) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(publication, "_inspect_final", cancelled)
    with pytest.raises(ReplicaPublicationUnresolvedError):
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    assert (tmp_path / "published" / declaration.operation_id).is_dir()


@pytest.mark.asyncio
async def test_sync_and_visibility_order_precedes_receipt(tmp_path: Path, monkeypatch) -> None:
    from yinshi.services import workspace_replica_publication as module

    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    publication = store(tmp_path)
    events: list[str] = []
    original_fsync = module.os.fsync
    original_rename = module.atomic_rename_no_replace
    original_verify = publication.verify_artifact_set
    verification_count = 0

    def tracked_fsync(descriptor: int) -> None:
        kind = "dir-sync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-sync"
        events.append(kind)
        original_fsync(descriptor)

    def tracked_rename(*args, **kwargs):
        events.append("rename-start")
        original_rename(*args, **kwargs)
        events.append("rename-end")

    async def tracked_verify(*args, **kwargs):
        nonlocal verification_count
        result = await original_verify(*args, **kwargs)
        verification_count += 1
        if verification_count == 1:
            events.clear()
        return result

    monkeypatch.setattr(module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(module, "atomic_rename_no_replace", tracked_rename)
    monkeypatch.setattr(publication, "verify_artifact_set", tracked_verify)
    receipt = await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    rename_start = events.index("rename-start")
    rename_end = events.index("rename-end")
    assert events[:rename_start].count("file-sync") == 4
    assert events[rename_start - 1] == "dir-sync"
    assert "dir-sync" in events[rename_start + 1 : rename_end]
    assert receipt.publication_receipt_id


def test_root_mode_must_be_private_before_staging(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(ReplicaPublicationRejectedError, match="exclusively owned"):
        WorkspaceReplicaPublicationStore(
            root,
            ceilings=ReplicaStoreLimits(),
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )
    assert not any(root.iterdir())


@pytest.mark.asyncio
async def test_declared_aggregate_limit_is_exact(tmp_path: Path) -> None:
    declaration, bundle, worktree, objects = await declaration_and_bytes(tmp_path)
    total = len(bundle) + len(worktree) + len(objects)
    exact_limits = replace(declaration.limits, max_set_bytes=total)
    declaration = replace(declaration, limits=exact_limits)
    exact_root = tmp_path / "exact"
    exact_root.mkdir(mode=0o700)
    publication = WorkspaceReplicaPublicationStore(
        exact_root,
        ceilings=exact_limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    assert (
        await publication.publish_artifact_set(declaration, bundle, worktree, objects)
    ).artifact_set_sha256
    assert publication.limits_sha256

    smaller_limits = replace(exact_limits, max_set_bytes=total - 1)
    other_root = tmp_path / "other"
    other_root.mkdir(mode=0o700)
    smaller = WorkspaceReplicaPublicationStore(
        other_root,
        ceilings=smaller_limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    with pytest.raises(ReplicaPublicationRejectedError):
        await smaller.publish_artifact_set(
            replace(
                declaration,
                operation_id="fedcba9876543210fedcba9876543210",
                limits=smaller_limits,
            ),
            bundle,
            worktree,
            objects,
        )
