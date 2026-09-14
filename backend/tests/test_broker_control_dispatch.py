"""Verify authenticated same-socket dispatch for broker control requests."""

from __future__ import annotations

import asyncio
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
from yinshi.services.broker_replica_journal_v2 import (
    ReplicaDrainContinuation,
    replica_drain_continuation_from_request,
)
from yinshi.services.broker_replica_lifecycle_v2 import (
    BrokerReplicaLifecycleCoordinatorV2,
)
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


def _drain_payload() -> dict[str, JsonValue]:
    return {
        "admission_receipt_id": "admission_receipt_000000001",
        "application_drain_intent_receipt_id": "drain_intent_0000000001",
        "authority": {
            "execution_owner_id": AUTHORITY.execution_owner_id,
            "physical_target_id": AUTHORITY.physical_target_id,
            "replica_generation": AUTHORITY.replica_generation,
        },
        "initial_request_sha256": "7" * 64,
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
class FakeReplicaCoordinatorV2:
    started: list[tuple[BrokerRequest, bytes, ReplicaAuthority]] = field(
        default_factory=list,
    )
    continued: list[tuple[BrokerRequest, bytes, ReplicaDrainContinuation]] = field(
        default_factory=list,
    )

    async def start(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> bytes:
        self.started.append((request, request_frame, authority))
        return b"replica-response"

    async def continue_drain(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        continuation: ReplicaDrainContinuation,
    ) -> bytes:
        self.continued.append((request, request_frame, continuation))
        return b"replica-drain-response"


@dataclass
class V1ShapedReplicaCoordinator:
    """Model the retired V1 coordinator contract with only run()."""

    async def run(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> bytes:
        del request, request_frame, authority
        return b"v1-response"


def _service(
    launch: FakeLaunchService,
    coordinator: object | None,
    *,
    enabled: bool,
    replica_lifecycle_timeout_seconds: float = 360.0,
) -> BrokerControlService:
    return BrokerControlService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=REQUEST_KEY.public_key(),
        broker_private_key=RESPONSE_KEY,
        launch_service=cast(BrokerService, launch),
        replica_coordinator=cast(
            BrokerReplicaLifecycleCoordinatorV2 | None,
            coordinator,
        ),
        replica_lifecycle_enabled=enabled,
        replica_lifecycle_timeout_seconds=replica_lifecycle_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_lifecycle_dispatch_passes_exact_frame_and_signed_authority() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=True)
    frame = _frame("replica.lifecycle", _lifecycle_payload())

    assert await service.handle(frame, peer_uid=APPLICATION_UID) == b"replica-response"
    assert launch.calls == []
    assert coordinator.continued == []
    assert len(coordinator.started) == 1
    request, received_frame, authority = coordinator.started[0]
    assert received_frame is frame
    assert request == parse_signed_request(frame, public_key=REQUEST_KEY.public_key())
    assert authority == AUTHORITY


@pytest.mark.asyncio
async def test_drain_dispatch_passes_exact_frame_and_parsed_continuation() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=True)
    frame = _frame("replica.drain", _drain_payload())

    response = await service.handle(frame, peer_uid=APPLICATION_UID)
    assert response == b"replica-drain-response"
    assert launch.calls == []
    assert coordinator.started == []
    assert len(coordinator.continued) == 1
    request, received_frame, continuation = coordinator.continued[0]
    assert received_frame is frame
    assert request == parse_signed_request(frame, public_key=REQUEST_KEY.public_key())
    assert continuation == replica_drain_continuation_from_request(request)
    assert continuation.authority == AUTHORITY
    assert continuation.initial_request_sha256 == "7" * 64
    assert continuation.admission_receipt_id == "admission_receipt_000000001"
    assert continuation.application_drain_intent_receipt_id == "drain_intent_0000000001"


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
async def test_disabled_drain_precedes_continuation_parsing_without_coordinator() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=False)
    frame = _frame("replica.drain", _drain_payload())
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

    malformed_payload = _drain_payload()
    del malformed_payload["initial_request_sha256"]
    malformed_frame = _frame("replica.drain", malformed_payload)
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
    assert coordinator.started == []
    assert coordinator.continued == []


@pytest.mark.asyncio
async def test_base_authentication_precedes_disabled_replica_response() -> None:
    launch = FakeLaunchService()
    service = _service(launch, None, enabled=False)
    for request_type, payload in (
        ("replica.lifecycle", _lifecycle_payload()),
        ("replica.drain", _drain_payload()),
    ):
        frame = _frame(request_type, payload)
        with pytest.raises(BrokerProtocolError, match="UID"):
            await service.handle(frame, peer_uid=APPLICATION_UID + 1)
        stale_frame = create_signed_request(
            private_key=REQUEST_KEY,
            protocol_version=BROKER_PROTOCOL_VERSION,
            broker_incarnation="stale_incarnation_000000000001",
            database_incarnation=DATABASE_INCARNATION,
            connection_sequence=1,
            operation_id="a" * 32,
            request_type=request_type,
            nonce="request_nonce_000000000001",
            payload=payload,
        )
        with pytest.raises(BrokerProtocolError, match="incarnation is stale"):
            await service.handle(stale_frame, peer_uid=APPLICATION_UID)
    assert launch.calls == []


