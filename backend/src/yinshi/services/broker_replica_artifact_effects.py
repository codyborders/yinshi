"""Concrete ingest, verify, and publish effects for replica lifecycles."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from typing import Literal, cast

from yinshi.services.broker_artifact_store import (
    BrokerArtifactLimits,
    BrokerArtifactStore,
    BrokerArtifactStoreRejectedError,
    BrokerArtifactStoreUnresolvedError,
    ReplicaArtifactManifest,
)
from yinshi.services.broker_protocol import canonical_json
from yinshi.services.broker_replica_journal import (
    ArtifactReference,
    IngestReceipt,
    PublishReceipt,
    VerifyReceipt,
    replica_authority_from_request,
)
from yinshi.services.broker_replica_lifecycle import (
    ReplicaLifecycleContext,
    StageOutcomeUnknown,
    StageReconciliation,
    StageRejected,
)
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
    compute_replica_limits_sha256,
)
from yinshi.services.workspace_replica_publication import (
    ReplicaArtifactBinding,
    ReplicaArtifactSetDeclaration,
    ReplicaIdentity,
    ReplicaPublicationReceipt,
    ReplicaPublicationRejectedError,
    ReplicaPublicationUnresolvedError,
    ReplicaStoreLimits,
    ReplicaVerificationReceipt,
    WorkspaceReplicaPublicationStore,
)

StageName = Literal["ingest", "verify", "publish"]
RoleName = Literal["committed_bundle", "worktree", "index_objects"]
_REJECTION_CODE_BY_STAGE: dict[StageName, str] = {
    "ingest": "artifact_rejected",
    "verify": "verification_rejected",
    "publish": "publication_rejected",
}
_UNRESOLVED_REASON_BY_STAGE: dict[StageName, str] = {
    "ingest": "transfer_unknown",
    "verify": "verification_unknown",
    "publish": "publication_unknown",
}
_REFERENCE_KEYS = frozenset({"artifact_id", "byte_length", "sha256"})
_REJECTION_DOMAIN = b"yinshi-replica-stage-rejection-v1\x00"
_VERIFICATION_DOMAIN = b"yinshi-replica-stage-verification-v1\x00"
_PUBLICATION_DOMAIN = b"yinshi-replica-stage-publication-v1\x00"


def _digest_identity(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + canonical_json(value)).hexdigest()


def _rejected_receipt_id(stage: StageName, operation_id: str, code: str) -> str:
    return "rejected_" + _digest_identity(
        _REJECTION_DOMAIN,
        {"code": code, "operation_id": operation_id, "stage": stage},
    )


def _verification_receipt_id(
    operation_id: str,
    verification: ReplicaVerificationReceipt,
) -> str:
    return "verify_" + _digest_identity(
        _VERIFICATION_DOMAIN,
        {
            "artifact_set_sha256": verification.artifact_set_sha256,
            "operation_id": operation_id,
            "verification_receipt_id": verification.verification_receipt_id,
        },
    )


def _publication_receipt_id(
    operation_id: str,
    publication: ReplicaPublicationReceipt,
) -> str:
    return "publish_" + _digest_identity(
        _PUBLICATION_DOMAIN,
        {
            "artifact_set_sha256": publication.artifact_set_sha256,
            "operation_id": operation_id,
            "publication_marker_sha256": publication.publication_marker_sha256,
            "synchronization_receipt_id": publication.synchronization_receipt_id,
        },
    )


def _binding(role: RoleName, raw: object) -> ReplicaArtifactBinding:
    if not isinstance(raw, dict) or set(raw) != _REFERENCE_KEYS:
        raise ValueError("replica artifact reference fields are invalid")
    artifact_id = raw["artifact_id"]
    sha256 = raw["sha256"]
    byte_length = raw["byte_length"]
    if (
        not isinstance(artifact_id, str)
        or not isinstance(sha256, str)
        or type(byte_length) is not int
    ):
        raise ValueError("replica artifact reference is invalid")
    return ReplicaArtifactBinding(
        role=role,
        media_type=REPLICA_ARTIFACT_MEDIA_TYPES[role],
        artifact_id=artifact_id,
        sha256=sha256,
        byte_length=byte_length,
    )


def _incoming_manifest(
    declaration: ReplicaArtifactSetDeclaration,
) -> ReplicaArtifactManifest:
    def reference(binding: ReplicaArtifactBinding) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=binding.artifact_id,
            sha256=binding.sha256,
            byte_length=binding.byte_length,
        )

    return ReplicaArtifactManifest(
        operation_id=declaration.operation_id,
        bundle=reference(declaration.bundle),
        worktree=reference(declaration.worktree),
        index_objects=reference(declaration.index_objects),
    )


def broker_artifact_limits_for_replica(
    limits: ReplicaStoreLimits,
    *,
    chunk_bytes: int | None = None,
    transfer_timeout_seconds: float | None = None,
) -> BrokerArtifactLimits:
    """Convert one publication profile to matching incoming semantic limits."""
    if type(limits) is not ReplicaStoreLimits:
        raise TypeError("replica store limits are invalid")
    defaults = BrokerArtifactLimits()
    return BrokerArtifactLimits(
        max_bundle_bytes=limits.bundle.max_bundle_bytes,
        max_worktree_bytes=limits.worktree.max_total_bytes,
        max_index_objects_bytes=limits.index_objects.max_pack_bytes,
        max_set_bytes=limits.max_set_bytes,
        chunk_bytes=chunk_bytes if chunk_bytes is not None else defaults.chunk_bytes,
        transfer_timeout_seconds=(
            transfer_timeout_seconds
            if transfer_timeout_seconds is not None
            else defaults.transfer_timeout_seconds
        ),
    )


def _semantic_limits(limits: BrokerArtifactLimits) -> tuple[int, int, int, int]:
    return (
        limits.max_bundle_bytes,
        limits.max_worktree_bytes,
        limits.max_index_objects_bytes,
        limits.max_set_bytes,
    )


def _declared_limit_exceeded(payload: dict[str, object], limits: ReplicaStoreLimits) -> bool:
    maxima = {
        "bundle": limits.bundle.max_bundle_bytes,
        "worktree": limits.worktree.max_total_bytes,
        "index_objects": limits.index_objects.max_pack_bytes,
    }
    total = 0
    for name, maximum in maxima.items():
        value = payload.get(name)
        if not isinstance(value, dict):
            return False
        byte_length = value.get("byte_length")
        if type(byte_length) is not int:
            return False
        if byte_length < 0 or byte_length > maximum:
            return True
        total += byte_length
    return total > limits.max_set_bytes


def _declaration(
    effects: BrokerReplicaArtifactEffects,
    context: ReplicaLifecycleContext,
    stage: StageName,
) -> ReplicaArtifactSetDeclaration:
    request = context.request
    payload = cast(dict[str, object], request.payload)

    def rejected(code: str | None = None) -> StageRejected:
        resolved = code or _REJECTION_CODE_BY_STAGE[stage]
        return StageRejected(
            resolved,
            _rejected_receipt_id(stage, request.operation_id, resolved),
        )

    try:
        payload_authority = replica_authority_from_request(request)
        if payload_authority != context.authority:
            raise ValueError("replica authority differs from lifecycle context")
        if payload.get("limits_sha256") != effects._limits_sha256:
            raise rejected("limit_rejected" if stage == "ingest" else None)
        if stage == "ingest" and _declared_limit_exceeded(payload, effects._limits):
            raise rejected("limit_rejected")
        bundle = _binding("committed_bundle", payload.get("bundle"))
        worktree = _binding("worktree", payload.get("worktree"))
        index_objects = _binding("index_objects", payload.get("index_objects"))
        object_format = payload.get("object_format")
        repository_id = payload.get("repository_id")
        workspace_id = payload.get("workspace_id")
        source_state_sha256 = payload.get("source_state_sha256")
        reconciliation_fingerprint = payload.get("reconciliation_fingerprint")
        if (
            object_format not in {"sha1", "sha256"}
            or not isinstance(repository_id, str)
            or not isinstance(workspace_id, str)
            or not isinstance(source_state_sha256, str)
            or not isinstance(reconciliation_fingerprint, str)
        ):
            raise ValueError("replica declaration fields are invalid")
        declaration = ReplicaArtifactSetDeclaration(
            version=2,
            operation_id=request.operation_id,
            repository_id=repository_id,
            workspace_id=workspace_id,
            identity=ReplicaIdentity(
                physical_target_id=context.authority.physical_target_id,
                replica_generation=context.authority.replica_generation,
                execution_owner_id=context.authority.execution_owner_id,
            ),
            object_format=cast(Literal["sha1", "sha256"], object_format),
            source_state_sha256=source_state_sha256,
            reconciliation_fingerprint=reconciliation_fingerprint,
            bundle=bundle,
            worktree=worktree,
            index_objects=index_objects,
            limits=effects._limits,
        )
        expected_set = compute_replica_artifact_set_sha256(
            operation_id=request.operation_id,
            repository_id=repository_id,
            workspace_id=workspace_id,
            physical_target_id=context.authority.physical_target_id,
            replica_generation=context.authority.replica_generation,
            execution_owner_id=context.authority.execution_owner_id,
            object_format=object_format,
            source_state_sha256=source_state_sha256,
            reconciliation_fingerprint=reconciliation_fingerprint,
            bundle=asdict(bundle),
            worktree=asdict(worktree),
            index_objects=asdict(index_objects),
            limits_sha256=effects._limits_sha256,
        )
        if payload.get("artifact_set_sha256") != expected_set:
            raise ValueError("replica artifact set digest is invalid")
        return declaration
    except StageRejected:
        raise
    except (TypeError, ValueError):
        raise rejected() from None


class _IngestStageEffect:
    def __init__(self, owner: BrokerReplicaArtifactEffects) -> None:
        self._owner = owner

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[IngestReceipt]:
        return await self._inspect(context)

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[IngestReceipt]:
        return await self._inspect(context)

    async def _inspect(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[IngestReceipt]:
        stage: StageName = "ingest"
        operation_id = context.request.operation_id
        declaration = _declaration(self._owner, context, stage)
        try:
            receipt = await self._owner._incoming.inspect_incoming_async(
                _incoming_manifest(declaration)
            )
        except asyncio.CancelledError:
            raise
        except BrokerArtifactStoreRejectedError:
            code = _REJECTION_CODE_BY_STAGE[stage]
            raise StageRejected(
                code,
                _rejected_receipt_id(stage, operation_id, code),
            ) from None
        except (BrokerArtifactStoreUnresolvedError, OSError):
            raise StageOutcomeUnknown(_UNRESOLVED_REASON_BY_STAGE[stage]) from None
        return StageReconciliation(outcome="completed", receipt=receipt)


class _VerifyStageEffect:
    def __init__(self, owner: BrokerReplicaArtifactEffects) -> None:
        self._owner = owner

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[VerifyReceipt]:
        return await self._verify(context)

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[VerifyReceipt]:
        return await self._verify(context)

    async def _verify(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[VerifyReceipt]:
        stage: StageName = "verify"
        operation_id = context.request.operation_id
        declaration = _declaration(self._owner, context, stage)
        try:
            async with self._owner._incoming.open_incoming_async(
                _incoming_manifest(declaration)
            ) as opened:
                verification = await self._owner._publication.verify_opened_artifact_set(
                    declaration,
                    opened,
                    recheck_source=self._owner._incoming.recheck_opened_async,
                )
        except asyncio.CancelledError:
            raise
        except (BrokerArtifactStoreRejectedError, ReplicaPublicationRejectedError):
            code = _REJECTION_CODE_BY_STAGE[stage]
            raise StageRejected(
                code,
                _rejected_receipt_id(stage, operation_id, code),
            ) from None
        except (
            BrokerArtifactStoreUnresolvedError,
            ReplicaPublicationUnresolvedError,
            OSError,
        ):
            raise StageOutcomeUnknown(_UNRESOLVED_REASON_BY_STAGE[stage]) from None
        return StageReconciliation(
            outcome="completed",
            receipt=VerifyReceipt(
                receipt_id=_verification_receipt_id(operation_id, verification),
                bundle_sha256=verification.bundle.sha256,
                worktree_sha256=verification.worktree.sha256,
                index_objects_sha256=verification.index_objects.sha256,
                source_state_sha256=verification.source_state_sha256,
                reconciliation_fingerprint=verification.reconciliation_fingerprint,
                artifact_set_sha256=verification.artifact_set_sha256,
            ),
        )


class _PublishStageEffect:
    def __init__(self, owner: BrokerReplicaArtifactEffects) -> None:
        self._owner = owner

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[PublishReceipt]:
        stage: StageName = "publish"
        operation_id = context.request.operation_id
        declaration = _declaration(self._owner, context, stage)
        try:
            async with self._owner._incoming.open_incoming_async(
                _incoming_manifest(declaration)
            ) as opened:
                publication = await self._owner._publication.publish_opened_artifact_set(
                    declaration,
                    opened,
                    recheck_source=self._owner._incoming.recheck_opened_async,
                )
        except asyncio.CancelledError:
            raise
        except (BrokerArtifactStoreRejectedError, ReplicaPublicationRejectedError):
            code = _REJECTION_CODE_BY_STAGE[stage]
            raise StageRejected(
                code,
                _rejected_receipt_id(stage, operation_id, code),
            ) from None
        except (
            BrokerArtifactStoreUnresolvedError,
            ReplicaPublicationUnresolvedError,
            OSError,
        ):
            raise StageOutcomeUnknown(_UNRESOLVED_REASON_BY_STAGE[stage]) from None
        return StageReconciliation(
            outcome="completed",
            receipt=self._receipt(declaration, publication, context),
        )

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[PublishReceipt]:
        stage: StageName = "publish"
        operation_id = context.request.operation_id
        declaration = _declaration(self._owner, context, stage)
        try:
            publication = await self._owner._publication.reconcile_published_artifact_set(
                declaration
            )
        except asyncio.CancelledError:
            raise
        except ReplicaPublicationRejectedError:
            code = _REJECTION_CODE_BY_STAGE[stage]
            raise StageRejected(
                code,
                _rejected_receipt_id(stage, operation_id, code),
            ) from None
        except (ReplicaPublicationUnresolvedError, OSError):
            raise StageOutcomeUnknown(_UNRESOLVED_REASON_BY_STAGE[stage]) from None
        if publication is None:
            return StageReconciliation(outcome="not_applied")
        return StageReconciliation(
            outcome="completed",
            receipt=self._receipt(declaration, publication, context),
        )

    @staticmethod
    def _receipt(
        declaration: ReplicaArtifactSetDeclaration,
        publication: ReplicaPublicationReceipt,
        context: ReplicaLifecycleContext,
    ) -> PublishReceipt:
        expected_identity = ReplicaIdentity(
            physical_target_id=context.authority.physical_target_id,
            replica_generation=context.authority.replica_generation,
            execution_owner_id=context.authority.execution_owner_id,
        )
        if publication.identity != expected_identity:
            code = _REJECTION_CODE_BY_STAGE["publish"]
            raise StageRejected(
                code,
                _rejected_receipt_id("publish", declaration.operation_id, code),
            )
        return PublishReceipt(
            receipt_id=_publication_receipt_id(declaration.operation_id, publication),
            physical_target_id=context.authority.physical_target_id,
            replica_generation=context.authority.replica_generation,
            execution_owner_id=context.authority.execution_owner_id,
            publication_marker_sha256=publication.publication_marker_sha256,
            synchronization_receipt_id=publication.synchronization_receipt_id,
            artifact_set_sha256=publication.artifact_set_sha256,
        )


class BrokerReplicaArtifactEffects:
    """Own broker-backed ingest, verify, and publish stage effects."""

    def __init__(
        self,
        *,
        incoming: BrokerArtifactStore,
        publication: WorkspaceReplicaPublicationStore,
        limits: ReplicaStoreLimits,
    ) -> None:
        if type(incoming) is not BrokerArtifactStore:
            raise TypeError("broker artifact effects incoming store is invalid")
        if type(publication) is not WorkspaceReplicaPublicationStore:
            raise TypeError("broker artifact effects publication store is invalid")
        if type(limits) is not ReplicaStoreLimits:
            raise TypeError("broker artifact effects limits are invalid")
        limits_sha256 = compute_replica_limits_sha256(asdict(limits))
        expected_incoming = broker_artifact_limits_for_replica(limits)
        if _semantic_limits(incoming.limits) != _semantic_limits(expected_incoming):
            raise ValueError("artifact effect limits differ from incoming configuration")
        if publication.limits_sha256 != limits_sha256:
            raise ValueError("artifact effect limits differ from publication configuration")
        self._incoming = incoming
        self._publication = publication
        self._limits = limits
        self._limits_sha256 = limits_sha256
        self.ingest = _IngestStageEffect(self)
        self.verify = _VerifyStageEffect(self)
        self.publish = _PublishStageEffect(self)
