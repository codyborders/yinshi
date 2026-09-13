"""Check concrete artifact effects for broker replica lifecycles."""

from __future__ import annotations

import errno
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest

from yinshi.services.broker_artifact_store import (
    BrokerArtifactLimits,
    BrokerArtifactStore,
    BrokerArtifactStoreRejectedError,
    BrokerArtifactStoreUnresolvedError,
)
from yinshi.services.broker_protocol import BROKER_PROTOCOL_VERSION, BrokerRequest
from yinshi.services.broker_replica_artifact_effects import (
    BrokerReplicaArtifactEffects,
    broker_artifact_limits_for_replica,
)
from yinshi.services.broker_replica_journal import (
    ArtifactReference,
    IngestReceipt,
    PublishReceipt,
    ReplicaAuthority,
)
from yinshi.services.broker_replica_lifecycle import (
    ReplicaLifecycleContext,
    StageOutcomeUnknown,
    StageRejected,
)
from yinshi.services.replica_artifact_contract import (
    compute_replica_artifact_set_sha256,
    compute_replica_limits_sha256,
)
from yinshi.services.workspace_replica_publication import (
    ReplicaArtifactBinding,
    ReplicaArtifactSetDeclaration,
    ReplicaIdentity,
    ReplicaPublicationReceipt,
    ReplicaPublicationUnresolvedError,
    ReplicaStoreLimits,
    ReplicaVerificationReceipt,
    WorkspaceReplicaPublicationStore,
)

OPERATION_ID = "0123456789abcdef0123456789abcdef"
LIMITS = ReplicaStoreLimits()
LIMITS_SHA256 = compute_replica_limits_sha256(asdict(LIMITS))
IDENTITY = ReplicaIdentity(
    physical_target_id="physical_target_000000000000001",
    replica_generation=3,
    execution_owner_id="execution_owner_000000000000001",
)
AUTHORITY = ReplicaAuthority(
    physical_target_id=IDENTITY.physical_target_id,
    replica_generation=IDENTITY.replica_generation,
    execution_owner_id=IDENTITY.execution_owner_id,
)
BUNDLE = ReplicaArtifactBinding(
    role="committed_bundle",
    media_type="application/vnd.yinshi.git-bundle.v1",
    artifact_id="bundle_0000000000001",
    sha256="1" * 64,
    byte_length=11,
)
WORKTREE = ReplicaArtifactBinding(
    role="worktree",
    media_type="application/vnd.yinshi.replica-worktree.v1",
    artifact_id="worktree_00000000001",
    sha256="2" * 64,
    byte_length=12,
)
INDEX_OBJECTS = ReplicaArtifactBinding(
    role="index_objects",
    media_type="application/vnd.yinshi.git-index-objects-pack.v1",
    artifact_id="index_objects_0000001",
    sha256="3" * 64,
    byte_length=13,
)
DECLARATION = ReplicaArtifactSetDeclaration(
    version=2,
    operation_id=OPERATION_ID,
    repository_id="repository_0000000000000000000000",
    workspace_id="workspace_000000000000000000000000",
    identity=IDENTITY,
    object_format="sha256",
    source_state_sha256="4" * 64,
    reconciliation_fingerprint="5" * 64,
    bundle=BUNDLE,
    worktree=WORKTREE,
    index_objects=INDEX_OBJECTS,
    limits=LIMITS,
)
ARTIFACT_SET_SHA256 = compute_replica_artifact_set_sha256(
    operation_id=OPERATION_ID,
    repository_id=DECLARATION.repository_id,
    workspace_id=DECLARATION.workspace_id,
    physical_target_id=IDENTITY.physical_target_id,
    replica_generation=IDENTITY.replica_generation,
    execution_owner_id=IDENTITY.execution_owner_id,
    object_format=DECLARATION.object_format,
    source_state_sha256=DECLARATION.source_state_sha256,
    reconciliation_fingerprint=DECLARATION.reconciliation_fingerprint,
    bundle=asdict(BUNDLE),
    worktree=asdict(WORKTREE),
    index_objects=asdict(INDEX_OBJECTS),
    limits_sha256=LIMITS_SHA256,
)


