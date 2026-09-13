"""Authenticated control metadata for the broker artifact upload data plane."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from yinshi.services.broker_artifact_store import (
    BrokerArtifactStore,
    BrokerArtifactStoreRejectedError,
    BrokerArtifactStoreUnresolvedError,
    ReplicaArtifactManifest,
)
from yinshi.services.broker_protocol import (
    BrokerProtocolError,
    BrokerRequest,
    JsonValue,
    create_signed_response,
    parse_signed_request,
)
from yinshi.services.broker_replica_journal import ArtifactReference, ReplicaAuthority
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
    validate_distinct_artifact_ids,
    validate_replica_identifier,
)

UPLOAD_REQUEST_TYPE = "replica.artifacts.upload"
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_PAYLOAD_KEYS = frozenset(
    {
        "artifact_set_sha256",
        "authority",
        "bundle",
        "index_objects",
        "limits_sha256",
        "object_format",
        "reconciliation_fingerprint",
        "repository_id",
        "source_state_sha256",
        "workspace_id",
        "worktree",
    }
)
_AUTHORITY_KEYS = frozenset({"execution_owner_id", "physical_target_id", "replica_generation"})
_REFERENCE_KEYS = frozenset({"artifact_id", "byte_length", "sha256"})


@dataclass(frozen=True, slots=True)
class ReplicaUploadDeclaration:
    """Fully authenticated metadata for one fixed-order artifact upload."""

    request: BrokerRequest
    manifest: ReplicaArtifactManifest
    authority: ReplicaAuthority
    artifact_set_sha256: str


def _digest(value: object, description: str) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{description} is invalid")
    return value


def _artifact_reference(value: object, description: str) -> ArtifactReference:
    if not isinstance(value, dict) or set(value) != _REFERENCE_KEYS:
        raise ValueError(f"{description} reference fields are invalid")
    return ArtifactReference(
        artifact_id=cast(str, value["artifact_id"]),
        sha256=cast(str, value["sha256"]),
        byte_length=cast(int, value["byte_length"]),
    )


def _authority(value: object) -> ReplicaAuthority:
    if not isinstance(value, dict) or set(value) != _AUTHORITY_KEYS:
        raise ValueError("artifact upload authority fields are invalid")
    return ReplicaAuthority(
        physical_target_id=cast(str, value["physical_target_id"]),
        replica_generation=cast(int, value["replica_generation"]),
        execution_owner_id=cast(str, value["execution_owner_id"]),
    )


def _binding(role: str, reference: ArtifactReference) -> dict[str, object]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "media_type": REPLICA_ARTIFACT_MEDIA_TYPES[role],
        "role": role,
        "sha256": reference.sha256,
    }


def parse_upload_declaration(
    request: BrokerRequest,
    *,
    expected_limits_sha256: str,
) -> ReplicaUploadDeclaration:
    """Validate exact upload metadata and its complete artifact-set binding."""
    if request.request_type != UPLOAD_REQUEST_TYPE:
        raise BrokerProtocolError("artifact upload request type is invalid")
    payload = request.payload
    if set(payload) != _PAYLOAD_KEYS:
        raise ValueError("artifact upload payload fields are invalid")
    limits_sha256 = _digest(payload["limits_sha256"], "artifact limits SHA-256")
    if limits_sha256 != expected_limits_sha256:
        raise ValueError("artifact limits profile differs from broker configuration")
    repository_id = validate_replica_identifier(payload["repository_id"], "repository ID")
    workspace_id = validate_replica_identifier(payload["workspace_id"], "workspace ID")
    object_format = payload["object_format"]
    if type(object_format) is not str or object_format not in {"sha1", "sha256"}:
        raise ValueError("artifact object format is invalid")
    source_state_sha256 = _digest(payload["source_state_sha256"], "source state SHA-256")
    reconciliation_fingerprint = _digest(
        payload["reconciliation_fingerprint"],
        "reconciliation fingerprint",
    )
    artifact_set_sha256 = _digest(payload["artifact_set_sha256"], "artifact set SHA-256")
    authority = _authority(payload["authority"])
    bundle = _artifact_reference(payload["bundle"], "bundle")
    worktree = _artifact_reference(payload["worktree"], "worktree")
    index_objects = _artifact_reference(payload["index_objects"], "index objects")
    validate_distinct_artifact_ids(
        (bundle.artifact_id, worktree.artifact_id, index_objects.artifact_id)
    )
    expected_artifact_set_sha256 = compute_replica_artifact_set_sha256(
        operation_id=request.operation_id,
        repository_id=repository_id,
        workspace_id=workspace_id,
        physical_target_id=authority.physical_target_id,
        replica_generation=authority.replica_generation,
        execution_owner_id=authority.execution_owner_id,
        object_format=object_format,
        source_state_sha256=source_state_sha256,
        reconciliation_fingerprint=reconciliation_fingerprint,
        bundle=_binding("committed_bundle", bundle),
        worktree=_binding("worktree", worktree),
        index_objects=_binding("index_objects", index_objects),
        limits_sha256=limits_sha256,
    )
    if expected_artifact_set_sha256 != artifact_set_sha256:
        raise ValueError("artifact set SHA-256 differs from authenticated metadata")
    return ReplicaUploadDeclaration(
        request=request,
        manifest=ReplicaArtifactManifest(
            operation_id=request.operation_id,
            bundle=bundle,
            worktree=worktree,
            index_objects=index_objects,
        ),
        authority=authority,
        artifact_set_sha256=artifact_set_sha256,
    )


class BrokerArtifactUploadService:
    """Authenticate and receive one upload without using the lifecycle journal."""

    def __init__(
        self,
        *,
        broker_incarnation: str,
        database_incarnation: str,
        application_uid: int,
        application_public_key: Ed25519PublicKey,
        broker_private_key: Ed25519PrivateKey,
        artifact_store: BrokerArtifactStore,
        expected_limits_sha256: str,
        enabled: bool,
    ) -> None:
        if (
            type(broker_incarnation) is not str
            or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
            or type(database_incarnation) is not str
            or _TOKEN_PATTERN.fullmatch(database_incarnation) is None
        ):
            raise ValueError("artifact upload incarnation is invalid")
        if type(application_uid) is not int or application_uid < 0:
            raise ValueError("artifact upload application UID is invalid")
        if not isinstance(application_public_key, Ed25519PublicKey):
            raise TypeError("artifact upload request key is invalid")
        if not isinstance(broker_private_key, Ed25519PrivateKey):
            raise TypeError("artifact upload response key is invalid")
        if type(artifact_store) is not BrokerArtifactStore:
            raise TypeError("artifact upload store is invalid")
        if type(enabled) is not bool:
            raise TypeError("artifact upload gate is invalid")
        _digest(expected_limits_sha256, "configured artifact limits SHA-256")
        self._broker_incarnation = broker_incarnation
        self._database_incarnation = database_incarnation
        self._application_uid = application_uid
        self._application_public_key = application_public_key
        self._broker_private_key = broker_private_key
        self._artifact_store = artifact_store
        self._expected_limits_sha256 = expected_limits_sha256
        self._enabled = enabled

    def authenticate(self, frame: bytes, *, peer_uid: int) -> BrokerRequest:
        """Authenticate transport peer and signed request before reading artifact bytes."""
        if type(peer_uid) is not int or peer_uid != self._application_uid:
            raise BrokerProtocolError("artifact upload peer UID is unauthorized")
        request = parse_signed_request(frame, public_key=self._application_public_key)
        if request.broker_incarnation != self._broker_incarnation:
            raise BrokerProtocolError("artifact upload broker incarnation is invalid")
        if request.database_incarnation != self._database_incarnation:
            raise BrokerProtocolError("artifact upload database incarnation is invalid")
        if request.request_type != UPLOAD_REQUEST_TYPE:
            raise BrokerProtocolError("artifact upload request type is invalid")
        return request

    def _response(
        self,
        request: BrokerRequest,
        *,
        status: str,
        error: str | None,
        result: dict[str, JsonValue],
    ) -> bytes:
        return create_signed_response(
            request,
            private_key=self._broker_private_key,
            status=status,
            error=error,
            result=result,
        )

    async def handle(
        self,
        frame: bytes,
        reader: asyncio.StreamReader,
        *,
        peer_uid: int,
    ) -> bytes:
        """Process one fixed-order upload and return one signed terminal result."""
        request = self.authenticate(frame, peer_uid=peer_uid)
        if not isinstance(reader, asyncio.StreamReader):
            raise BrokerProtocolError("artifact upload reader is invalid")
        try:
            declaration = parse_upload_declaration(
                request,
                expected_limits_sha256=self._expected_limits_sha256,
            )
        except (TypeError, ValueError):
            return self._response(
                request,
                status="error",
                error="upload_rejected",
                result={"stored": False},
            )
        try:
            if not self._enabled:
                await self._artifact_store.consume(declaration.manifest, reader)
                return self._response(
                    request,
                    status="error",
                    error="upload_disabled",
                    result={"stored": False},
                )
            receipt = await self._artifact_store.receive(declaration.manifest, reader)
        except BrokerArtifactStoreRejectedError:
            return self._response(
                request,
                status="error",
                error="upload_rejected",
                result={"stored": False},
            )
        except BrokerArtifactStoreUnresolvedError:
            return self._response(
                request,
                status="error",
                error="upload_unknown",
                result={"stored": False},
            )
        return self._response(
            request,
            status="ok",
            error=None,
            result={
                "artifact_set_sha256": declaration.artifact_set_sha256,
                "receipt_id": receipt.receipt_id,
                "stored": True,
            },
        )