@pytest.mark.asyncio
async def test_malformed_or_tampered_drain_rejects_before_coordinator() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=True)

    missing_field = _drain_payload()
    del missing_field["admission_receipt_id"]
    with pytest.raises(BrokerProtocolError, match="payload is invalid"):
        await service.handle(_frame("replica.drain", missing_field), peer_uid=APPLICATION_UID)

    tampered = _drain_payload()
    tampered["initial_request_sha256"] = "not-even-hex"
    with pytest.raises(BrokerProtocolError, match="payload is invalid"):
        await service.handle(_frame("replica.drain", tampered), peer_uid=APPLICATION_UID)

    tampered = _drain_payload()
    tampered["admission_receipt_id"] = "short"
    with pytest.raises(BrokerProtocolError, match="payload is invalid"):
        await service.handle(_frame("replica.drain", tampered), peer_uid=APPLICATION_UID)

    tampered = _drain_payload()
    tampered["authority"] = {}
    with pytest.raises(BrokerProtocolError, match="payload is invalid"):
        await service.handle(_frame("replica.drain", tampered), peer_uid=APPLICATION_UID)

    assert launch.calls == []
    assert coordinator.started == []
    assert coordinator.continued == []


@pytest.mark.asyncio
async def test_replica_routes_reject_malformed_lifecycle_payload_before_coordinator() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=True)
    payload = _lifecycle_payload()
    payload["authority"] = {"physical_target_id": AUTHORITY.physical_target_id}
    with pytest.raises(BrokerProtocolError, match="payload is invalid"):
        await service.handle(_frame("replica.lifecycle", payload), peer_uid=APPLICATION_UID)

    assert launch.calls == []
    assert coordinator.started == []
    assert coordinator.continued == []


@pytest.mark.asyncio
async def test_unknown_request_type_is_rejected() -> None:
    launch = FakeLaunchService()
    coordinator = FakeReplicaCoordinatorV2()
    service = _service(launch, coordinator, enabled=True)
    frame = _frame("replica.unknown", {})
    with pytest.raises(BrokerProtocolError, match="not supported"):
        await service.handle(frame, peer_uid=APPLICATION_UID)
    assert launch.calls == []
    assert coordinator.started == []
    assert coordinator.continued == []


def test_constructor_accepts_only_v2_coordinator_contract() -> None:
    launch = FakeLaunchService()
    service = _service(launch, FakeReplicaCoordinatorV2(), enabled=True)
    assert service.application_uid == APPLICATION_UID

    with pytest.raises(TypeError, match="replica coordinator"):
        _service(launch, V1ShapedReplicaCoordinator(), enabled=False)
    with pytest.raises(TypeError, match="replica coordinator"):
        _service(launch, V1ShapedReplicaCoordinator(), enabled=True)
    with pytest.raises(TypeError, match="replica coordinator"):
        _service(launch, object(), enabled=True)


@pytest.mark.asyncio
async def test_replica_routes_are_bounded_by_replica_timeout() -> None:
    launch = FakeLaunchService()

    class HangingCoordinator:
        async def start(
            self,
            request: BrokerRequest,
            request_frame: bytes,
            authority: ReplicaAuthority,
        ) -> bytes:
            del request, request_frame, authority
            await asyncio.sleep(5)
            raise AssertionError("start effect outlived the replica timeout")

        async def continue_drain(
            self,
            request: BrokerRequest,
            request_frame: bytes,
            continuation: ReplicaDrainContinuation,
        ) -> bytes:
            del request, request_frame, continuation
            await asyncio.sleep(5)
            raise AssertionError("drain effect outlived the replica timeout")

    coordinator = cast(BrokerReplicaLifecycleCoordinatorV2, HangingCoordinator())
    lifecycle_service = _service(
        launch,
        coordinator,
        enabled=True,
        replica_lifecycle_timeout_seconds=0.05,
    )
    with pytest.raises(TimeoutError):
        await lifecycle_service.handle(
            _frame("replica.lifecycle", _lifecycle_payload()),
            peer_uid=APPLICATION_UID,
        )
    with pytest.raises(TimeoutError):
        await lifecycle_service.handle(
            _frame("replica.drain", _drain_payload()),
            peer_uid=APPLICATION_UID,
        )
    assert launch.calls == []