def request_for(*, limits_sha256: str = LIMITS_SHA256) -> BrokerRequest:
    payload = {
        "artifact_set_sha256": ARTIFACT_SET_SHA256,
        "authority": {
            "execution_owner_id": AUTHORITY.execution_owner_id,
            "physical_target_id": AUTHORITY.physical_target_id,
            "replica_generation": AUTHORITY.replica_generation,
        },
        "bundle": {
            "artifact_id": BUNDLE.artifact_id,
            "byte_length": BUNDLE.byte_length,
            "sha256": BUNDLE.sha256,
        },
        "index_objects": {
            "artifact_id": INDEX_OBJECTS.artifact_id,
            "byte_length": INDEX_OBJECTS.byte_length,
            "sha256": INDEX_OBJECTS.sha256,
        },
        "limits_sha256": limits_sha256,
        "object_format": DECLARATION.object_format,
        "reconciliation_fingerprint": DECLARATION.reconciliation_fingerprint,
        "repository_id": DECLARATION.repository_id,
        "source_state_sha256": DECLARATION.source_state_sha256,
        "workspace_id": DECLARATION.workspace_id,
        "worktree": {
            "artifact_id": WORKTREE.artifact_id,
            "byte_length": WORKTREE.byte_length,
            "sha256": WORKTREE.sha256,
        },
    }
    return BrokerRequest(
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation="b" * 32,
        database_incarnation="d" * 32,
        connection_sequence=1,
        operation_id=OPERATION_ID,
        request_type="replica.lifecycle",
        nonce="nonce_000000000001",
        payload_digest="0" * 64,
        payload=payload,
    )


def lifecycle_context(
    *,
    request: BrokerRequest | None = None,
    authority: ReplicaAuthority = AUTHORITY,
) -> ReplicaLifecycleContext:
    return ReplicaLifecycleContext(
        request=request or request_for(),
        authority=authority,
        owner_token="owner_token_000000000000000",
    )


