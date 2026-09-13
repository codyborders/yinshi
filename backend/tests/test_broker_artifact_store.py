"""Check bounded broker-owned ingestion of the fixed replica artifact set."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from yinshi.services.broker_artifact_store import (
    ArtifactReconciliationResult,
    BrokerArtifactLimits,
    BrokerArtifactStore,
    BrokerArtifactStoreCollisionError,
    BrokerArtifactStoreRejectedError,
    BrokerArtifactStoreUnresolvedError,
    ReplicaArtifactManifest,
)
from yinshi.services.broker_replica_journal import ArtifactReference, IngestReceipt

OPERATION_ID = "a" * 32
BUNDLE = b"bundle-content"
WORKTREE = b"worktree-content"
INDEX_OBJECTS = b"pack-content"


def reference(role: str, content: bytes) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=f"{role}_000000000000000000000000",
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


def manifest(**changes: object) -> ReplicaArtifactManifest:
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "bundle": reference("bundle", BUNDLE),
        "worktree": reference("worktree", WORKTREE),
        "index_objects": reference("index", INDEX_OBJECTS),
    }
    values.update(changes)
    return ReplicaArtifactManifest(**values)  # type: ignore[arg-type]


def reader_for(*parts: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for part in parts:
        reader.feed_data(part)
    if eof:
        reader.feed_eof()
    return reader


def store(tmp_path: Path, **limit_changes: object) -> BrokerArtifactStore:
    root = tmp_path / "incoming"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    limits = BrokerArtifactLimits(**limit_changes)  # type: ignore[arg-type]
    return BrokerArtifactStore(
        root,
        limits=limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )


@pytest.mark.asyncio
async def test_receive_streams_fixed_files_and_returns_reference_only_receipt(
    tmp_path: Path,
) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    receipt = await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    assert receipt == IngestReceipt(
        receipt_id=receipt.receipt_id,
        bundle=declared.bundle,
        worktree=declared.worktree,
        index_objects=declared.index_objects,
    )
    final = tmp_path / "incoming" / OPERATION_ID
    assert {item.name for item in final.iterdir()} == {
        "committed.bundle",
        "worktree.yra",
        "index-objects.pack",
    }
    assert (final / "committed.bundle").read_bytes() == BUNDLE
    assert (final / "worktree.yra").read_bytes() == WORKTREE
    assert (final / "index-objects.pack").read_bytes() == INDEX_OBJECTS
    assert stat.S_IMODE(final.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in final.iterdir())

    with artifact_store.open_incoming(declared) as opened:
        assert opened.bundle.reference == declared.bundle
        assert opened.worktree.reference == declared.worktree
        assert opened.index_objects.reference == declared.index_objects
        assert os.pread(opened.bundle.descriptor, len(BUNDLE), 0) == BUNDLE


@pytest.mark.asyncio
async def test_exact_retry_consumes_and_reuses_existing_set(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    first = await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    second = await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    assert second == first
    assert {item.name for item in (tmp_path / "incoming").iterdir()} == {OPERATION_ID}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parts", "match"),
    [
        ((BUNDLE[:-1],), "truncated"),
        ((BUNDLE, WORKTREE, INDEX_OBJECTS, b"extra"), "trailing"),
        ((b"x" * len(BUNDLE), WORKTREE, INDEX_OBJECTS), "digest"),
    ],
)
async def test_invalid_transfer_never_becomes_incoming(
    tmp_path: Path,
    parts: tuple[bytes, ...],
    match: str,
) -> None:
    artifact_store = store(tmp_path)
    with pytest.raises(BrokerArtifactStoreRejectedError, match=match):
        await artifact_store.receive(manifest(), reader_for(*parts))
    root = tmp_path / "incoming"
    assert not (root / OPERATION_ID).exists()
    assert not (root / f".{OPERATION_ID}.pending").exists()
    assert len(list(root.glob(f".{OPERATION_ID}.pending.abandoned.*"))) == 1


@pytest.mark.asyncio
async def test_limits_reject_before_any_directory_effect(tmp_path: Path) -> None:
    artifact_store = store(tmp_path, max_set_bytes=1)
    with pytest.raises(BrokerArtifactStoreRejectedError, match="limit"):
        await artifact_store.receive(
            manifest(),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        )
    assert not any((tmp_path / "incoming").iterdir())


@pytest.mark.asyncio
async def test_idle_timeout_is_unresolved_and_quarantined(tmp_path: Path) -> None:
    artifact_store = store(tmp_path, idle_timeout_seconds=0.01)
    with pytest.raises(BrokerArtifactStoreUnresolvedError, match="timeout"):
        await artifact_store.receive(manifest(), reader_for(eof=False))
    assert not (tmp_path / "incoming" / OPERATION_ID).exists()
    assert len(list((tmp_path / "incoming").glob(f".{OPERATION_ID}.pending.abandoned.*"))) == 1


@pytest.mark.asyncio
async def test_absolute_transfer_deadline_stops_drip_input(tmp_path: Path) -> None:
    artifact_store = store(
        tmp_path,
        idle_timeout_seconds=0.02,
        transfer_timeout_seconds=0.04,
    )
    reader = reader_for(eof=False)

    async def drip() -> None:
        for byte in BUNDLE:
            reader.feed_data(bytes((byte,)))
            await asyncio.sleep(0.01)

    feeder = asyncio.create_task(drip())
    try:
        with pytest.raises(BrokerArtifactStoreUnresolvedError, match="deadline"):
            await artifact_store.receive(manifest(), reader)
    finally:
        feeder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await feeder
    assert not (tmp_path / "incoming" / OPERATION_ID).exists()
    assert len(list((tmp_path / "incoming").glob(f".{OPERATION_ID}.pending.abandoned.*"))) == 1


@pytest.mark.asyncio
async def test_cancellation_quarantines_owned_attempt(tmp_path: Path) -> None:
    artifact_store = store(tmp_path, idle_timeout_seconds=60.0)
    task = asyncio.create_task(artifact_store.receive(manifest(), reader_for(eof=False)))
    for _ in range(100):
        if list((tmp_path / "incoming").glob(".*.pending.*")):
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "incoming" / OPERATION_ID).exists()
    assert len(list((tmp_path / "incoming").glob(f".{OPERATION_ID}.pending.abandoned.*"))) == 1


@pytest.mark.asyncio
async def test_collision_preserves_existing_foreign_state(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    final = tmp_path / "incoming" / OPERATION_ID
    final.mkdir(mode=0o700)
    (final / "foreign").write_bytes(b"keep")
    with pytest.raises(BrokerArtifactStoreCollisionError):
        await artifact_store.receive(
            manifest(),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        )
    assert (final / "foreign").read_bytes() == b"keep"


def test_manifest_rejects_duplicate_ids_and_bad_operation() -> None:
    duplicate = reference("same", BUNDLE)
    with pytest.raises(ValueError):
        manifest(bundle=duplicate, worktree=duplicate)
    with pytest.raises(ValueError):
        manifest(operation_id="../unsafe")


def test_root_must_be_private_and_owned(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(BrokerArtifactStoreRejectedError, match="root"):
        BrokerArtifactStore(
            root,
            limits=BrokerArtifactLimits(),
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )


@pytest.mark.asyncio
async def test_inspection_rejects_symlink_without_following_it(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    final = tmp_path / "incoming" / OPERATION_ID
    (final / "worktree.yra").unlink()
    (final / "worktree.yra").symlink_to(tmp_path / "outside")
    with pytest.raises(BrokerArtifactStoreRejectedError):
        artifact_store.inspect_incoming(declared)


class _OneByteReader(asyncio.StreamReader):
    async def read(self, count: int = -1) -> bytes:
        return await super().read(1 if count < 0 else min(count, 1))


@pytest.mark.asyncio
async def test_partial_reads_make_progress_until_each_declared_length(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    reader = _OneByteReader()
    reader.feed_data(BUNDLE + WORKTREE + INDEX_OBJECTS)
    reader.feed_eof()
    receipt = await artifact_store.receive(manifest(), reader)
    assert receipt.bundle.sha256 == hashlib.sha256(BUNDLE).hexdigest()


@pytest.mark.asyncio
async def test_preidentity_failure_does_not_block_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import broker_artifact_store as module

    artifact_store = store(tmp_path)
    original_stat = module.os.stat
    failed = False

    def fail_first_pending_stat(path, *args, **kwargs):
        nonlocal failed
        if not failed and isinstance(path, str) and path.startswith(f".{OPERATION_ID}.pending."):
            failed = True
            raise PermissionError("pending identity unavailable")
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "stat", fail_first_pending_stat)
        with pytest.raises(BrokerArtifactStoreUnresolvedError):
            await artifact_store.receive(
                manifest(),
                reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
            )
    receipt = await artifact_store.receive(
        manifest(),
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    assert receipt.worktree.sha256 == hashlib.sha256(WORKTREE).hexdigest()
    reconciliation = artifact_store.reconcile_attempts({OPERATION_ID: manifest()})
    assert {result.state for result in reconciliation} == {
        "conflict_quarantined",
        "incoming_verified",
    }


@pytest.mark.asyncio
async def test_repeated_failures_use_distinct_retained_quarantine_names(
    tmp_path: Path,
) -> None:
    artifact_store = store(tmp_path)
    for _ in range(2):
        with pytest.raises(BrokerArtifactStoreRejectedError, match="truncated"):
            await artifact_store.receive(manifest(), reader_for(BUNDLE[:-1]))
    retained = list((tmp_path / "incoming").glob(f".{OPERATION_ID}.pending.abandoned.*"))
    assert len(retained) == 2
    assert len({path.name for path in retained}) == 2


@pytest.mark.asyncio
async def test_post_publication_cancellation_returns_unresolved_without_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = store(tmp_path)

    async def cancel_inspection(
        _self: BrokerArtifactStore,
        _manifest: ReplicaArtifactManifest,
    ) -> IngestReceipt:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        BrokerArtifactStore,
        "inspect_incoming_async",
        cancel_inspection,
    )
    with pytest.raises(BrokerArtifactStoreUnresolvedError, match="acknowledgment"):
        await artifact_store.receive(
            manifest(),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        )
    root = tmp_path / "incoming"
    assert (root / OPERATION_ID).is_dir()
    assert not list(root.glob(f".{OPERATION_ID}.pending.abandoned.*"))


def test_reconciliation_quarantines_pending_final_conflict(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    root = tmp_path / "incoming"
    (root / OPERATION_ID).mkdir(mode=0o700)
    (root / f".{OPERATION_ID}.pending").mkdir(mode=0o700)
    results = artifact_store.reconcile_attempts({OPERATION_ID: manifest()})
    states = {result.state for result in results}
    assert states == {"unverified_final_present", "conflict_quarantined"}
    assert not (root / f".{OPERATION_ID}.pending").exists()
    assert len(list(root.glob(f".{OPERATION_ID}.pending.abandoned.*"))) == 1


@pytest.mark.asyncio
async def test_root_entry_capacity_bounds_sequential_and_concurrent_attempts(
    tmp_path: Path,
) -> None:
    (tmp_path / "sequential").mkdir()
    sequential = store(tmp_path / "sequential", max_root_entries=1)
    (tmp_path / "sequential" / "incoming" / "retained").mkdir(mode=0o700)
    with pytest.raises(BrokerArtifactStoreRejectedError, match="capacity"):
        await sequential.receive(
            manifest(),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        )

    (tmp_path / "concurrent").mkdir()
    concurrent = store(
        tmp_path / "concurrent",
        max_root_entries=1,
        idle_timeout_seconds=1.0,
    )
    second_instance = BrokerArtifactStore(
        tmp_path / "concurrent" / "incoming",
        limits=BrokerArtifactLimits(
            max_bundle_bytes=100,
            max_worktree_bytes=100,
            max_index_objects_bytes=100,
            max_set_bytes=300,
            max_root_entries=1,
            chunk_bytes=3,
            idle_timeout_seconds=1.0,
        ),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    first = asyncio.create_task(concurrent.receive(manifest(), reader_for(eof=False)))
    for _ in range(100):
        if list((tmp_path / "concurrent" / "incoming").glob(".*.pending.*")):
            break
        await asyncio.sleep(0.001)
    with pytest.raises(BrokerArtifactStoreRejectedError, match="capacity"):
        await second_instance.receive(
            replace(manifest(), operation_id="b" * 32),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(list((tmp_path / "concurrent" / "incoming").iterdir())) == 1


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_root_lock_does_not_retain_lock(
    tmp_path: Path,
) -> None:
    artifact_store = store(tmp_path, idle_timeout_seconds=1.0)
    root = tmp_path / "incoming"
    blocker = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(blocker, fcntl.LOCK_EX)
    try:
        waiting = asyncio.create_task(
            artifact_store.receive(
                manifest(),
                reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
            )
        )
        await asyncio.sleep(0.03)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        fcntl.flock(blocker, fcntl.LOCK_UN)
        os.close(blocker)

    receipt = await asyncio.wait_for(
        artifact_store.receive(
            manifest(),
            reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
        ),
        timeout=1.0,
    )
    assert receipt.bundle.sha256 == hashlib.sha256(BUNDLE).hexdigest()


@pytest.mark.asyncio
async def test_reconciliation_verifies_known_final_manifest(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    assert artifact_store.reconcile_attempts({OPERATION_ID: declared}) == (
        ArtifactReconciliationResult(
            entry_name=OPERATION_ID,
            operation_id=OPERATION_ID,
            state="incoming_verified",
        ),
    )


@pytest.mark.asyncio
async def test_reconciliation_never_verifies_through_replaced_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    original = BrokerArtifactStore._inspect_named_set_closed
    replaced = False

    def replace_root(self, *args, **kwargs):
        nonlocal replaced
        if not replaced:
            root = tmp_path / "incoming"
            root.rename(tmp_path / "old-incoming")
            root.mkdir(mode=0o700)
            replaced = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BrokerArtifactStore, "_inspect_named_set_closed", replace_root)
    reconciliation = artifact_store.reconcile_attempts({OPERATION_ID: declared})
    assert reconciliation[0].state == "unverified_final_present"


@pytest.mark.asyncio
async def test_async_open_and_recheck_keep_artifacts_pinned(tmp_path: Path) -> None:
    artifact_store = store(tmp_path)
    declared = manifest()
    await artifact_store.receive(
        declared,
        reader_for(BUNDLE, WORKTREE, INDEX_OBJECTS),
    )
    async with artifact_store.open_incoming_async(declared) as opened:
        await artifact_store.recheck_opened_async(opened)
        assert os.pread(opened.worktree.descriptor, len(WORKTREE), 0) == WORKTREE
