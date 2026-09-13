"""Verify authenticated same-socket dispatch for broker control requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerProtocolError,
    BrokerRequest,
    JsonValue,
    create_signed_request,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.broker_replica_journal import (
    ArtifactReference,
    ReplicaAuthority,
)
from yinshi.services.broker_replica_lifecycle import BrokerReplicaLifecycleCoordinator
from yinshi.services.execution_broker import BrokerControlService, BrokerService
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
)

REQUEST_KEY = Ed25519PrivateKey.generate()
RESPONSE_KEY = Ed25519PrivateKey.generate()
BROKER_INCARNATION = "broker_incarnation_00000000000001"
DATABASE_INCARNATION = "database_incarnation_0000000001"
APPLICATION_UID = 501
AUTHORITY = ReplicaAuthority(
    physical_target_id="physical_target_00000000000000001",
    replica_generation=7,
    execution_owner_id="execution_owner_00000000000000001",
)
BUNDLE = ArtifactReference("bundle_0000000000001", "1" * 64, 11)
WORKTREE = ArtifactReference("worktree_00000000001", "2" * 64, 12)
INDEX_OBJECTS = ArtifactReference("index_objects_0000001", "3" * 64, 13)
LIMITS_SHA256 = "4" * 64


def _reference(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def _binding(role: str, reference: ArtifactReference) -> dict[str, object]:
    return {
        **_reference(reference),
        "media_type": REPLICA_ARTIFACT_MEDIA_TYPES[role],
        "role": role,
    }


def _lifecycle_payload() -> dict[str, JsonValue]:
    operation_id = "a" * 32
    artifact_set_sha256 = compute_replica_artifact_set_sha256(
        operation_id=operation_id,
        repository_id="repository_0000000000000000000000",
        workspace_id="workspace_000000000000000000000000",
        physical_target_id=AUTHORITY.physical_target_id,
        replica_generation=AUTHORITY.replica_generation,
        execution_owner_id=AUTHORITY.execution_owner_id,
        object_format="sha256",
        source_state_sha256="5" * 64,
        reconciliation_fingerprint="6" * 64,
        bundle=_binding("committed_bundle", BUNDLE),
        worktree=_binding("worktree", WORKTREE),
        index_objects=_binding("index_objects", INDEX_OBJECTS),
        limits_sha256=LIMITS_SHA256,
    )
    return {
        "artifact_set_sha256": artifact_set_sha256,
        "authority": {
            "execution_owner_id": AUTHORITY.execution_owner_id,
            "physical_target_id": AUTHORITY.physical_target_id,
            "replica_generation": AUTHORITY.replica_generation,
        },
        "bundle": _reference(BUNDLE),
        "index_objects": _reference(INDEX_OBJECTS),
        "limits_sha256": LIMITS_SHA256,
        "object_format": "sha256",
        "reconciliation_fingerprint": "6" * 64,
        "repository_id": "repository_0000000000000000000000",
        "source_state_sha256": "5" * 64,
        "workspace_id": "workspace_000000000000000000000000",
        "worktree": _reference(WORKTREE),
    }


def _frame(request_type: str, payload: dict[str, JsonValue]) -> bytes:
    return create_signed_request(
        private_key=REQUEST_KEY,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id="a" * 32,
        request_type=request_type,
        nonce="request_nonce_000000000001",
        payload=payload,
    )


@dataclass
class FakeLaunchService:
    calls: list[tuple[bytes, int]] = field(default_factory=list)

    async def handle(self, frame: bytes, *, peer_uid: int) -> bytes:
        self.calls.append((frame, peer_uid))
        return b"launch-response"


@dataclass
class FakeReplicaCoordinator:
    calls: list[tuple[BrokerRequest, bytes, ReplicaAuthority]] = field(default_factory=list)

    async def run(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> bytes:
        self.calls.append((request, request_frame, authority))
        return b"replica-response"


def _service(
    launch: FakeLaunchService,
    coordinator: FakeReplicaCoordinator | None,
    *,
    enabled: bool,
) -> BrokerControlService:
    return BrokerControlService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=REQUEST_KEY.public_key(),
        broker_private_key=RESPONSE_KEY,
        launch_service=cast(BrokerService, launch),
        replica_coordinator=cast(BrokerReplicaLifecycleCoordinator | None, coordinator),
        replica_lifecycle_enabled=enabled,
    )


@pytest.mark.asyncio
async def test_lifecycle_dispatch_passes_exact_frame_and_signed_authority() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinator()
    service = _service(launch, coordinator, enabled=True)
    frame = _frame("replica.lifecycle", _lifecycle_payload())

    assert await service.handle(frame, peer_uid=APPLICATION_UID) == b"replica-response"
    assert launch.calls == []
    assert len(coordinator.calls) == 1
    request, received_frame, authority = coordinator.calls[0]
    assert received_frame is frame
    assert request == parse_signed_request(frame, public_key=REQUEST_KEY.public_key())
    assert authority == AUTHORITY


@pytest.mark.asyncio
async def test_launch_dispatch_preserves_existing_handler_frame_and_peer() -> None:
    launch = FakeLaunchService()
    service = _service(launch, None, enabled=False)
    frame = _frame("executor.launch", {})

    assert await service.handle(frame, peer_uid=APPLICATION_UID) == b"launch-response"
    assert launch.calls == [(frame, APPLICATION_UID)]


@pytest.mark.asyncio
async def test_disabled_lifecycle_returns_signed_response_without_coordinator() -> None:
    launch = FakeLaunchService()
    service = _service(launch, None, enabled=False)
    frame = _frame("replica.lifecycle", _lifecycle_payload())
    request = parse_signed_request(frame, public_key=REQUEST_KEY.public_key())

    response = await service.handle(frame, peer_uid=APPLICATION_UID)
    parsed = verify_broker_response(
        response,
        public_key=RESPONSE_KEY.public_key(),
        expected_request=request,
    )
    assert parsed.status == "error"
    assert parsed.error == "replica_lifecycle_disabled"
    assert parsed.result == {
        "code": "replica_lifecycle_disabled",
        "stage": "ingest",
        "state": "rejected",
    }
    malformed_payload = _lifecycle_payload()
    malformed_payload["authority"] = {}
    malformed_frame = _frame("replica.lifecycle", malformed_payload)
    malformed_request = parse_signed_request(
        malformed_frame,
        public_key=REQUEST_KEY.public_key(),
    )
    malformed_response = await service.handle(
        malformed_frame,
        peer_uid=APPLICATION_UID,
    )
    assert (
        verify_broker_response(
            malformed_response,
            public_key=RESPONSE_KEY.public_key(),
            expected_request=malformed_request,
        ).error
        == "replica_lifecycle_disabled"
    )
    assert launch.calls == []


@pytest.mark.asyncio
async def test_lifecycle_dispatch_rejects_peer_and_malformed_authority() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinator()
    service = _service(launch, coordinator, enabled=True)
    frame = _frame("replica.lifecycle", _lifecycle_payload())
    with pytest.raises(BrokerProtocolError, match="UID"):
        await service.handle(frame, peer_uid=APPLICATION_UID + 1)

    payload = _lifecycle_payload()
    payload["authority"] = {"physical_target_id": AUTHORITY.physical_target_id}
    malformed = _frame("replica.lifecycle", payload)
    with pytest.raises(BrokerProtocolError, match="lifecycle payload"):
        await service.handle(malformed, peer_uid=APPLICATION_UID)

    assert launch.calls == []
    assert coordinator.calls == []