def stores(tmp_path: Path) -> tuple[BrokerArtifactStore, WorkspaceReplicaPublicationStore]:
    incoming_root = tmp_path / "incoming"
    publication_root = tmp_path / "published"
    incoming_root.mkdir(mode=0o700)
    publication_root.mkdir(mode=0o700)
    incoming = BrokerArtifactStore(
        incoming_root,
        limits=broker_artifact_limits_for_replica(LIMITS),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    publication = WorkspaceReplicaPublicationStore(
        publication_root,
        ceilings=LIMITS,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    return incoming, publication


def effects(tmp_path: Path) -> BrokerReplicaArtifactEffects:
    incoming, publication = stores(tmp_path)
    return BrokerReplicaArtifactEffects(
        incoming=incoming,
        publication=publication,
        limits=LIMITS,
    )


def verification() -> ReplicaVerificationReceipt:
    return ReplicaVerificationReceipt(
        verification_receipt_id="verification_receipt_00000000001",
        artifact_set_sha256=ARTIFACT_SET_SHA256,
        object_format="sha256",
        source_state_sha256=DECLARATION.source_state_sha256,
        reconciliation_fingerprint=DECLARATION.reconciliation_fingerprint,
        bundle=BUNDLE,
        worktree=WORKTREE,
        index_objects=INDEX_OBJECTS,
        bundle_refs_sha256="6" * 64,
        bundle_inventory_sha256="7" * 64,
        index_inventory_sha256="8" * 64,
    )


def publication() -> ReplicaPublicationReceipt:
    return ReplicaPublicationReceipt(
        publication_receipt_id="publication_receipt_0000000001",
        artifact_set_sha256=ARTIFACT_SET_SHA256,
        verification=verification(),
        identity=IDENTITY,
        source_state_sha256=DECLARATION.source_state_sha256,
        final_directory_identity="final_directory_000000000000001",
        publication_marker_sha256="9" * 64,
        synchronization_receipt_id="synchronization_00000000000001",
    )


@pytest.mark.asyncio
async def test_ingest_maps_exact_store_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_effects = effects(tmp_path)
    expected = IngestReceipt(
        receipt_id="ingest_receipt_000000000000001",
        bundle=ArtifactReference(BUNDLE.artifact_id, BUNDLE.sha256, BUNDLE.byte_length),
        worktree=ArtifactReference(
            WORKTREE.artifact_id,
            WORKTREE.sha256,
            WORKTREE.byte_length,
        ),
        index_objects=ArtifactReference(
            INDEX_OBJECTS.artifact_id,
            INDEX_OBJECTS.sha256,
            INDEX_OBJECTS.byte_length,
        ),
    )

    async def inspect(_self: BrokerArtifactStore, _manifest: object) -> IngestReceipt:
        return expected

    monkeypatch.setattr(BrokerArtifactStore, "inspect_incoming_async", inspect)
    completed = await artifact_effects.ingest.apply(lifecycle_context())
    assert completed.outcome == "completed"
    assert completed.receipt == expected


@pytest.mark.asyncio
async def test_verify_and_publish_map_authority_bound_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_effects = effects(tmp_path)

    @asynccontextmanager
    async def opened(
        _self: BrokerArtifactStore,
        _manifest: object,
    ) -> AsyncIterator[object]:
        yield object()

    async def verify(_self: object, *_args: object, **_kwargs: object) -> object:
        return verification()

    async def publish(_self: object, *_args: object, **_kwargs: object) -> object:
        return publication()

    async def reconcile(_self: object, *_args: object) -> object:
        return publication()

    monkeypatch.setattr(BrokerArtifactStore, "open_incoming_async", opened)
    monkeypatch.setattr(WorkspaceReplicaPublicationStore, "verify_opened_artifact_set", verify)
    monkeypatch.setattr(WorkspaceReplicaPublicationStore, "publish_opened_artifact_set", publish)
    monkeypatch.setattr(
        WorkspaceReplicaPublicationStore,
        "reconcile_published_artifact_set",
        reconcile,
    )

    verified = await artifact_effects.verify.apply(lifecycle_context())
    assert verified.outcome == "completed"
    assert verified.receipt is not None
    assert verified.receipt.artifact_set_sha256 == ARTIFACT_SET_SHA256

    published = await artifact_effects.publish.apply(lifecycle_context())
    assert published.outcome == "completed"
    assert isinstance(published.receipt, PublishReceipt)
    assert published.receipt.physical_target_id == AUTHORITY.physical_target_id
    assert await artifact_effects.publish.reconcile(lifecycle_context()) == published


@pytest.mark.asyncio
async def test_missing_ingest_and_bad_limits_reject_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_effects = effects(tmp_path)

    async def reject(_self: BrokerArtifactStore, _manifest: object) -> IngestReceipt:
        raise BrokerArtifactStoreRejectedError("missing")

    monkeypatch.setattr(BrokerArtifactStore, "inspect_incoming_async", reject)
    with pytest.raises(StageRejected) as first:
        await artifact_effects.ingest.apply(lifecycle_context())
    with pytest.raises(StageRejected) as second:
        await artifact_effects.ingest.reconcile(lifecycle_context())
    assert first.value.status == "artifact_rejected"
    assert first.value.receipt_id == second.value.receipt_id

    with pytest.raises(StageRejected) as limits:
        await artifact_effects.ingest.apply(
            lifecycle_context(request=request_for(limits_sha256="f" * 64))
        )
    assert limits.value.status == "limit_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [errno.EIO, errno.ENOSPC, errno.EMFILE])
async def test_operational_incoming_failure_is_stage_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    from yinshi.services import broker_artifact_store as module

    incoming, publication_store = stores(tmp_path)
    artifact_effects = BrokerReplicaArtifactEffects(
        incoming=incoming,
        publication=publication_store,
        limits=LIMITS,
    )
    incoming_root = tmp_path / "incoming"
    real_lstat = os.lstat

    def fail_root(path: object, *args: object, **kwargs: object):
        if path == incoming_root:
            raise OSError(error_number, "storage fault")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", fail_root)
    with pytest.raises(StageOutcomeUnknown) as unknown:
        await artifact_effects.ingest.apply(lifecycle_context())
    assert unknown.value.reason == "transfer_unknown"


@pytest.mark.asyncio
async def test_verify_publish_and_reconcile_preserve_stage_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_effects = effects(tmp_path)

    @asynccontextmanager
    async def fail_open(
        _self: BrokerArtifactStore,
        _manifest: object,
    ) -> AsyncIterator[object]:
        raise BrokerArtifactStoreUnresolvedError("read fault")
        yield object()

    monkeypatch.setattr(BrokerArtifactStore, "open_incoming_async", fail_open)
    for stage_name, reason in (
        ("verify", "verification_unknown"),
        ("publish", "publication_unknown"),
    ):
        with pytest.raises(StageOutcomeUnknown) as unknown:
            await getattr(artifact_effects, stage_name).apply(lifecycle_context())
        assert unknown.value.reason == reason

    async def fail_reconcile(_self: object, _declaration: object) -> object:
        raise ReplicaPublicationUnresolvedError("sync fault")

    monkeypatch.setattr(
        WorkspaceReplicaPublicationStore,
        "reconcile_published_artifact_set",
        fail_reconcile,
    )
    with pytest.raises(StageOutcomeUnknown) as reconciled:
        await artifact_effects.publish.reconcile(lifecycle_context())
    assert reconciled.value.reason == "publication_unknown"


@pytest.mark.asyncio
async def test_publish_reconcile_reports_confirmed_absence(tmp_path: Path) -> None:
    artifact_effects = effects(tmp_path)
    reconciled = await artifact_effects.publish.reconcile(lifecycle_context())
    assert reconciled.outcome == "not_applied"
    assert reconciled.receipt is None


def test_effects_require_matching_semantic_limit_profiles(tmp_path: Path) -> None:
    _incoming, publication_store = stores(tmp_path)
    changed_limits = ReplicaStoreLimits(max_set_bytes=LIMITS.max_set_bytes - 1)
    changed_root = tmp_path / "changed"
    changed_root.mkdir(mode=0o700)
    changed_incoming = BrokerArtifactStore(
        changed_root,
        limits=broker_artifact_limits_for_replica(changed_limits),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    with pytest.raises(ValueError, match="publication configuration"):
        BrokerReplicaArtifactEffects(
            incoming=changed_incoming,
            publication=publication_store,
            limits=changed_limits,
        )

    narrower_root = tmp_path / "narrower"
    narrower_root.mkdir(mode=0o700)
    expected = broker_artifact_limits_for_replica(LIMITS)
    narrower = BrokerArtifactStore(
        narrower_root,
        limits=BrokerArtifactLimits(
            max_bundle_bytes=expected.max_bundle_bytes - 1,
            max_worktree_bytes=expected.max_worktree_bytes,
            max_index_objects_bytes=expected.max_index_objects_bytes,
            max_set_bytes=expected.max_set_bytes,
        ),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    with pytest.raises(ValueError, match="incoming configuration"):
        BrokerReplicaArtifactEffects(
            incoming=narrower,
            publication=publication_store,
            limits=LIMITS,
        )

    transport_root = tmp_path / "transport"
    transport_root.mkdir(mode=0o700)
    transport_only = broker_artifact_limits_for_replica(
        LIMITS,
        chunk_bytes=4096,
        transfer_timeout_seconds=47.0,
    )
    compatible = BrokerArtifactStore(
        transport_root,
        limits=transport_only,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    BrokerReplicaArtifactEffects(
        incoming=compatible,
        publication=publication_store,
        limits=LIMITS,
    )
