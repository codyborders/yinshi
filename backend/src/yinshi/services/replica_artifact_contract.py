"""Canonical identity for broker-managed replica artifact sets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

REPLICA_OPERATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
REPLICA_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}\Z"
)
REPLICA_ARTIFACT_FILENAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "committed_bundle": "committed.bundle",
        "worktree": "worktree.yra",
        "index_objects": "index-objects.pack",
    }
)
REPLICA_ARTIFACT_MEDIA_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "committed_bundle": "application/vnd.yinshi.git-bundle.v1",
        "worktree": "application/vnd.yinshi.replica-worktree.v1",
        "index_objects": "application/vnd.yinshi.git-index-objects-pack.v1",
    }
)


def validate_replica_identifier(value: object, description: str) -> str:
    if type(value) is not str or not REPLICA_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{description} is invalid")
    return value


def validate_replica_operation_id(value: object) -> str:
    if type(value) is not str or not REPLICA_OPERATION_PATTERN.fullmatch(value):
        raise ValueError("replica operation ID is invalid")
    return value


def validate_distinct_artifact_ids(artifact_ids: Sequence[str]) -> None:
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("replica artifact IDs must be distinct")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def compute_replica_limits_sha256(limits: object) -> str:
    """Bind every immutable artifact verification limit."""
    return hashlib.sha256(b"yinshi-replica-limits-v1\x00" + _canonical_json(limits)).hexdigest()


def compute_replica_artifact_set_sha256(
    *,
    operation_id: str,
    repository_id: str,
    workspace_id: str,
    physical_target_id: str,
    replica_generation: int,
    execution_owner_id: str,
    object_format: str,
    source_state_sha256: str,
    reconciliation_fingerprint: str,
    bundle: Mapping[str, object],
    worktree: Mapping[str, object],
    index_objects: Mapping[str, object],
    limits_sha256: str,
) -> str:
    """Return one shared artifact-set identity for journal and publication code."""
    value = {
        "version": 2,
        "operation_id": operation_id,
        "repository_id": repository_id,
        "workspace_id": workspace_id,
        "identity": {
            "physical_target_id": physical_target_id,
            "replica_generation": replica_generation,
            "execution_owner_id": execution_owner_id,
        },
        "object_format": object_format,
        "source_state_sha256": source_state_sha256,
        "reconciliation_fingerprint": reconciliation_fingerprint,
        "bundle": dict(bundle),
        "worktree": dict(worktree),
        "index_objects": dict(index_objects),
        "limits_sha256": limits_sha256,
    }
    return hashlib.sha256(
        b"yinshi-replica-artifact-set-v2\x00" + _canonical_json(value)
    ).hexdigest()
