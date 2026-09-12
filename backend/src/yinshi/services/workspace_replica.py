"""Pure replica reconciliation core (A4, approved offline packet).

This module is the internal deterministic planning and replay core for the
future reconciliation owner. Validated snapshots and recorded observations
produce either a deterministic ordered mutation plan or a precise refusal.
It never performs effects: no database access, no Git parsing or processes,
no filesystem mutation, no provider or runtime calls. Ownership boundaries
and the full contract are documented in docs/workspace-os-isolation.md.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from itertools import pairwise


class ReplicaReconciliationError(ValueError):
    """Base error for replica reconciliation inputs."""


class SnapshotRejected(ReplicaReconciliationError):
    """Reject a snapshot or entry that cannot participate in reconciliation."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


@dataclass(frozen=True)
class NodeIdentity:
    """Concrete filesystem and node identity from a trusted observation."""

    filesystem_id: str
    node_id: str


@dataclass(frozen=True)
class ExistingNode:
    """Desired-state reference to a concrete node already in the baseline."""

    identity: NodeIdentity


@dataclass(frozen=True)
class PreparedNode:
    """Desired-state reference resolved by a durable preparation fact."""

    slot_id: str


@dataclass(frozen=True)
class Location:
    """One raw location in an explicit reconciliation namespace."""

    namespace: str
    raw_path: bytes


@dataclass(frozen=True)
class ParentBinding:
    """Immediate changed directory, with existing or durably prepared identity."""

    location: Location
    reference: ExistingNode | PreparedNode


@dataclass(frozen=True)
class ParentObservation:
    """Fresh concrete identity of one immediate entry-owning directory."""

    location: Location
    identity: NodeIdentity


@dataclass(frozen=True)
class NodeState:
    """Concrete observed bytes and physical node identity."""

    location: Location
    identity: NodeIdentity
    kind: str
    content: bytes
    executable: bool
    nlink: int
    privilege_bits: bool


@dataclass(frozen=True)
class AbsentState:
    """Explicit absence at one location, distinct from an empty file."""

    location: Location


@dataclass(frozen=True)
class RefState:
    """Concrete selected-ref representations and preserved metadata."""

    selected_ref: bytes
    head_binding: bytes
    resolved_oid: str | None
    packed_fallback_oid: str | None
    head: NodeState
    loose_ref: NodeState | AbsentState
    packed_refs: NodeState | AbsentState
    reflogs: tuple[NodeState | AbsentState, ...]
    unrelated_metadata: tuple[NodeState | AbsentState, ...]


@dataclass(frozen=True)
class IndexState:
    """Concrete index storage and independent semantic entries."""

    storage: NodeState | AbsentState
    entries: tuple[IndexEntry, ...]
    sparse_index: bool


@dataclass(frozen=True)
class ReplicaState:
    """Concrete baseline or fresh replica state."""

    generation: int
    replica_generation: int
    index: IndexState
    refs: RefState
    manifest: tuple[ManifestEntry, ...]
    working_nodes: tuple[NodeState, ...]


@dataclass(frozen=True)
class DesiredNodeState:
    """Desired bytes with an existing-node or prepared-slot reference."""

    location: Location
    reference: ExistingNode | PreparedNode
    kind: str
    content: bytes
    executable: bool
    nlink: int
    privilege_bits: bool


@dataclass(frozen=True)
class DesiredIndexState:
    """Desired index storage and independent semantic entries."""

    storage: DesiredNodeState | AbsentState
    entries: tuple[IndexEntry, ...]
    sparse_index: bool


@dataclass(frozen=True)
class DesiredRefState:
    """Desired selected-ref representations and preserved metadata."""

    selected_ref: bytes
    head_binding: bytes
    resolved_oid: str | None
    packed_fallback_oid: str | None
    head: DesiredNodeState
    loose_ref: DesiredNodeState | AbsentState
    packed_refs: DesiredNodeState | AbsentState
    reflogs: tuple[DesiredNodeState | AbsentState, ...]
    unrelated_metadata: tuple[DesiredNodeState | AbsentState, ...]


@dataclass(frozen=True)
class DesiredReplicaState:
    """Desired published state without fabricated node identities."""

    generation: int
    replica_generation: int
    index: DesiredIndexState
    refs: DesiredRefState
    manifest: tuple[ManifestEntry, ...]
    working_nodes: tuple[DesiredNodeState, ...]


@dataclass(frozen=True)
class OwnershipMarkerSpec:
    """Marker template whose fingerprint-bearing payload is derived after binding."""

    slot_id: str
    destination: Location
    parent: ParentBinding
    initial: AbsentState
    payload_prefix: bytes
    executable: bool


@dataclass(frozen=True)
class DestinationObject:
    """Durable physical storage for one semantic object identity.

    ``content_sha256`` covers the exact bytes in ``storage.content``. Those
    bytes can be a compressed loose object. This digest is independent from
    the semantic payload digest in ``RequiredObject``.
    """

    object_id: str
    storage: NodeState
    parent: ParentBinding
    content_sha256: str
    content_sync_receipt_id: str
    parent_sync_receipt_id: str


@dataclass(frozen=True)
class RequiredObject:
    """Semantic object inventory entry supplied by validated traversal.

    ``byte_length`` counts only the uncompressed Git object payload.
    ``content_sha256`` covers only that payload. ``object_id`` covers the Git
    object header plus payload under the selected object format. An external
    inventory producer validates this relationship because this core neither
    parses Git objects nor receives semantic payload bytes.
    """

    object_id: str
    kind: str
    byte_length: int
    content_sha256: str
    source_slot_id: str


@dataclass(frozen=True)
class PreparationSlot:
    """Private no-replace materialization slot with both changed parents."""

    slot_id: str
    destination: Location
    materialization_parent: ParentBinding
    installation_parent: ParentBinding
    initial: NodeState | AbsentState
    desired_kind: str
    desired_content: bytes
    executable: bool


@dataclass(frozen=True)
class QuarantineSlot:
    """Exact same-filesystem move with source, destination, and restoration parents."""

    slot_id: str
    source: Location
    source_identity: NodeIdentity
    source_parent: ParentBinding
    destination: Location
    destination_parent: ParentBinding
    restoration_parent: ParentBinding | None
    initial: AbsentState


@dataclass(frozen=True)
class Boundary:
    """Protected root or ancestor chain with concrete identities."""

    boundary_id: str
    role: str
    location: Location
    identity: NodeIdentity
    ancestors: tuple[NodeState, ...]


@dataclass(frozen=True)
class InventoryFact:
    """Validated semantic and destination inventory receipt."""

    receipt_id: str
    original_owner_id: str
    object_format: str
    required_object_ids: tuple[str, ...]
    destination_object_ids: tuple[str, ...]
    specification_fingerprint: str


@dataclass(frozen=True)
class ExportFact:
    """Original export receipt linked to one immutable specification."""

    receipt_id: str
    physical_target_id: str
    replica_generation: int
    specification_fingerprint: str


@dataclass(frozen=True)
class DrainFact:
    """Original broker drain receipt bound to target and replica generation."""

    receipt_id: str
    physical_target_id: str
    replica_generation: int


@dataclass(frozen=True)
class OriginalAcknowledgmentFact:
    """Original runtime acknowledgment without reconciliation re-ownership."""

    receipt_id: str
    execution_owner_id: str


@dataclass(frozen=True)
class RetainedInputs:
    """Supplied external facts retained with their original ownership."""

    acknowledgment: OriginalAcknowledgmentFact
    drain: DrainFact
    export: ExportFact
    inventory: InventoryFact


@dataclass(frozen=True)
class ControlReceipt:
    """Exact durable receipt identity and the owner that can create it."""

    receipt_id: str
    owner: str


@dataclass(frozen=True)
class ControlState:
    """Durable control receipts, with explicit absence for every transition."""

    application_intent: ControlReceipt | None
    reservation: ControlReceipt | None
    broker_acceptance: ControlReceipt | None
    exclusion_receipts: tuple[ControlReceipt, ...]
    admission_gate: ControlReceipt | None
    drain: ControlReceipt | None
    export_validation: ControlReceipt | None
    inventory_validation: ControlReceipt | None
    baseline_acceptance: ControlReceipt | None
    verification: ControlReceipt | None
    publication: ControlReceipt | None
    destination_binding: ControlReceipt | None
    binding_acknowledgment: ControlReceipt | None
    release_intent: ControlReceipt | None
    physical_release: ControlReceipt | None
    logical_release: ControlReceipt | None
    admission: ControlReceipt | None
    effect_receipts: tuple[ControlReceipt, ...]


@dataclass(frozen=True)
class FreshObservation:
    """Fresh concrete replica, parent identities, storage, boundaries, and receipts."""

    identity: OperationIdentity
    replica_state: ReplicaState
    boundaries: tuple[Boundary, ...]
    parents: tuple[ParentObservation, ...]
    storage: tuple[NodeState | AbsentState, ...]
    control: ControlState


@dataclass(frozen=True)
class InventoryLimits:
    """Explicit limits checked before action expansion."""

    max_paths: int
    max_objects: int
    max_total_bytes: int
    max_depth: int
    max_transitions: int


@dataclass(frozen=True)
class ReconciliationSpec:
    """Complete immutable input for one reconciliation protocol."""

    version: int
    identity: OperationIdentity
    baseline: ReplicaState
    desired: DesiredReplicaState
    boundaries: tuple[Boundary, ...]
    preparation_slots: tuple[PreparationSlot, ...]
    quarantine_slots: tuple[QuarantineSlot, ...]
    ownership_markers: tuple[OwnershipMarkerSpec, ...]
    required_objects: tuple[RequiredObject, ...]
    destination_objects: tuple[DestinationObject, ...]
    limits: InventoryLimits


@dataclass(frozen=True)
class OperationIdentity:
    """Immutable operation identity and original external receipt references."""

    incarnation: str
    binding_id: str
    selected_authority: str
    operation_id: str
    reservation_id: str
    destination_binding: str
    physical_target_id: str
    generation: int
    replica_generation: int
    acknowledgment_receipt_id: str
    drain_receipt_id: str
    export_receipt_id: str
    object_format: str
    inventory_receipt_id: str


@dataclass(frozen=True)
class BoundSpecification:
    """Canonical specification, domain fingerprints, and exact transition count."""

    spec: ReconciliationSpec
    fingerprint: str
    baseline_fingerprint: str
    desired_fingerprint: str
    transition_count: int


def _canonical_projection(value: object) -> object:
    """Project immutable protocol values into unambiguous canonical JSON data."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _canonical_projection(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if type(value) is bytes:
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    if type(value) is tuple:
        return [_canonical_projection(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise SnapshotRejected("invalid_input_type", "specification values must be immutable")


def _canonical_bytes(value: object) -> bytes:
    """Encode one protocol value as canonical UTF-8 JSON bytes."""
    return json.dumps(
        _canonical_projection(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _protocol_fingerprint(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_bytes(value)).hexdigest()


def _corrected_decoded_byte_count(
    value: object,
    visiting: set[int] | None = None,
    depth: int = 0,
) -> int:
    """Count raw bytes before canonical projection, with bounded traversal."""
    if depth > 256:
        raise SnapshotRejected(
            "inventory_limit_exceeded",
            "specification structure exceeds the traversal depth limit",
        )
    if is_dataclass(value) and not isinstance(value, type):
        marker = id(value)
        if visiting is None:
            visiting = set()
        if marker in visiting:
            raise SnapshotRejected(
                "invalid_input_type",
                "specification values must be immutable and acyclic",
            )
        visiting.add(marker)
        try:
            return sum(
                _corrected_decoded_byte_count(
                    getattr(value, field.name),
                    visiting,
                    depth + 1,
                )
                for field in fields(value)
            )
        finally:
            visiting.discard(marker)
    if type(value) is bytes:
        return len(value)
    if type(value) is tuple:
        return sum(_corrected_decoded_byte_count(item, visiting, depth + 1) for item in value)
    return 0


def _corrected_require_str(value: object, field_name: str) -> str:
    if type(value) is not str or value == "":
        raise SnapshotRejected("invalid_input_type", f"{field_name} must be a non-empty string")
    return value


def _corrected_require_bytes(value: object, field_name: str) -> bytes:
    if type(value) is not bytes:
        raise SnapshotRejected("invalid_input_type", f"{field_name} must be immutable bytes")
    return value


def _corrected_require_int(value: object, field_name: str, minimum: int = 0) -> int:
    if type(value) is not int:
        raise SnapshotRejected("invalid_input_type", f"{field_name} must be an exact integer")
    if value < minimum:
        raise SnapshotRejected("invalid_specification", f"{field_name} is below its minimum")
    if value > 2**63 - 1:
        raise SnapshotRejected("invalid_specification", f"{field_name} exceeds its maximum")
    return value


def _corrected_require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise SnapshotRejected("invalid_input_type", f"{field_name} must be an exact boolean")
    return value


def _corrected_require_tuple(value: object, field_name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise SnapshotRejected("invalid_input_type", f"{field_name} must be a tuple")
    return value


def _corrected_validate_identity(identity: object, field_name: str) -> NodeIdentity:
    if type(identity) is not NodeIdentity:
        raise SnapshotRejected("invalid_input_type", f"{field_name} identity type is invalid")
    _corrected_require_str(identity.filesystem_id, f"{field_name}.filesystem_id")
    _corrected_require_str(identity.node_id, f"{field_name}.node_id")
    return identity


def _corrected_validate_location(location: object, field_name: str) -> Location:
    if type(location) is not Location:
        raise SnapshotRejected("invalid_input_type", f"{field_name} location type is invalid")
    namespace = _corrected_require_str(location.namespace, f"{field_name}.namespace")
    if namespace not in {"working", "admin", "registration", "excluded", "private"}:
        raise SnapshotRejected("invalid_specification", f"{field_name} namespace is unsupported")
    raw_path = _corrected_require_bytes(location.raw_path, f"{field_name}.raw_path")
    if _is_unsafe_path(raw_path):
        raise SnapshotRejected("unsafe_path", f"{field_name} contains an unsafe raw path")
    return location


def _corrected_validate_node_semantics(
    field_name: str,
    kind: str,
    content: bytes,
    executable: bool,
    nlink: int,
    privilege_bits: bool,
    *,
    content_reason: str,
    content_detail: str,
) -> None:
    """Reject privilege bits, directory content, hardlinks, and executable symlinks."""
    if privilege_bits:
        raise SnapshotRejected("unsupported_privilege_bits", f"{field_name} has privilege bits")
    if kind == "directory" and content != b"":
        raise SnapshotRejected(content_reason, content_detail)
    if kind != "directory" and nlink != 1:
        raise SnapshotRejected("unsupported_hardlink", f"{field_name} has multiple links")
    if kind == "symlink" and executable:
        raise SnapshotRejected("invalid_node_state", f"{field_name} symlink is executable")


def _corrected_validate_node(node: object, field_name: str) -> NodeState:
    if type(node) is not NodeState:
        raise SnapshotRejected("invalid_input_type", f"{field_name} node type is invalid")
    _corrected_validate_location(node.location, f"{field_name}.location")
    _corrected_validate_identity(node.identity, f"{field_name}.identity")
    kind = _corrected_require_str(node.kind, f"{field_name}.kind")
    if kind not in {"file", "directory", "symlink"}:
        raise SnapshotRejected("unsupported_kind", f"{field_name} kind is unsupported")
    content = _corrected_require_bytes(node.content, f"{field_name}.content")
    executable = _corrected_require_bool(node.executable, f"{field_name}.executable")
    nlink = _corrected_require_int(node.nlink, f"{field_name}.nlink", 1)
    privilege_bits = _corrected_require_bool(node.privilege_bits, f"{field_name}.privilege_bits")
    _corrected_validate_node_semantics(
        field_name,
        kind,
        content,
        executable,
        nlink,
        privilege_bits,
        content_reason="invalid_node_state",
        content_detail=f"{field_name} directory carries content",
    )
    return node


def _corrected_validate_storage(state: object, field_name: str) -> NodeState | AbsentState:
    if type(state) is NodeState:
        return _corrected_validate_node(state, field_name)
    if type(state) is AbsentState:
        _corrected_validate_location(state.location, f"{field_name}.location")
        return state
    raise SnapshotRejected("invalid_input_type", f"{field_name} storage type is invalid")


def _corrected_require_administrative_file(state: object, field_name: str) -> None:
    """Require every present administrative node to be a regular non-executable file."""
    if not isinstance(state, (NodeState, DesiredNodeState)):
        return
    if state.kind != "file" or state.executable or state.nlink != 1:
        raise SnapshotRejected(
            "invalid_node_state",
            f"{field_name} must be a regular non-executable file",
        )


def _corrected_validate_index_entry(entry: object, object_format: str) -> IndexEntry:
    if type(entry) is not IndexEntry:
        raise SnapshotRejected("invalid_input_type", "index entry type is invalid")
    raw_path = _corrected_require_bytes(entry.raw_path, "index.raw_path")
    if _is_unsafe_path(raw_path):
        raise SnapshotRejected("unsafe_path", "index entry path is unsafe")
    if type(entry.stage) is not int:
        raise SnapshotRejected("invalid_input_type", "index.stage must be an exact integer")
    _corrected_require_str(entry.mode, "index.mode")
    _corrected_require_str(entry.object_id, "index.object_id")
    for field_name in ("intent_to_add", "skip_worktree", "assume_unchanged"):
        _corrected_require_bool(getattr(entry, field_name), f"index.{field_name}")
    if entry.stage not in (0, 1, 2, 3):
        raise SnapshotRejected(
            "invalid_index_stage",
            "index stage is outside stages 0 through 3",
        )
    if entry.mode not in {"100644", "100755", "120000"}:
        raise SnapshotRejected("unsupported_index_mode", "index mode is unsupported")
    _corrected_validate_oid(entry.object_id, object_format, "index.object_id")
    return entry


def _corrected_validate_manifest_entry(entry: object) -> ManifestEntry:
    if type(entry) is not ManifestEntry:
        raise SnapshotRejected("invalid_input_type", "manifest entry type is invalid")
    raw_path = _corrected_require_bytes(entry.raw_path, "manifest.raw_path")
    if _is_unsafe_path(raw_path):
        raise SnapshotRejected("unsafe_path", "manifest path is unsafe")
    kind = _corrected_require_str(entry.kind, "manifest.kind")
    if kind not in {"file", "directory", "symlink"}:
        raise SnapshotRejected("unsupported_kind", "manifest kind is unsupported")
    content = _corrected_require_bytes(entry.content, "manifest.content")
    executable = _corrected_require_bool(entry.executable, "manifest.executable")
    nlink = _corrected_require_int(entry.nlink, "manifest.nlink", 1)
    privilege_bits = _corrected_require_bool(entry.privilege_bits, "manifest.privilege_bits")
    _corrected_validate_node_semantics(
        "manifest",
        kind,
        content,
        executable,
        nlink,
        privilege_bits,
        content_reason="invalid_manifest",
        content_detail="directory manifest entry carries content",
    )
    return entry


def _corrected_validate_oid(value: object, object_format: str, field_name: str) -> str:
    oid = _corrected_require_str(value, field_name)
    length = 40 if object_format == "sha1" else 64
    if len(oid) != length or any(character not in "0123456789abcdef" for character in oid):
        raise SnapshotRejected("invalid_object_id", f"{field_name} is not canonical")
    return oid


def _corrected_canonical_index_entries(
    entries: tuple[IndexEntry, ...],
) -> tuple[IndexEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: (entry.raw_path, entry.stage)))


def _corrected_validate_entries(
    index_entries: object,
    manifest: object,
    object_format: str,
) -> None:
    index_tuple = _corrected_require_tuple(index_entries, "index.entries")
    manifest_tuple = _corrected_require_tuple(manifest, "manifest")
    paths_index: set[bytes] = set()
    seen_stage_entries: set[tuple[bytes, int]] = set()
    stages_by_path: dict[bytes, set[int]] = {}
    for index_value in index_tuple:
        checked_index = _corrected_validate_index_entry(index_value, object_format)
        stage_key = (checked_index.raw_path, checked_index.stage)
        if stage_key in seen_stage_entries:
            raise SnapshotRejected(
                "duplicate_index_entry",
                "index path and stage are duplicated",
            )
        seen_stage_entries.add(stage_key)
        paths_index.add(checked_index.raw_path)
        stages_by_path.setdefault(checked_index.raw_path, set()).add(checked_index.stage)
    for stages in stages_by_path.values():
        if 0 in stages and len(stages) != 1:
            raise SnapshotRejected(
                "unmerged_index",
                "stage-zero entries cannot coexist with conflict stages",
            )
    for path in paths_index:
        if any(other != path and _is_directory_prefix(path, other) for other in paths_index):
            raise SnapshotRejected("index_prefix_collision", "index paths have a prefix collision")

    entries_by_path: dict[bytes, ManifestEntry] = {}
    for manifest_value in manifest_tuple:
        checked_manifest = _corrected_validate_manifest_entry(manifest_value)
        if checked_manifest.raw_path in entries_by_path:
            raise SnapshotRejected("duplicate_manifest_entry", "manifest path is duplicated")
        entries_by_path[checked_manifest.raw_path] = checked_manifest
    for path in entries_by_path:
        components = path.split(b"/")
        for length in range(1, len(components)):
            parent_path = b"/".join(components[:length])
            parent = entries_by_path.get(parent_path)
            if parent is None or parent.kind != "directory":
                raise SnapshotRejected(
                    "incomplete_topology",
                    "every non-root manifest parent must be an explicit directory",
                )


def _corrected_storage_key(state: NodeState | AbsentState) -> tuple[str, bytes]:
    return state.location.namespace, state.location.raw_path


def _corrected_require_unique_storage_locations(
    values: tuple[NodeState | DesiredNodeState | AbsentState, ...],
    field_name: str,
) -> None:
    seen: set[tuple[str, bytes]] = set()
    for state in values:
        key = (state.location.namespace, state.location.raw_path)
        if key in seen:
            raise SnapshotRejected("duplicate_location", f"{field_name} location is duplicated")
        seen.add(key)


def _corrected_require_admin_namespace(
    state: NodeState | DesiredNodeState | AbsentState,
    field_name: str,
) -> None:
    """Require every administrative storage field to use the admin namespace."""
    if state.location.namespace != "admin":
        raise SnapshotRejected(
            "invalid_specification",
            f"{field_name} must use the admin namespace",
        )


def _corrected_administrative_registrations(
    replica: ReplicaState | DesiredReplicaState,
) -> list[tuple[NodeState | DesiredNodeState | AbsentState, str]]:
    """Pair administrative storage fields of one phase with logical field names."""
    return [
        (replica.index.storage, "index.storage"),
        (replica.refs.head, "refs.head"),
        (replica.refs.loose_ref, "refs.loose_ref"),
        (replica.refs.packed_refs, "refs.packed_refs"),
        *[(state, "refs.reflogs") for state in replica.refs.reflogs],
        *[(state, "refs.unrelated_metadata") for state in replica.refs.unrelated_metadata],
    ]


def _corrected_static_registrations(
    replica: ReplicaState | DesiredReplicaState,
) -> list[tuple[NodeState | DesiredNodeState | AbsentState, str]]:
    """Pair every static storage field of one phase with logical field names."""
    return [
        *_corrected_administrative_registrations(replica),
        *[(node, "working_nodes") for node in replica.working_nodes],
    ]


def _corrected_static_location_registry(
    replica: ReplicaState | DesiredReplicaState,
    phase: str,
) -> dict[tuple[str, bytes], tuple[str, NodeState | DesiredNodeState | AbsentState]]:
    """Register each static storage field once per physical location and phase."""
    registry: dict[
        tuple[str, bytes],
        tuple[str, NodeState | DesiredNodeState | AbsentState],
    ] = {}
    for state, field_name in _corrected_static_registrations(replica):
        key = (state.location.namespace, state.location.raw_path)
        prior_field = registry.get(key)
        if prior_field is not None:
            raise SnapshotRejected(
                "duplicate_location",
                f"one {phase} location holds the {prior_field[0]} and {field_name} fields",
            )
        registry[key] = (field_name, state)
    return registry


def _corrected_validate_replica(
    replica: object,
    object_format: str,
    field_name: str,
) -> ReplicaState:
    if type(replica) is not ReplicaState:
        raise SnapshotRejected("invalid_input_type", f"{field_name} replica type is invalid")
    _corrected_require_int(replica.generation, f"{field_name}.generation")
    _corrected_require_int(replica.replica_generation, f"{field_name}.replica_generation")
    if type(replica.index) is not IndexState:
        raise SnapshotRejected("invalid_input_type", f"{field_name}.index type is invalid")
    _corrected_validate_storage(replica.index.storage, f"{field_name}.index.storage")
    _corrected_require_bool(replica.index.sparse_index, f"{field_name}.index.sparse_index")
    if replica.index.sparse_index:
        raise SnapshotRejected("sparse_index_unsupported", "sparse index is unsupported")
    _corrected_validate_entries(replica.index.entries, replica.manifest, object_format)
    if type(replica.index.storage) is AbsentState and replica.index.entries:
        raise SnapshotRejected(
            "invalid_index_semantics",
            f"{field_name} absent index storage cannot retain entries",
        )
    if type(replica.refs) is not RefState:
        raise SnapshotRejected("invalid_input_type", f"{field_name}.refs type is invalid")
    _corrected_validate_ref_state(replica.refs, object_format, field_name)
    _corrected_require_unique_storage_locations(
        (
            replica.index.storage,
            replica.refs.head,
            replica.refs.loose_ref,
            replica.refs.packed_refs,
            *replica.refs.reflogs,
            *replica.refs.unrelated_metadata,
        ),
        f"{field_name}.administrative_storage",
    )
    for admin_state, admin_field in _corrected_administrative_registrations(replica):
        _corrected_require_admin_namespace(admin_state, f"{field_name}.{admin_field}")
    working_nodes = _corrected_require_tuple(replica.working_nodes, f"{field_name}.working_nodes")
    nodes_by_path: dict[bytes, NodeState] = {}
    for node in working_nodes:
        checked = _corrected_validate_node(node, f"{field_name}.working_node")
        if checked.location.namespace != "working":
            raise SnapshotRejected("invalid_specification", "working node has wrong namespace")
        if checked.location.raw_path in nodes_by_path:
            raise SnapshotRejected("duplicate_location", "working location is duplicated")
        nodes_by_path[checked.location.raw_path] = checked
    if set(nodes_by_path) != {entry.raw_path for entry in replica.manifest}:
        raise SnapshotRejected("incomplete_topology", "manifest and working nodes differ")
    for entry in replica.manifest:
        node = nodes_by_path[entry.raw_path]
        semantic = (node.kind, node.content, node.executable, node.nlink, node.privilege_bits)
        expected = (entry.kind, entry.content, entry.executable, entry.nlink, entry.privilege_bits)
        if semantic != expected:
            raise SnapshotRejected("manifest_node_mismatch", "manifest and node semantics differ")
    return replica


def _corrected_validate_ref_semantics(
    refs: RefState | DesiredRefState,
    object_format: str,
    field_name: str,
) -> None:
    expected_binding = b"symbolic:" + refs.selected_ref
    if (
        refs.head_binding != expected_binding
        or refs.head.content != refs.head_binding
        or refs.loose_ref.location.namespace != "admin"
        or refs.loose_ref.location.raw_path != refs.selected_ref
    ):
        raise SnapshotRejected(
            "invalid_ref_semantics",
            f"{field_name} selected-ref binding is inconsistent",
        )
    if type(refs.loose_ref) is NodeState or type(refs.loose_ref) is DesiredNodeState:
        try:
            loose_oid = refs.loose_ref.content.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SnapshotRejected(
                "invalid_ref_semantics",
                f"{field_name} selected loose ref is not ASCII",
            ) from exc
        _corrected_validate_oid(loose_oid, object_format, f"{field_name}.refs.loose_ref")
        expected_oid: str | None = loose_oid
    else:
        expected_oid = refs.packed_fallback_oid
    if refs.resolved_oid != expected_oid:
        raise SnapshotRejected(
            "invalid_ref_semantics",
            f"{field_name} resolved OID differs from selected physical source",
        )


def _corrected_validate_ref_state(refs: RefState, object_format: str, field_name: str) -> None:
    _corrected_require_bytes(refs.selected_ref, f"{field_name}.refs.selected_ref")
    _corrected_require_bytes(refs.head_binding, f"{field_name}.refs.head_binding")
    if refs.resolved_oid is not None:
        _corrected_validate_oid(refs.resolved_oid, object_format, "refs.resolved_oid")
    if refs.packed_fallback_oid is not None:
        _corrected_validate_oid(refs.packed_fallback_oid, object_format, "refs.packed_fallback_oid")
    _corrected_validate_node(refs.head, f"{field_name}.refs.head")
    _corrected_validate_storage(refs.loose_ref, f"{field_name}.refs.loose_ref")
    _corrected_validate_storage(refs.packed_refs, f"{field_name}.refs.packed_refs")
    for collection_name in ("reflogs", "unrelated_metadata"):
        collection = _corrected_require_tuple(getattr(refs, collection_name), collection_name)
        for state in collection:
            _corrected_validate_storage(state, f"{field_name}.refs.{collection_name}")
    _corrected_validate_ref_semantics(refs, object_format, field_name)


def _corrected_validate_desired(
    desired: object,
    object_format: str,
    baseline_nodes: dict[NodeIdentity, NodeState],
    slots: dict[str, PreparationSlot],
    namespace_filesystems: dict[str, str],
) -> DesiredReplicaState:
    if type(desired) is not DesiredReplicaState:
        raise SnapshotRejected("invalid_input_type", "desired replica type is invalid")
    _corrected_require_int(desired.generation, "desired.generation")
    _corrected_require_int(desired.replica_generation, "desired.replica_generation")
    if type(desired.index) is not DesiredIndexState:
        raise SnapshotRejected("invalid_input_type", "desired index type is invalid")
    _corrected_require_bool(desired.index.sparse_index, "desired.index.sparse_index")
    if desired.index.sparse_index:
        raise SnapshotRejected("sparse_index_unsupported", "sparse index is unsupported")
    _corrected_validate_entries(desired.index.entries, desired.manifest, object_format)
    if type(desired.index.storage) is AbsentState and desired.index.entries:
        raise SnapshotRejected(
            "invalid_index_semantics",
            "desired absent index storage cannot retain entries",
        )
    _corrected_validate_desired_storage(
        desired.index.storage,
        "desired.index.storage",
        baseline_nodes,
        slots,
        namespace_filesystems,
    )
    if type(desired.refs) is not DesiredRefState:
        raise SnapshotRejected("invalid_input_type", "desired refs type is invalid")
    _corrected_require_bytes(desired.refs.selected_ref, "desired.refs.selected_ref")
    _corrected_require_bytes(desired.refs.head_binding, "desired.refs.head_binding")
    if desired.refs.resolved_oid is not None:
        _corrected_validate_oid(
            desired.refs.resolved_oid, object_format, "desired.refs.resolved_oid"
        )
    if desired.refs.packed_fallback_oid is not None:
        _corrected_validate_oid(
            desired.refs.packed_fallback_oid,
            object_format,
            "desired.refs.packed_fallback_oid",
        )
    _corrected_validate_desired_node(
        desired.refs.head,
        "desired.refs.head",
        baseline_nodes,
        slots,
        namespace_filesystems,
    )
    for name in ("loose_ref", "packed_refs"):
        _corrected_validate_desired_storage(
            getattr(desired.refs, name),
            f"desired.refs.{name}",
            baseline_nodes,
            slots,
            namespace_filesystems,
        )
    for name in ("reflogs", "unrelated_metadata"):
        collection = _corrected_require_tuple(getattr(desired.refs, name), f"desired.refs.{name}")
        for state in collection:
            _corrected_validate_desired_storage(
                state,
                f"desired.refs.{name}",
                baseline_nodes,
                slots,
                namespace_filesystems,
            )
    _corrected_require_unique_storage_locations(
        (
            desired.index.storage,
            desired.refs.head,
            desired.refs.loose_ref,
            desired.refs.packed_refs,
            *desired.refs.reflogs,
            *desired.refs.unrelated_metadata,
        ),
        "desired.administrative_storage",
    )
    for admin_state, admin_field in _corrected_administrative_registrations(desired):
        _corrected_require_admin_namespace(admin_state, f"desired.{admin_field}")
    _corrected_validate_ref_semantics(desired.refs, object_format, "desired")
    nodes = _corrected_require_tuple(desired.working_nodes, "desired.working_nodes")
    nodes_by_path: dict[bytes, DesiredNodeState] = {}
    for node in nodes:
        checked = _corrected_validate_desired_node(
            node,
            "desired.working_node",
            baseline_nodes,
            slots,
            namespace_filesystems,
        )
        if checked.location.namespace != "working":
            raise SnapshotRejected(
                "invalid_specification", "desired working node has wrong namespace"
            )
        if checked.location.raw_path in nodes_by_path:
            raise SnapshotRejected("duplicate_location", "desired working location is duplicated")
        nodes_by_path[checked.location.raw_path] = checked
    if set(nodes_by_path) != {entry.raw_path for entry in desired.manifest}:
        raise SnapshotRejected("incomplete_topology", "desired manifest and working nodes differ")
    for entry in desired.manifest:
        node = nodes_by_path[entry.raw_path]
        semantic = (node.kind, node.content, node.executable, node.nlink, node.privilege_bits)
        expected = (entry.kind, entry.content, entry.executable, entry.nlink, entry.privilege_bits)
        if semantic != expected:
            raise SnapshotRejected("manifest_node_mismatch", "desired manifest and nodes differ")
    return desired


def _corrected_validate_desired_node(
    node: object,
    field_name: str,
    baseline_nodes: dict[NodeIdentity, NodeState],
    slots: dict[str, PreparationSlot],
    namespace_filesystems: dict[str, str],
) -> DesiredNodeState:
    if type(node) is not DesiredNodeState:
        raise SnapshotRejected("invalid_input_type", f"{field_name} desired node type is invalid")
    _corrected_validate_location(node.location, f"{field_name}.location")
    kind = _corrected_require_str(node.kind, f"{field_name}.kind")
    if kind not in {"file", "directory", "symlink"}:
        raise SnapshotRejected("unsupported_kind", f"{field_name} kind is unsupported")
    content = _corrected_require_bytes(node.content, f"{field_name}.content")
    executable = _corrected_require_bool(node.executable, f"{field_name}.executable")
    nlink = _corrected_require_int(node.nlink, f"{field_name}.nlink", 1)
    privilege_bits = _corrected_require_bool(node.privilege_bits, f"{field_name}.privilege_bits")
    _corrected_validate_node_semantics(
        field_name,
        kind,
        content,
        executable,
        nlink,
        privilege_bits,
        content_reason="invalid_node_state",
        content_detail=f"{field_name} directory carries content",
    )
    if type(node.reference) is ExistingNode:
        _corrected_validate_identity(node.reference.identity, f"{field_name}.reference")
        baseline = baseline_nodes.get(node.reference.identity)
        if baseline is None:
            raise SnapshotRejected("unexplained_desired_node", "desired existing node is unknown")
        desired_semantic = (
            node.location,
            node.kind,
            node.content,
            node.executable,
            node.nlink,
            node.privilege_bits,
        )
        baseline_semantic = (
            baseline.location,
            baseline.kind,
            baseline.content,
            baseline.executable,
            baseline.nlink,
            baseline.privilege_bits,
        )
        if desired_semantic != baseline_semantic:
            raise SnapshotRejected("unexplained_desired_node", "existing node was changed in place")
    elif type(node.reference) is PreparedNode:
        slot_id = _corrected_require_str(node.reference.slot_id, f"{field_name}.slot_id")
        slot = slots.get(slot_id)
        if slot is None:
            raise SnapshotRejected("missing_preparation_slot", "desired prepared slot is unknown")
        destination_filesystem = namespace_filesystems.get(node.location.namespace)
        if (
            _corrected_parent_filesystem(slot.materialization_parent, slots)
            != destination_filesystem
            or _corrected_parent_filesystem(slot.installation_parent, slots)
            != destination_filesystem
        ):
            raise SnapshotRejected(
                "cross_filesystem_transition", "prepared install crosses filesystems"
            )
    else:
        raise SnapshotRejected("invalid_input_type", f"{field_name} reference type is invalid")
    return node


def _corrected_validate_desired_storage(
    state: object,
    field_name: str,
    baseline_nodes: dict[NodeIdentity, NodeState],
    slots: dict[str, PreparationSlot],
    namespace_filesystems: dict[str, str],
) -> DesiredNodeState | AbsentState:
    if type(state) is AbsentState:
        _corrected_validate_location(state.location, f"{field_name}.location")
        return state
    return _corrected_validate_desired_node(
        state,
        field_name,
        baseline_nodes,
        slots,
        namespace_filesystems,
    )


def _corrected_validate_parent_binding(
    binding: object,
    field_name: str,
    slots: dict[str, PreparationSlot],
    *,
    require_existing: bool = False,
) -> ParentBinding:
    if type(binding) is not ParentBinding:
        raise SnapshotRejected("invalid_input_type", f"{field_name} type is invalid")
    _corrected_validate_location(binding.location, f"{field_name}.location")
    reference = binding.reference
    if type(reference) is ExistingNode:
        _corrected_validate_identity(reference.identity, f"{field_name}.identity")
    elif type(reference) is PreparedNode and not require_existing:
        slot_id = _corrected_require_str(reference.slot_id, f"{field_name}.slot_id")
        if slot_id not in slots:
            raise SnapshotRejected(
                "missing_preparation_slot", f"{field_name} prepared parent is unknown"
            )
    else:
        raise SnapshotRejected("invalid_input_type", f"{field_name} reference type is invalid")
    return binding


def _corrected_parent_filesystem(
    binding: ParentBinding,
    slots: dict[str, PreparationSlot],
) -> str:
    if isinstance(binding.reference, ExistingNode):
        return binding.reference.identity.filesystem_id
    parent_slot = slots[binding.reference.slot_id]
    materialization_reference = parent_slot.materialization_parent.reference
    if type(materialization_reference) is not ExistingNode:
        raise SnapshotRejected(
            "invalid_specification",
            "a preparation materialization parent must already exist",
        )
    return materialization_reference.identity.filesystem_id


def _corrected_expected_parent_location(
    entry: Location,
    namespace_roots: dict[str, Location],
) -> Location:
    head, separator, _tail = entry.raw_path.rpartition(b"/")
    if separator:
        return Location(entry.namespace, head)
    root = namespace_roots.get(entry.namespace)
    if root is None:
        raise SnapshotRejected(
            "incomplete_topology", "entry namespace has no protected root boundary"
        )
    return root


def _corrected_require_immediate_parent(
    parent: ParentBinding,
    entry: Location,
    namespace_roots: dict[str, Location],
    field_name: str,
) -> None:
    expected = _corrected_expected_parent_location(entry, namespace_roots)
    if parent.location != expected:
        raise SnapshotRejected(
            "invalid_specification",
            f"{field_name} must be the immediate entry-owning directory",
        )


def _corrected_parent_binding_key(binding: ParentBinding) -> tuple[str, bytes, str, str, str]:
    reference = binding.reference
    if isinstance(reference, ExistingNode):
        return (
            binding.location.namespace,
            binding.location.raw_path,
            "existing",
            reference.identity.filesystem_id,
            reference.identity.node_id,
        )
    return (
        binding.location.namespace,
        binding.location.raw_path,
        "prepared",
        reference.slot_id,
        "",
    )


def _corrected_initial_parent_bindings(spec: ReconciliationSpec) -> tuple[ParentBinding, ...]:
    bindings: list[ParentBinding] = []
    bindings.extend(marker.parent for marker in spec.ownership_markers)
    bindings.extend(destination.parent for destination in spec.destination_objects)
    for slot in spec.preparation_slots:
        bindings.append(slot.materialization_parent)
        if type(slot.installation_parent.reference) is ExistingNode:
            bindings.append(slot.installation_parent)
    for quarantine_slot in spec.quarantine_slots:
        bindings.extend((quarantine_slot.source_parent, quarantine_slot.destination_parent))
        if quarantine_slot.restoration_parent is not None and isinstance(
            quarantine_slot.restoration_parent.reference, ExistingNode
        ):
            bindings.append(quarantine_slot.restoration_parent)
    by_location: dict[Location, ParentBinding] = {}
    by_identity: dict[NodeIdentity, Location] = {}
    for binding in bindings:
        prior = by_location.get(binding.location)
        if prior is not None and prior != binding:
            raise SnapshotRejected(
                "ambiguous_node_identity", "one parent location has conflicting identities"
            )
        if type(binding.reference) is ExistingNode:
            prior_location = by_identity.get(binding.reference.identity)
            if prior_location is not None and prior_location != binding.location:
                raise SnapshotRejected(
                    "ambiguous_node_identity", "one parent identity has conflicting locations"
                )
            by_identity[binding.reference.identity] = binding.location
        by_location[binding.location] = binding
    return tuple(sorted(by_location.values(), key=_corrected_parent_binding_key))


def _corrected_collect_baseline_nodes(replica: ReplicaState) -> dict[NodeIdentity, NodeState]:
    storage: list[NodeState | AbsentState] = [
        replica.index.storage,
        replica.refs.head,
        replica.refs.loose_ref,
        replica.refs.packed_refs,
        *replica.refs.reflogs,
        *replica.refs.unrelated_metadata,
        *replica.working_nodes,
    ]
    nodes: dict[NodeIdentity, NodeState] = {}
    for state in storage:
        if type(state) is NodeState:
            previous = nodes.get(state.identity)
            if previous is not None and previous != state:
                raise SnapshotRejected(
                    "ambiguous_node_identity", "one node identity has two states"
                )
            nodes[state.identity] = state
    return nodes


def _corrected_validate_sha256(value: object, field_name: str) -> str:
    digest = _corrected_require_str(value, field_name)
    payload = digest.removeprefix("sha256:")
    if not digest.startswith("sha256:") or len(payload) != 64:
        raise SnapshotRejected("invalid_digest", f"{field_name} is not canonical sha256")
    if any(character not in "0123456789abcdef" for character in payload):
        raise SnapshotRejected("invalid_digest", f"{field_name} is not canonical sha256")
    return digest


def _corrected_schema_paths(spec: ReconciliationSpec) -> tuple[bytes, ...]:
    """Collect every explicit or derived schema path for admission accounting."""
    try:
        paths: list[bytes] = []
        for replica in (spec.baseline, spec.desired):
            paths.append(replica.index.storage.location.raw_path)
            paths.extend(entry.raw_path for entry in replica.index.entries)
            paths.extend(entry.raw_path for entry in replica.manifest)
            paths.extend(node.location.raw_path for node in replica.working_nodes)
            refs = replica.refs
            paths.extend(
                (
                    refs.head.location.raw_path,
                    refs.loose_ref.location.raw_path,
                    refs.packed_refs.location.raw_path,
                    refs.selected_ref,
                )
            )
            paths.extend(state.location.raw_path for state in refs.reflogs)
            paths.extend(state.location.raw_path for state in refs.unrelated_metadata)
        for boundary in spec.boundaries:
            paths.append(boundary.location.raw_path)
            paths.extend(ancestor.location.raw_path for ancestor in boundary.ancestors)
        for preparation_slot in spec.preparation_slots:
            paths.extend(
                (
                    preparation_slot.destination.raw_path,
                    preparation_slot.materialization_parent.location.raw_path,
                    preparation_slot.installation_parent.location.raw_path,
                    preparation_slot.initial.location.raw_path,
                )
            )
        for quarantine_slot in spec.quarantine_slots:
            paths.extend(
                (
                    quarantine_slot.source.raw_path,
                    quarantine_slot.destination.raw_path,
                    quarantine_slot.source_parent.location.raw_path,
                    quarantine_slot.destination_parent.location.raw_path,
                    quarantine_slot.initial.location.raw_path,
                )
            )
            if quarantine_slot.restoration_parent is not None:
                paths.append(quarantine_slot.restoration_parent.location.raw_path)
        for marker in spec.ownership_markers:
            paths.extend(
                (
                    marker.destination.raw_path,
                    marker.parent.location.raw_path,
                    marker.initial.location.raw_path,
                )
            )
        for required_object in spec.required_objects:
            paths.append(
                (f"objects/{required_object.object_id[:2]}/{required_object.object_id[2:]}").encode(
                    "ascii"
                )
            )
        for destination_object in spec.destination_objects:
            paths.extend(
                (
                    destination_object.storage.location.raw_path,
                    destination_object.parent.location.raw_path,
                )
            )
    except (AttributeError, TypeError) as exc:
        raise SnapshotRejected(
            "invalid_input_type",
            "specification shape does not permit complete path accounting",
        ) from exc
    if any(type(path) is not bytes for path in paths):
        raise SnapshotRejected("invalid_input_type", "schema paths must be exact bytes")
    return tuple(paths)


def _corrected_enforce_schema_path_limits(spec: ReconciliationSpec) -> None:
    """Apply complete path count and depth limits before topology checks."""
    paths = _corrected_schema_paths(spec)
    if len(paths) > spec.limits.max_paths:
        raise SnapshotRejected("inventory_limit_exceeded", "path or object limit exceeded")
    depth = max((path.count(b"/") + 1 for path in paths), default=0)
    if depth > spec.limits.max_depth:
        raise SnapshotRejected("inventory_limit_exceeded", "path depth limit exceeded")


def _corrected_validate_specification(spec: ReconciliationSpec) -> None:
    if type(spec.limits) is not InventoryLimits:
        raise SnapshotRejected("invalid_input_type", "inventory limits type is invalid")
    for field in fields(InventoryLimits):
        _corrected_require_int(getattr(spec.limits, field.name), f"limits.{field.name}", 1)
    semantic_bytes = 0
    if type(spec.required_objects) is tuple:
        semantic_bytes = sum(
            item.byte_length
            for item in spec.required_objects
            if type(item) is RequiredObject and type(item.byte_length) is int
        )
    if _corrected_decoded_byte_count(spec) + semantic_bytes > spec.limits.max_total_bytes:
        raise SnapshotRejected("inventory_limit_exceeded", "byte limit exceeded")
    _corrected_enforce_schema_path_limits(spec)

    _corrected_require_int(spec.version, "version", 1)
    if spec.version != 1:
        raise SnapshotRejected("unsupported_version", "specification version is unsupported")
    if type(spec.identity) is not OperationIdentity:
        raise SnapshotRejected("invalid_input_type", "operation identity type is invalid")
    identity = spec.identity
    for field_name in (
        "incarnation",
        "binding_id",
        "selected_authority",
        "operation_id",
        "reservation_id",
        "destination_binding",
        "physical_target_id",
        "acknowledgment_receipt_id",
        "drain_receipt_id",
        "export_receipt_id",
        "inventory_receipt_id",
    ):
        _corrected_require_str(getattr(identity, field_name), f"identity.{field_name}")
    _corrected_require_int(identity.generation, "identity.generation")
    _corrected_require_int(identity.replica_generation, "identity.replica_generation")
    object_format = _corrected_require_str(identity.object_format, "identity.object_format")
    if object_format not in {"sha1", "sha256"}:
        raise SnapshotRejected("unsupported_object_format", "object format is unsupported")
    if type(spec.desired) is not DesiredReplicaState:
        raise SnapshotRejected("invalid_input_type", "desired replica type is invalid")

    baseline = _corrected_validate_replica(spec.baseline, identity.object_format, "baseline")
    if baseline.generation != identity.generation:
        raise SnapshotRejected(
            "generation_mismatch", "baseline generation is not operation generation"
        )
    if baseline.replica_generation != identity.replica_generation:
        raise SnapshotRejected("generation_mismatch", "baseline replica generation changed")
    if spec.desired.generation != baseline.generation + 1:
        raise SnapshotRejected(
            "generation_mismatch", "desired generation must advance exactly once"
        )
    if spec.desired.replica_generation != baseline.replica_generation:
        raise SnapshotRejected("generation_mismatch", "desired replica generation changed")

    boundaries = _corrected_require_tuple(spec.boundaries, "boundaries")
    required_roles = {"root", "admin", "registration", "excluded", "private"}
    namespace_filesystems: dict[str, str] = {}
    seen_boundary_ids: set[str] = set()
    seen_roles: set[str] = set()
    validated_boundaries: list[Boundary] = []
    for boundary in boundaries:
        if type(boundary) is not Boundary:
            raise SnapshotRejected("invalid_input_type", "boundary type is invalid")
        validated_boundaries.append(boundary)
        _corrected_require_str(boundary.boundary_id, "boundary.boundary_id")
        if boundary.boundary_id in seen_boundary_ids:
            raise SnapshotRejected("duplicate_boundary", "boundary id is duplicated")
        seen_boundary_ids.add(boundary.boundary_id)
        role = _corrected_require_str(boundary.role, "boundary.role")
        if role not in required_roles or role in seen_roles:
            raise SnapshotRejected(
                "incomplete_topology", "boundary roles are incomplete or duplicated"
            )
        seen_roles.add(role)
        location = _corrected_validate_location(boundary.location, "boundary.location")
        concrete = _corrected_validate_identity(boundary.identity, "boundary.identity")
        expected_namespace = "working" if role == "root" else role
        if location.namespace != expected_namespace:
            raise SnapshotRejected("incomplete_topology", "boundary namespace does not match role")
        namespace_filesystems[expected_namespace] = concrete.filesystem_id
        ancestors = _corrected_require_tuple(boundary.ancestors, "boundary.ancestors")
        ancestor_locations: set[Location] = set()
        for ancestor in ancestors:
            checked = _corrected_validate_node(ancestor, "boundary.ancestor")
            if checked.kind != "directory" or checked.location in ancestor_locations:
                raise SnapshotRejected("incomplete_topology", "boundary ancestor chain is invalid")
            ancestor_locations.add(checked.location)
    if seen_roles != required_roles:
        raise SnapshotRejected("incomplete_topology", "all protected boundary roles are required")
    namespace_roots = {
        boundary.location.namespace: boundary.location for boundary in validated_boundaries
    }

    preparation_slots = _corrected_require_tuple(spec.preparation_slots, "preparation_slots")
    slots: dict[str, PreparationSlot] = {}
    auxiliary_locations: set[Location] = set()
    for slot in preparation_slots:
        if type(slot) is not PreparationSlot:
            raise SnapshotRejected("invalid_input_type", "preparation slot type is invalid")
        slot_id = _corrected_require_str(slot.slot_id, "preparation.slot_id")
        if slot_id in slots:
            raise SnapshotRejected("duplicate_slot", "preparation slot id is duplicated")
        slots[slot_id] = slot
        location = _corrected_validate_location(slot.destination, "preparation.destination")
        if location.namespace != "private":
            raise SnapshotRejected(
                "invalid_specification", "preparation destination is not private"
            )
        if type(slot.initial) is not AbsentState or slot.initial.location != location:
            raise SnapshotRejected(
                "invalid_specification", "preparation destination must be absent"
            )
        desired_kind = _corrected_require_str(slot.desired_kind, "preparation.desired_kind")
        if desired_kind not in {"file", "directory", "symlink"}:
            raise SnapshotRejected("unsupported_kind", "preparation kind is unsupported")
        desired_content = _corrected_require_bytes(
            slot.desired_content, "preparation.desired_content"
        )
        executable = _corrected_require_bool(slot.executable, "preparation.executable")
        _corrected_validate_node_semantics(
            "preparation slot",
            desired_kind,
            desired_content,
            executable,
            1,
            False,
            content_reason="invalid_node_state",
            content_detail="preparation slot directory carries content",
        )
        if location in auxiliary_locations:
            raise SnapshotRejected("duplicate_location", "auxiliary location is duplicated")
        auxiliary_locations.add(location)

    for slot in slots.values():
        materialization_parent = _corrected_validate_parent_binding(
            slot.materialization_parent,
            "preparation.materialization_parent",
            slots,
            require_existing=True,
        )
        installation_parent = _corrected_validate_parent_binding(
            slot.installation_parent,
            "preparation.installation_parent",
            slots,
        )
        if slot.destination == materialization_parent.location:
            raise SnapshotRejected("invalid_specification", "preparation cannot contain itself")
        materialization_filesystem = _corrected_parent_filesystem(materialization_parent, slots)
        if materialization_filesystem != _corrected_parent_filesystem(installation_parent, slots):
            raise SnapshotRejected(
                "cross_filesystem_transition", "preparation install crosses filesystems"
            )
        _corrected_require_immediate_parent(
            materialization_parent,
            slot.destination,
            namespace_roots,
            "preparation.materialization_parent",
        )

    marker_tuple = _corrected_require_tuple(spec.ownership_markers, "ownership_markers")
    marker_ids: set[str] = set()
    for marker in marker_tuple:
        if type(marker) is not OwnershipMarkerSpec:
            raise SnapshotRejected("invalid_input_type", "ownership marker type is invalid")
        marker_id = _corrected_require_str(marker.slot_id, "ownership_marker.slot_id")
        if marker_id in slots or marker_id in marker_ids:
            raise SnapshotRejected("duplicate_slot", "ownership marker slot id is duplicated")
        marker_ids.add(marker_id)
        location = _corrected_validate_location(marker.destination, "ownership_marker.destination")
        if (
            location.namespace != "private"
            or type(marker.initial) is not AbsentState
            or marker.initial.location != location
        ):
            raise SnapshotRejected(
                "invalid_specification", "ownership marker destination must be absent"
            )
        parent = _corrected_validate_parent_binding(
            marker.parent,
            "ownership_marker.parent",
            slots,
            require_existing=True,
        )
        if (
            marker.destination == parent.location
            or _corrected_parent_filesystem(parent, slots) != namespace_filesystems["private"]
        ):
            raise SnapshotRejected(
                "cross_filesystem_transition", "ownership marker crosses filesystems"
            )
        _corrected_require_immediate_parent(
            parent,
            marker.destination,
            namespace_roots,
            "ownership_marker.parent",
        )
        _corrected_require_bytes(marker.payload_prefix, "ownership_marker.payload_prefix")
        _corrected_require_bool(marker.executable, "ownership_marker.executable")
        if location in auxiliary_locations:
            raise SnapshotRejected("duplicate_location", "auxiliary location is duplicated")
        auxiliary_locations.add(location)

    quarantine_tuple = _corrected_require_tuple(spec.quarantine_slots, "quarantine_slots")
    quarantine_ids: set[str] = set()
    for slot in quarantine_tuple:
        if type(slot) is not QuarantineSlot:
            raise SnapshotRejected("invalid_input_type", "quarantine slot type is invalid")
        slot_id = _corrected_require_str(slot.slot_id, "quarantine.slot_id")
        if slot_id in quarantine_ids or slot_id in slots or slot_id in marker_ids:
            raise SnapshotRejected("duplicate_slot", "quarantine slot id is duplicated")
        quarantine_ids.add(slot_id)
        _corrected_validate_location(slot.source, "quarantine.source")
        source_identity = _corrected_validate_identity(
            slot.source_identity, "quarantine.source_identity"
        )
        destination = _corrected_validate_location(slot.destination, "quarantine.destination")
        source_parent = _corrected_validate_parent_binding(
            slot.source_parent,
            "quarantine.source_parent",
            slots,
            require_existing=True,
        )
        destination_parent = _corrected_validate_parent_binding(
            slot.destination_parent,
            "quarantine.destination_parent",
            slots,
            require_existing=True,
        )
        restoration_parent = slot.restoration_parent
        if restoration_parent is not None:
            restoration_parent = _corrected_validate_parent_binding(
                restoration_parent,
                "quarantine.restoration_parent",
                slots,
            )
        parent_filesystems = {
            _corrected_parent_filesystem(source_parent, slots),
            _corrected_parent_filesystem(destination_parent, slots),
        }
        if restoration_parent is not None:
            parent_filesystems.add(_corrected_parent_filesystem(restoration_parent, slots))
        if source_identity.filesystem_id not in parent_filesystems or len(parent_filesystems) != 1:
            raise SnapshotRejected("cross_filesystem_transition", "quarantine crosses filesystems")
        if slot.source == source_parent.location or destination == destination_parent.location:
            raise SnapshotRejected("invalid_specification", "quarantine parent equals moved entry")
        if destination == slot.source:
            raise SnapshotRejected(
                "invalid_specification", "quarantine destination equals its source"
            )
        _corrected_require_immediate_parent(
            source_parent,
            slot.source,
            namespace_roots,
            "quarantine.source_parent",
        )
        _corrected_require_immediate_parent(
            destination_parent,
            destination,
            namespace_roots,
            "quarantine.destination_parent",
        )
        if restoration_parent is not None:
            _corrected_require_immediate_parent(
                restoration_parent,
                slot.source,
                namespace_roots,
                "quarantine.restoration_parent",
            )
        if type(slot.initial) is not AbsentState or slot.initial.location != destination:
            raise SnapshotRejected("invalid_specification", "quarantine destination must be absent")
        if destination in auxiliary_locations:
            raise SnapshotRejected("duplicate_location", "auxiliary location is duplicated")
        auxiliary_locations.add(destination)

    baseline_nodes = _corrected_collect_baseline_nodes(baseline)
    physical_identities: dict[NodeIdentity, tuple[Location, NodeState | None]] = {}
    physical_locations: dict[Location, tuple[NodeIdentity, NodeState | None]] = {}

    def register_physical_identity(
        location: Location,
        identity: NodeIdentity,
        state: NodeState | None = None,
    ) -> None:
        prior_identity_binding = physical_identities.get(identity)
        prior_location_binding = physical_locations.get(location)
        identity_conflict = (
            prior_identity_binding is not None and prior_identity_binding[0] != location
        )
        location_conflict = (
            prior_location_binding is not None and prior_location_binding[0] != identity
        )
        state_conflict = any(
            prior is not None and state is not None and prior != state
            for prior in (
                None if prior_identity_binding is None else prior_identity_binding[1],
                None if prior_location_binding is None else prior_location_binding[1],
            )
        )
        if identity_conflict or location_conflict or state_conflict:
            raise SnapshotRejected(
                "ambiguous_node_identity",
                "physical identity, location, and concrete state must be unique",
            )
        retained_state = state
        if retained_state is None and prior_identity_binding is not None:
            retained_state = prior_identity_binding[1]
        physical_identities[identity] = (location, retained_state)
        physical_locations[location] = (identity, retained_state)

    for boundary in spec.boundaries:
        register_physical_identity(boundary.location, boundary.identity)
        for ancestor in boundary.ancestors:
            register_physical_identity(ancestor.location, ancestor.identity, ancestor)
    for node in baseline_nodes.values():
        register_physical_identity(node.location, node.identity, node)
        expected_filesystem = namespace_filesystems.get(node.location.namespace)
        if expected_filesystem != node.identity.filesystem_id:
            raise SnapshotRejected(
                "filesystem_identity_mismatch", "node filesystem is outside boundary"
            )
    desired = _corrected_validate_desired(
        spec.desired,
        identity.object_format,
        baseline_nodes,
        slots,
        namespace_filesystems,
    )
    for prefix, replica in (("baseline", baseline), ("desired", desired)):
        _corrected_require_administrative_file(replica.index.storage, f"{prefix}.index.storage")
        _corrected_require_administrative_file(replica.refs.head, f"{prefix}.refs.head")
        _corrected_require_administrative_file(replica.refs.loose_ref, f"{prefix}.refs.loose_ref")
        _corrected_require_administrative_file(
            replica.refs.packed_refs, f"{prefix}.refs.packed_refs"
        )
        for field_name in ("reflogs", "unrelated_metadata"):
            for state in getattr(replica.refs, field_name):
                _corrected_require_administrative_file(state, f"{prefix}.refs.{field_name}")
    unchanged_index_storage = desired.index.storage == (
        _corrected_desired_storage_from_concrete(baseline.index.storage)
    )
    if unchanged_index_storage and (
        _corrected_canonical_index_entries(desired.index.entries)
        != _corrected_canonical_index_entries(baseline.index.entries)
        or desired.index.sparse_index != baseline.index.sparse_index
    ):
        raise SnapshotRejected(
            "invalid_index_semantics",
            "index semantics cannot change without a physical index transition",
        )

    required_tuple = _corrected_require_tuple(spec.required_objects, "required_objects")
    required: dict[str, RequiredObject] = {}
    for item in required_tuple:
        if type(item) is not RequiredObject:
            raise SnapshotRejected("invalid_input_type", "required object type is invalid")
        object_id = _corrected_validate_oid(
            item.object_id, identity.object_format, "object.object_id"
        )
        if object_id in required:
            raise SnapshotRejected("duplicate_object", "required object id is duplicated")
        required[object_id] = item
        object_kind = _corrected_require_str(item.kind, "object.kind")
        if object_kind not in {"blob", "tree", "commit", "tag"}:
            raise SnapshotRejected("unsupported_object_kind", "object kind is unsupported")
        _corrected_require_int(item.byte_length, "object.byte_length")
        _corrected_validate_sha256(item.content_sha256, "object.content_sha256")
        source_slot_id = _corrected_require_str(item.source_slot_id, "object.source_slot_id")
        if source_slot_id not in slots:
            raise SnapshotRejected("missing_preparation_slot", "object source slot is unknown")
        object_slot = slots[source_slot_id]
        if (
            _corrected_parent_filesystem(object_slot.materialization_parent, slots)
            != namespace_filesystems["admin"]
            or _corrected_parent_filesystem(object_slot.installation_parent, slots)
            != namespace_filesystems["admin"]
        ):
            raise SnapshotRejected(
                "cross_filesystem_transition",
                "prepared object promotion crosses filesystems",
            )
    covered_oids = {entry.object_id for entry in spec.desired.index.entries}
    if spec.desired.refs.resolved_oid is not None:
        covered_oids.add(spec.desired.refs.resolved_oid)
    if spec.desired.refs.packed_fallback_oid is not None:
        covered_oids.add(spec.desired.refs.packed_fallback_oid)
    if not covered_oids.issubset(required):
        raise SnapshotRejected(
            "incomplete_object_inventory", "desired roots are not fully inventoried"
        )

    destination_tuple = _corrected_require_tuple(spec.destination_objects, "destination_objects")
    destination_ids: set[str] = set()
    for item in destination_tuple:
        if type(item) is not DestinationObject:
            raise SnapshotRejected("invalid_input_type", "destination object type is invalid")
        object_id = _corrected_validate_oid(
            item.object_id, identity.object_format, "destination.object_id"
        )
        if object_id not in required or object_id in destination_ids:
            raise SnapshotRejected(
                "object_identity_conflict", "destination object identity is invalid"
            )
        destination_ids.add(object_id)
        storage = _corrected_validate_node(item.storage, "destination.storage")
        _corrected_require_administrative_file(storage, "destination.storage")
        parent = _corrected_validate_parent_binding(
            item.parent,
            "destination.parent",
            slots,
            require_existing=True,
        )
        if (
            storage.location == parent.location
            or _corrected_parent_filesystem(parent, slots) != namespace_filesystems["admin"]
        ):
            raise SnapshotRejected(
                "cross_filesystem_transition", "destination object parent is invalid"
            )
        register_physical_identity(storage.location, storage.identity, storage)
        if (
            storage.location != _corrected_object_location(object_id)
            or storage.identity.filesystem_id != namespace_filesystems["admin"]
        ):
            raise SnapshotRejected(
                "object_identity_conflict",
                "destination object has the wrong location or filesystem",
            )
        _corrected_require_immediate_parent(
            parent,
            storage.location,
            namespace_roots,
            "destination.parent",
        )
        content_digest = "sha256:" + hashlib.sha256(storage.content).hexdigest()
        if (
            _corrected_validate_sha256(item.content_sha256, "destination.content_sha256")
            != content_digest
        ):
            raise SnapshotRejected("object_identity_conflict", "destination physical bytes changed")
        _corrected_require_str(item.content_sync_receipt_id, "destination.content_sync_receipt_id")
        _corrected_require_str(item.parent_sync_receipt_id, "destination.parent_sync_receipt_id")

    static_storage: list[NodeState | DesiredNodeState | AbsentState] = []
    for replica in (baseline, desired):
        static_storage.extend(
            (
                replica.index.storage,
                replica.refs.head,
                replica.refs.loose_ref,
                replica.refs.packed_refs,
                *replica.refs.reflogs,
                *replica.refs.unrelated_metadata,
                *replica.working_nodes,
            )
        )
    static_storage_locations = {state.location for state in static_storage}
    static_absent_locations = {
        state.location for state in static_storage if type(state) is AbsentState
    }
    baseline_registry = _corrected_static_location_registry(baseline, "baseline")
    desired_registry = _corrected_static_location_registry(desired, "desired")
    quarantine_transitions = {(slot.source, slot.source_identity) for slot in spec.quarantine_slots}
    for location_key, (baseline_field, baseline_state) in baseline_registry.items():
        desired_registration = desired_registry.get(location_key)
        if desired_registration is None:
            continue
        desired_field, _desired_state = desired_registration
        if desired_field == baseline_field:
            continue
        if (
            type(baseline_state) is not NodeState
            or (
                baseline_state.location,
                baseline_state.identity,
            )
            not in quarantine_transitions
        ):
            raise SnapshotRejected(
                "duplicate_location",
                "one location changes fields without an explicit quarantine",
            )

    for parent in _corrected_initial_parent_bindings(spec):
        if type(parent.reference) is not ExistingNode:
            raise SnapshotRejected("invalid_specification", "an initial parent must already exist")
        if parent.location in static_absent_locations:
            raise SnapshotRejected("duplicate_location", "an initial parent is recorded as absent")
        register_physical_identity(parent.location, parent.reference.identity)
        known_parent_state = physical_locations[parent.location][1]
        if known_parent_state is not None and known_parent_state.kind != "directory":
            raise SnapshotRejected(
                "invalid_parent_state",
                "an initial parent must resolve to a known directory",
            )

    required_absent_locations: list[tuple[Location, str]] = []
    required_absent_locations.extend(
        (slot.destination, "preparation destination") for slot in spec.preparation_slots
    )
    required_absent_locations.extend(
        (marker.destination, "ownership marker destination") for marker in spec.ownership_markers
    )
    required_absent_locations.extend(
        (slot.destination, "quarantine destination") for slot in spec.quarantine_slots
    )
    present_destination_object_ids = {
        destination.object_id for destination in spec.destination_objects
    }
    required_absent_locations.extend(
        (_corrected_object_location(item.object_id), "object destination")
        for item in spec.required_objects
        if item.object_id not in present_destination_object_ids
    )
    no_replace_roles: dict[Location, str] = {}
    for required_absent_location, absent_role in required_absent_locations:
        if required_absent_location in physical_locations:
            raise SnapshotRejected(
                "duplicate_location",
                f"{absent_role} must stay absent but is present in the specification",
            )
        if required_absent_location in static_storage_locations:
            raise SnapshotRejected(
                "duplicate_location",
                f"{absent_role} conflicts with replica storage",
            )
        prior_role = no_replace_roles.get(required_absent_location)
        if prior_role is not None:
            raise SnapshotRejected(
                "duplicate_location",
                f"{absent_role} duplicates the independent {prior_role}",
            )
        no_replace_roles[required_absent_location] = absent_role

    object_locations = {destination.storage.location for destination in spec.destination_objects}
    if object_locations & static_storage_locations:
        raise SnapshotRejected(
            "duplicate_location", "durable object storage conflicts with replica storage"
        )

    desired_nodes: list[DesiredNodeState] = [
        spec.desired.refs.head,
        *spec.desired.working_nodes,
    ]
    if type(spec.desired.index.storage) is DesiredNodeState:
        desired_nodes.append(spec.desired.index.storage)
    for state in (spec.desired.refs.loose_ref, spec.desired.refs.packed_refs):
        if type(state) is DesiredNodeState:
            desired_nodes.append(state)
    desired_nodes.extend(
        state
        for state in spec.desired.refs.reflogs + spec.desired.refs.unrelated_metadata
        if type(state) is DesiredNodeState
    )
    slot_consumers: dict[str, str] = {}
    for desired_node_candidate in desired_nodes:
        reference = desired_node_candidate.reference
        if type(reference) is not PreparedNode:
            continue
        prepared_slot = slots[reference.slot_id]
        if reference.slot_id in slot_consumers:
            raise SnapshotRejected(
                "ambiguous_preparation_slot", "preparation slot has two consumers"
            )
        slot_consumers[reference.slot_id] = "desired-node"
        prepared_semantic = (
            prepared_slot.desired_kind,
            prepared_slot.desired_content,
            prepared_slot.executable,
        )
        desired_semantic = (
            desired_node_candidate.kind,
            desired_node_candidate.content,
            desired_node_candidate.executable,
        )
        unsafe_link_count = (
            desired_node_candidate.kind != "directory" and desired_node_candidate.nlink != 1
        )
        if (
            prepared_semantic != desired_semantic
            or unsafe_link_count
            or desired_node_candidate.privilege_bits
        ):
            raise SnapshotRejected(
                "preparation_content_mismatch",
                "prepared bytes do not match desired node semantics",
            )
    for item in required.values():
        if item.source_slot_id in slot_consumers:
            raise SnapshotRejected(
                "ambiguous_preparation_slot", "preparation slot has two consumers"
            )
        source_slot = slots[item.source_slot_id]
        if source_slot.desired_kind != "file" or source_slot.executable:
            raise SnapshotRejected(
                "preparation_content_mismatch",
                "prepared object storage must be a non-executable file",
            )
        slot_consumers[item.source_slot_id] = "required-object"
    if set(slots) != set(slot_consumers):
        raise SnapshotRejected(
            "unexplained_preparation_slot",
            "every preparation slot must have exactly one consumer",
        )
    install_targets: dict[str, Location] = {}
    for desired_node_candidate in desired_nodes:
        reference = desired_node_candidate.reference
        if type(reference) is PreparedNode:
            if reference.slot_id in install_targets:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one preparation slot has two final installation targets",
                )
            install_targets[reference.slot_id] = desired_node_candidate.location
    for item in required.values():
        if item.object_id not in destination_ids:
            if item.source_slot_id in install_targets:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one preparation slot has two final installation targets",
                )
            install_targets[item.source_slot_id] = _corrected_object_location(item.object_id)
    for slot_id, entry in install_targets.items():
        _corrected_require_immediate_parent(
            slots[slot_id].installation_parent,
            entry,
            namespace_roots,
            "preparation.installation_parent",
        )
    initial_parent_bindings = _corrected_initial_parent_bindings(spec)
    protected_target_locations = {boundary.location for boundary in validated_boundaries}
    protected_target_locations.update(
        ancestor.location for boundary in validated_boundaries for ancestor in boundary.ancestors
    )
    for entry in install_targets.values():
        if entry in protected_target_locations:
            continue
        for binding in initial_parent_bindings:
            if binding.location != entry:
                continue
            reference = binding.reference
            if (
                type(reference) is ExistingNode
                and (
                    binding.location,
                    reference.identity,
                )
                in quarantine_transitions
            ):
                continue
            raise SnapshotRejected(
                "duplicate_location",
                "a prepared final target overlaps a still-bound existing parent",
            )

    def storage_changed(
        baseline_state: NodeState | AbsentState,
        desired_state: DesiredNodeState | AbsentState,
    ) -> bool:
        return desired_state != _corrected_desired_storage_from_concrete(baseline_state)

    expected_quarantine: set[tuple[Location, NodeIdentity]] = set()
    desired_working_by_path = {node.location.raw_path: node for node in spec.desired.working_nodes}
    directly_changed_paths = {
        node.location.raw_path
        for node in spec.baseline.working_nodes
        if desired_working_by_path.get(node.location.raw_path)
        != _corrected_desired_node_from_concrete(node)
    }
    replaced_directory_paths = {
        node.location.raw_path
        for node in spec.baseline.working_nodes
        if node.kind == "directory" and node.location.raw_path in directly_changed_paths
    }
    for node in spec.baseline.working_nodes:
        inside_replaced_directory = any(
            _is_directory_prefix(directory, node.location.raw_path)
            for directory in replaced_directory_paths
        )
        if node.location.raw_path in directly_changed_paths or inside_replaced_directory:
            expected_quarantine.add((node.location, node.identity))
    if (
        storage_changed(spec.baseline.index.storage, spec.desired.index.storage)
        and type(spec.baseline.index.storage) is NodeState
    ):
        expected_quarantine.add(
            (spec.baseline.index.storage.location, spec.baseline.index.storage.identity)
        )
    baseline_reflogs = {
        _corrected_storage_key(state): state for state in spec.baseline.refs.reflogs
    }
    desired_reflogs = {
        _corrected_expected_storage_key(state): state for state in spec.desired.refs.reflogs
    }
    for location_key in set(desired_reflogs) - set(baseline_reflogs):
        if type(desired_reflogs[location_key]) is AbsentState:
            raise SnapshotRejected(
                "invalid_ref_semantics",
                "a new reflog absence has no physical transition",
            )
    for location_key, baseline_state in baseline_reflogs.items():
        desired_state = desired_reflogs.get(location_key)
        if desired_state is None:
            raise SnapshotRejected(
                "invalid_ref_semantics",
                "every baseline reflog requires an explicit desired state",
            )
        if type(baseline_state) is NodeState and storage_changed(baseline_state, desired_state):
            expected_quarantine.add((baseline_state.location, baseline_state.identity))
    if type(spec.baseline.refs.loose_ref) is NodeState and storage_changed(
        spec.baseline.refs.loose_ref,
        spec.desired.refs.loose_ref,
    ):
        expected_quarantine.add(
            (spec.baseline.refs.loose_ref.location, spec.baseline.refs.loose_ref.identity)
        )
    baseline_unrelated = {
        _corrected_storage_key(state): _corrected_desired_storage_from_concrete(state)
        for state in spec.baseline.refs.unrelated_metadata
    }
    desired_unrelated = {
        _corrected_expected_storage_key(state): state
        for state in spec.desired.refs.unrelated_metadata
    }
    immutable_admin = (
        spec.desired.refs.head == _corrected_desired_node_from_concrete(spec.baseline.refs.head)
        and spec.desired.refs.packed_refs
        == _corrected_desired_storage_from_concrete(spec.baseline.refs.packed_refs)
        and desired_unrelated == baseline_unrelated
    )
    if not immutable_admin:
        raise SnapshotRejected(
            "unsupported_administrative_transition",
            "HEAD, packed refs, and unrelated metadata must remain exact",
        )
    if spec.desired.refs.packed_fallback_oid != spec.baseline.refs.packed_fallback_oid:
        raise SnapshotRejected(
            "invalid_ref_semantics",
            "packed fallback cannot change without a physical packed-refs transition",
        )
    actual_quarantine = [(slot.source, slot.source_identity) for slot in spec.quarantine_slots]
    if len(actual_quarantine) != len(set(actual_quarantine)):
        raise SnapshotRejected(
            "unexplained_quarantine_slot",
            "one source has multiple quarantine destinations",
        )
    if expected_quarantine - set(actual_quarantine):
        raise SnapshotRejected(
            "missing_quarantine_slot",
            "every replaced concrete node requires an exact quarantine slot",
        )
    if set(actual_quarantine) - expected_quarantine:
        raise SnapshotRejected(
            "unexplained_quarantine_slot",
            "quarantine slot does not correspond to a replaced node",
        )

    protected_locations = {boundary.location for boundary in spec.boundaries}
    protected_locations.update(
        ancestor.location for boundary in spec.boundaries for ancestor in boundary.ancestors
    )
    protected_identities = {boundary.identity for boundary in spec.boundaries}
    protected_identities.update(
        ancestor.identity for boundary in spec.boundaries for ancestor in boundary.ancestors
    )
    mutation_locations = (
        {slot.source for slot in spec.quarantine_slots}
        | auxiliary_locations
        | {destination.storage.location for destination in spec.destination_objects}
        | {node.location for node in desired_nodes if type(node.reference) is PreparedNode}
    )
    mutation_identities = {slot.source_identity for slot in spec.quarantine_slots}
    if protected_locations & mutation_locations or protected_identities & mutation_identities:
        raise SnapshotRejected(
            "protected_topology_conflict",
            "reconciliation cannot mutate a protected boundary node",
        )
    for boundary in spec.boundaries:
        chain_locations = tuple(ancestor.location for ancestor in boundary.ancestors) + (
            boundary.location,
        )
        for outer, inner in pairwise(chain_locations):
            if outer.namespace != inner.namespace or not _is_directory_prefix(
                outer.raw_path, inner.raw_path
            ):
                raise SnapshotRejected(
                    "incomplete_topology",
                    "protected boundary ancestors must form one connected ordered parent chain",
                )

    if len(required) > spec.limits.max_objects:
        raise SnapshotRejected("inventory_limit_exceeded", "path or object limit exceeded")
    transitions = _corrected_compile_transitions(spec)
    _corrected_validate_transition_graph(
        spec,
        transitions,
        install_targets,
        baseline_registry,
    )


_INSTALL_EFFECT_KINDS = (
    "promote_required_object",
    "install_working_node",
    "install_index",
    "install_reflog",
    "install_selected_ref",
)

_QUARANTINE_EFFECT_KINDS = (
    "quarantine_working_node",
    "quarantine_index",
    "quarantine_reflog",
    "quarantine_selected_ref",
)


def _corrected_validate_transition_graph(
    spec: ReconciliationSpec,
    transitions: tuple[_TransitionStep, ...],
    install_targets: dict[str, Location],
    baseline_registry: dict[
        tuple[str, bytes],
        tuple[str, NodeState | DesiredNodeState | AbsentState],
    ],
) -> None:
    """Reject prepared-parent and location-order contradictions before planning."""
    install_index: dict[str, int] = {}
    restore_index: dict[str, int] = {}
    quarantine_index_by_source: dict[tuple[str, bytes], int] = {}
    install_index_by_target: dict[tuple[str, bytes], int] = {}
    for index, transition in enumerate(transitions):
        effect_kind = transition.effect_kind
        slot_id = transition.prepared_slot_id
        if effect_kind in _INSTALL_EFFECT_KINDS:
            if slot_id is None or slot_id in install_index:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one preparation slot has multiple installation actions",
                )
            install_index[slot_id] = index
            target = install_targets.get(slot_id)
            if target is None:
                raise SnapshotRejected(
                    "invalid_specification",
                    "an installation action has no final target",
                )
            target_key = (target.namespace, target.raw_path)
            if target_key in install_index_by_target:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one location receives two prepared installations",
                )
            install_index_by_target[target_key] = index
        elif effect_kind == "restore_working_node":
            if slot_id is None or slot_id in restore_index:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one quarantine slot has multiple restoration actions",
                )
            restore_index[slot_id] = index
        elif effect_kind in _QUARANTINE_EFFECT_KINDS:
            if slot_id is None:
                raise SnapshotRejected("invalid_plan_integrity", "quarantine slot is absent")
            quarantine = next(item for item in spec.quarantine_slots if item.slot_id == slot_id)
            source_key = (quarantine.source.namespace, quarantine.source.raw_path)
            if source_key in quarantine_index_by_source:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "one source location has multiple quarantine actions",
                )
            quarantine_index_by_source[source_key] = index

    slots = {slot.slot_id: slot for slot in spec.preparation_slots}
    edges: list[tuple[ParentBinding, str, PreparationSlot | QuarantineSlot]] = []
    for slot in spec.preparation_slots:
        reference = slot.installation_parent.reference
        if type(reference) is PreparedNode:
            edges.append((slot.installation_parent, reference.slot_id, slot))
    for quarantine in spec.quarantine_slots:
        if quarantine.restoration_parent is not None:
            reference = quarantine.restoration_parent.reference
            if type(reference) is PreparedNode:
                edges.append((quarantine.restoration_parent, reference.slot_id, quarantine))

    dependencies: dict[str, set[str]] = {}
    for binding, parent_slot_id, dependent in edges:
        parent_target = install_targets.get(parent_slot_id)
        if parent_target is None or parent_slot_id not in slots:
            raise SnapshotRejected(
                "invalid_specification",
                "a prepared parent resolves to no consumed installation target",
            )
        if type(dependent) is PreparationSlot and parent_slot_id == dependent.slot_id:
            raise SnapshotRejected(
                "cyclic_prepared_parent",
                "a preparation slot references itself as its installation parent",
            )
        if slots[parent_slot_id].desired_kind != "directory":
            raise SnapshotRejected(
                "invalid_parent_state",
                "a prepared installation parent must be a directory slot",
            )
        if parent_target != binding.location:
            raise SnapshotRejected(
                "invalid_specification",
                "a prepared parent final target differs from the parent binding location",
            )
        if type(dependent) is PreparationSlot:
            dependencies.setdefault(dependent.slot_id, set()).add(parent_slot_id)

    resolving: set[str] = set()
    resolved: set[str] = set()

    def visit(slot_id: str) -> None:
        if slot_id in resolved:
            return
        if slot_id in resolving:
            raise SnapshotRejected(
                "cyclic_prepared_parent",
                "prepared installation parents form a dependency cycle",
            )
        resolving.add(slot_id)
        for parent_id in sorted(dependencies.get(slot_id, ())):
            visit(parent_id)
        resolving.discard(slot_id)
        resolved.add(slot_id)

    for slot_id in sorted(dependencies):
        visit(slot_id)

    for _binding, parent_slot_id, dependent in edges:
        parent_install = install_index[parent_slot_id]
        if type(dependent) is PreparationSlot:
            dependent_install = install_index.get(dependent.slot_id)
            if dependent_install is None:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "a prepared dependent has no installation action",
                )
            if parent_install >= dependent_install:
                raise SnapshotRejected(
                    "impossible_replay_order",
                    "a prepared parent installs after its dependent transition",
                )
        else:
            restoration = restore_index.get(dependent.slot_id)
            if restoration is None:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "a prepared restoration parent has no restoration action",
                )
            if parent_install >= restoration:
                raise SnapshotRejected(
                    "impossible_replay_order",
                    "a prepared parent installs after its dependent restoration",
                )

    for target_key, install_at in install_index_by_target.items():
        baseline_registration = baseline_registry.get(target_key)
        if baseline_registration is None:
            continue
        _field_name, baseline_state = baseline_registration
        if type(baseline_state) is not NodeState:
            continue
        if target_key not in quarantine_index_by_source:
            raise SnapshotRejected(
                "invalid_plan_integrity",
                "an installation replaces a baseline node without quarantine",
            )
        if quarantine_index_by_source[target_key] >= install_at:
            raise SnapshotRejected(
                "invalid_plan_integrity",
                "an installation occurs before quarantine of its target",
            )


def _corrected_validate_expected_image(image: ExpectedObservation) -> None:
    """Apply fresh-observation location checks to one derived expected image."""
    if type(image) is not ExpectedObservation:
        raise SnapshotRejected("invalid_input_type", "expected image type is invalid")
    replica_type = type(image.replica_state)
    if replica_type is not ReplicaState and replica_type is not DesiredReplicaState:
        raise SnapshotRejected("invalid_input_type", "expected image replica type is invalid")
    registry = _corrected_static_location_registry(image.replica_state, "expected")
    identity_locations: dict[NodeIdentity, Location] = {}
    location_identities: dict[Location, NodeIdentity] = {}
    present_keys: set[tuple[str, bytes]] = set()
    storage_keys: set[tuple[str, bytes]] = set()
    absent_keys: set[tuple[str, bytes]] = set()

    def register(location: Location, identity: NodeIdentity) -> None:
        prior_location = identity_locations.get(identity)
        prior_identity = location_identities.get(location)
        identity_conflict = prior_location is not None and prior_location != location
        location_conflict = prior_identity is not None and prior_identity != identity
        if identity_conflict or location_conflict:
            raise SnapshotRejected(
                "ambiguous_node_identity",
                "one expected image maps an identity or location to two nodes",
            )
        identity_locations[identity] = location
        location_identities[location] = identity

    for boundary in image.boundaries:
        register(boundary.location, boundary.identity)
        for ancestor in boundary.ancestors:
            register(ancestor.location, ancestor.identity)
    for _field_name, state in registry.values():
        if type(state) is NodeState:
            register(state.location, state.identity)
            present_keys.add((state.location.namespace, state.location.raw_path))
        elif type(state) is DesiredNodeState:
            if type(state.reference) is ExistingNode:
                register(state.location, state.reference.identity)
            present_keys.add((state.location.namespace, state.location.raw_path))
    for parent in image.parents:
        if type(parent.reference) is ExistingNode:
            register(parent.location, parent.reference.identity)
    for state in image.storage:
        key = (state.location.namespace, state.location.raw_path)
        if key in storage_keys:
            raise SnapshotRejected(
                "duplicate_location", "an expected image records one storage location twice"
            )
        storage_keys.add(key)
        if type(state) is AbsentState:
            if key in present_keys:
                raise SnapshotRejected(
                    "duplicate_location",
                    "an expected image records absence at a present location",
                )
            absent_keys.add(key)
            continue
        if key in present_keys or key in absent_keys:
            raise SnapshotRejected(
                "duplicate_location",
                "an expected image records one location in conflicting states",
            )
        if type(state) is DesiredNodeState:
            if type(state.reference) is ExistingNode:
                register(state.location, state.reference.identity)
        elif type(state) is NodeState:
            register(state.location, state.identity)
        else:
            raise SnapshotRejected("invalid_input_type", "expected image storage type is invalid")
        present_keys.add(key)


def _corrected_canonicalize_spec(spec: ReconciliationSpec) -> ReconciliationSpec:
    def sort_storage(
        values: tuple[NodeState | AbsentState, ...],
    ) -> tuple[NodeState | AbsentState, ...]:
        return tuple(sorted(values, key=_corrected_storage_key))

    baseline_refs = replace(
        spec.baseline.refs,
        reflogs=sort_storage(spec.baseline.refs.reflogs),
        unrelated_metadata=sort_storage(spec.baseline.refs.unrelated_metadata),
    )
    baseline = replace(
        spec.baseline,
        index=replace(
            spec.baseline.index,
            entries=_corrected_canonical_index_entries(spec.baseline.index.entries),
        ),
        refs=baseline_refs,
        manifest=tuple(sorted(spec.baseline.manifest, key=lambda entry: entry.raw_path)),
        working_nodes=tuple(
            sorted(spec.baseline.working_nodes, key=lambda node: _corrected_storage_key(node))
        ),
    )
    desired_refs = replace(
        spec.desired.refs,
        reflogs=tuple(sorted(spec.desired.refs.reflogs, key=_corrected_expected_storage_key)),
        unrelated_metadata=tuple(
            sorted(
                spec.desired.refs.unrelated_metadata,
                key=_corrected_expected_storage_key,
            )
        ),
    )
    desired = replace(
        spec.desired,
        index=replace(
            spec.desired.index,
            entries=_corrected_canonical_index_entries(spec.desired.index.entries),
        ),
        refs=desired_refs,
        manifest=tuple(sorted(spec.desired.manifest, key=lambda entry: entry.raw_path)),
        working_nodes=tuple(
            sorted(
                spec.desired.working_nodes,
                key=lambda node: _corrected_expected_storage_key(node),
            )
        ),
    )
    return replace(
        spec,
        baseline=baseline,
        desired=desired,
        boundaries=tuple(sorted(spec.boundaries, key=lambda boundary: boundary.boundary_id)),
        preparation_slots=tuple(sorted(spec.preparation_slots, key=lambda slot: slot.slot_id)),
        quarantine_slots=tuple(sorted(spec.quarantine_slots, key=lambda slot: slot.slot_id)),
        ownership_markers=tuple(sorted(spec.ownership_markers, key=lambda marker: marker.slot_id)),
        required_objects=tuple(sorted(spec.required_objects, key=lambda item: item.object_id)),
        destination_objects=tuple(
            sorted(spec.destination_objects, key=lambda item: item.object_id)
        ),
    )


def bind_specification(spec: ReconciliationSpec) -> BoundSpecification:
    """Validate, canonicalize, and bind one complete immutable specification."""
    if type(spec) is not ReconciliationSpec:
        raise SnapshotRejected("invalid_input_type", "specification type is invalid")
    _corrected_validate_specification(spec)
    canonical = _corrected_canonicalize_spec(spec)
    total_bytes = len(_canonical_bytes(canonical)) + sum(
        item.byte_length for item in canonical.required_objects
    )
    if total_bytes > canonical.limits.max_total_bytes:
        raise SnapshotRejected("inventory_limit_exceeded", "byte limit exceeded")
    return BoundSpecification(
        spec=canonical,
        fingerprint=_protocol_fingerprint(b"yinshi.workspace-replica.spec\x00v1\x00", canonical),
        baseline_fingerprint=_protocol_fingerprint(
            b"yinshi.workspace-replica.state\x00v1\x00baseline\x00", canonical.baseline
        ),
        desired_fingerprint=_protocol_fingerprint(
            b"yinshi.workspace-replica.state\x00v1\x00desired\x00", canonical.desired
        ),
        transition_count=len(_corrected_compile_transitions(canonical)),
    )


@dataclass(frozen=True)
class IndexEntry:
    """One semantic index entry with independent identity fields.

    ``raw_path`` is raw path bytes, never display text. ``stage`` is the
    semantic stage: one stage-zero entry per path, or unique conflict stages
    1 through 3 for one unmerged path. Extended semantic flags remain explicit
    so exact index state survives validation and fingerprinting.
    """

    raw_path: bytes
    stage: int
    mode: str
    object_id: str
    intent_to_add: bool
    skip_worktree: bool
    assume_unchanged: bool


@dataclass(frozen=True)
class ManifestEntry:
    """One working-state manifest leaf with its bytes carried separately.

    ``content`` is the raw working bytes for files and the opaque symlink
    text for symlinks. Symlink text is never resolved or traversed.
    """

    raw_path: bytes
    kind: str
    content: bytes
    executable: bool
    nlink: int
    privilege_bits: bool


def canonical_path_base64(raw_path: bytes) -> str:
    """Encode immutable raw path bytes as canonical padded base64."""
    if type(raw_path) is not bytes:
        raise SnapshotRejected(
            "invalid_input_type",
            "path identity must use immutable bytes",
        )
    return base64.b64encode(raw_path).decode("ascii")


def raw_path_from_base64(encoded: str) -> bytes:
    """Strictly decode canonical base64 back to raw path bytes.

    The decoded value must re-encode to exactly the input text. This rejects
    whitespace, wrong padding, and non-zero discarded bits. Display text is
    never an identity input.
    """
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise SnapshotRejected(
            "invalid_base64_path",
            "path identity must be canonical base64",
        ) from exc
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise SnapshotRejected(
            "invalid_base64_path",
            "path identity must re-encode to the identical base64 text",
        )
    return decoded


def _is_unsafe_path(raw_path: bytes) -> bool:
    """Decide whether raw path bytes are unsafe for canonical mutation.

    Paths must be relative, non-empty, contain no NUL byte, and split into
    non-empty components with no dot names and no administrative ``.git``
    component. Bytes are never normalized or decoded.
    """
    if not raw_path or raw_path.startswith(b"/") or b"\x00" in raw_path:
        return True
    components = raw_path.split(b"/")
    return any(component in (b"", b".", b"..", b".git") for component in components)


_MANIFEST_KINDS = {"file", "symlink", "directory"}
_INDEX_MODES = {"100644", "100755", "120000"}


def _is_directory_prefix(ancestor: bytes, descendant: bytes) -> bool:
    """Return whether ``ancestor`` bytes are the directory prefix of ``descendant``."""
    return descendant.startswith(ancestor + b"/")


@dataclass(frozen=True)
class IntentFact:
    """Durable request intent recorded before one effect."""

    fingerprint: str
    action_id: str
    receipt_id: str


@dataclass(frozen=True)
class JournalPosition:
    """Position assigned by one trusted append-only journal."""

    journal_id: str
    sequence: int
    append_receipt_id: str


@dataclass(frozen=True)
class SynchronizationFact:
    """Durable synchronization bound to one action and optional creation."""

    fingerprint: str
    action_id: str
    requirement: str
    receipt_id: str
    journal_position: JournalPosition
    creation_receipt_id: str | None = None


@dataclass(frozen=True)
class CompletionFact:
    """Durable completion bound to the concrete completion postimage."""

    fingerprint: str
    action_id: str
    postimage_fingerprint: str
    receipt_id: str


@dataclass(frozen=True)
class ReconciliationJournal:
    """Durable retained inputs and protocol progression facts."""

    retained_inputs: RetainedInputs
    intents: tuple[IntentFact, ...]
    preparations: tuple[PreparationFact, ...]
    synchronizations: tuple[SynchronizationFact, ...]
    completions: tuple[CompletionFact, ...]


@dataclass(frozen=True)
class PreparationFact:
    """Durable concrete identity returned by one no-replace materialization."""

    fingerprint: str
    slot_id: str
    identity: NodeIdentity
    creation_receipt_id: str
    journal_position: JournalPosition


@dataclass(frozen=True)
class ExpectedObservation:
    """Exact expected image whose prepared references require durable resolution."""

    identity: OperationIdentity
    replica_state: ReplicaState | DesiredReplicaState
    boundaries: tuple[Boundary, ...]
    parents: tuple[ParentBinding, ...]
    storage: tuple[NodeState | DesiredNodeState | AbsentState, ...]
    control: ControlState


@dataclass(frozen=True)
class ActionContract:
    """One corrected protocol action with durable progression requirements."""

    action_id: str
    fingerprint: str
    stage: int
    owner: str
    effect_kind: str
    preimage: ExpectedObservation
    permitted_effect_postimages: tuple[ExpectedObservation, ...]
    synchronization_requirements: tuple[str, ...]
    parent_synchronizations: tuple[ParentBinding, ...]
    completion_postimage: ExpectedObservation
    effect_payload: bytes | None
    prepared_slot_id: str | None
    creates_prepared_identity: bool


ProtocolAction = ActionContract


@dataclass(frozen=True)
class _TransitionStep:
    """One normalized transition compiled from a validated specification."""

    stage: int
    owner: str
    effect_kind: str
    synchronization: tuple[str, ...]
    parent_synchronizations: tuple[ParentBinding, ...] = ()
    payload: bytes | None = None
    prepared_slot_id: str | None = None


_CONTENT_MUTATION_EFFECT_KINDS = frozenset(
    {
        "promote_required_object",
        "quarantine_working_node",
        "install_working_node",
        "restore_working_node",
        "quarantine_index",
        "install_index",
        "quarantine_reflog",
        "install_reflog",
        "quarantine_selected_ref",
        "install_selected_ref",
    }
)


@dataclass(frozen=True)
class ReconciliationPlan:
    """Deterministic corrected action sequence derived from a bound specification."""

    fingerprint: str
    actions: tuple[ProtocolAction, ...]

    @property
    def content_mutation_count(self) -> int:
        """Count canonical-content mutation actions without granting authority."""
        return sum(action.effect_kind in _CONTENT_MUTATION_EFFECT_KINDS for action in self.actions)


ProtocolPlan = ReconciliationPlan


def _corrected_validate_control(control: object) -> ControlState:
    if type(control) is not ControlState:
        raise SnapshotRejected("invalid_input_type", "control state type is invalid")
    receipt_fields = (
        "application_intent",
        "reservation",
        "broker_acceptance",
        "admission_gate",
        "drain",
        "export_validation",
        "inventory_validation",
        "baseline_acceptance",
        "verification",
        "publication",
        "destination_binding",
        "binding_acknowledgment",
        "release_intent",
        "physical_release",
        "logical_release",
        "admission",
    )
    receipts: list[ControlReceipt] = []
    for field_name in receipt_fields:
        receipt = getattr(control, field_name)
        if receipt is not None:
            receipts.append(receipt)
    for collection_name in ("exclusion_receipts", "effect_receipts"):
        collection = _corrected_require_tuple(
            getattr(control, collection_name), f"control.{collection_name}"
        )
        for receipt in collection:
            if type(receipt) is not ControlReceipt:
                raise SnapshotRejected(
                    "invalid_input_type",
                    "control receipt type is invalid",
                )
            assert isinstance(receipt, ControlReceipt)
            receipts.append(receipt)
    for receipt in receipts:
        if type(receipt) is not ControlReceipt:
            raise SnapshotRejected("invalid_input_type", "control receipt type is invalid")
        _corrected_require_str(receipt.receipt_id, "control.receipt_id")
        _corrected_require_str(receipt.owner, "control.owner")
    return control


def _corrected_validate_fresh_observation(
    bound: BoundSpecification,
    current: object,
) -> FreshObservation:
    if type(current) is not FreshObservation:
        raise SnapshotRejected("invalid_input_type", "fresh observation type is invalid")
    if type(current.identity) is not OperationIdentity:
        raise SnapshotRejected("invalid_input_type", "fresh operation identity type is invalid")
    for field_name in (
        "incarnation",
        "binding_id",
        "selected_authority",
        "operation_id",
        "reservation_id",
        "destination_binding",
        "physical_target_id",
        "acknowledgment_receipt_id",
        "drain_receipt_id",
        "export_receipt_id",
        "object_format",
        "inventory_receipt_id",
    ):
        _corrected_require_str(
            getattr(current.identity, field_name), f"fresh.identity.{field_name}"
        )
    _corrected_require_int(current.identity.generation, "fresh.identity.generation")
    _corrected_require_int(current.identity.replica_generation, "fresh.identity.replica_generation")
    _corrected_validate_replica(
        current.replica_state,
        bound.spec.identity.object_format,
        "fresh",
    )
    boundaries = _corrected_require_tuple(current.boundaries, "fresh.boundaries")
    validated_boundaries: list[Boundary] = []
    for boundary in boundaries:
        if type(boundary) is not Boundary:
            raise SnapshotRejected("invalid_input_type", "fresh boundary type is invalid")
        validated_boundaries.append(boundary)
        _corrected_require_str(boundary.boundary_id, "fresh.boundary.boundary_id")
        _corrected_require_str(boundary.role, "fresh.boundary.role")
        _corrected_validate_location(boundary.location, "fresh.boundary.location")
        _corrected_validate_identity(boundary.identity, "fresh.boundary.identity")
        ancestors = _corrected_require_tuple(boundary.ancestors, "fresh.boundary.ancestors")
        for ancestor in ancestors:
            _corrected_validate_node(ancestor, "fresh.boundary.ancestor")
    parents = _corrected_require_tuple(current.parents, "fresh.parents")
    parent_locations: dict[Location, NodeIdentity] = {}
    parent_identities: dict[NodeIdentity, Location] = {}
    validated_parents: list[ParentObservation] = []
    for parent in parents:
        if type(parent) is not ParentObservation:
            raise SnapshotRejected("invalid_input_type", "fresh parent type is invalid")
        validated_parents.append(parent)
        _corrected_validate_location(parent.location, "fresh.parent.location")
        _corrected_validate_identity(parent.identity, "fresh.parent.identity")
        if parent.location in parent_locations or parent.identity in parent_identities:
            raise SnapshotRejected(
                "duplicate_location", "fresh parent observation is duplicated or aliased"
            )
        parent_locations[parent.location] = parent.identity
        parent_identities[parent.identity] = parent.location
    storage = _corrected_require_tuple(current.storage, "fresh.storage")
    validated_storage: list[NodeState | AbsentState] = []
    for state in storage:
        validated_storage.append(_corrected_validate_storage(state, "fresh.storage"))
    identity_locations: dict[NodeIdentity, Location] = {}
    location_identities: dict[Location, NodeIdentity] = {}

    def register_fresh_identity_location(location: Location, identity: NodeIdentity) -> None:
        prior_location = identity_locations.get(identity)
        prior_identity = location_identities.get(location)
        identity_conflict = prior_location is not None and prior_location != location
        location_conflict = prior_identity is not None and prior_identity != identity
        if identity_conflict or location_conflict:
            raise SnapshotRejected(
                "ambiguous_node_identity",
                "fresh observation maps one identity or location to two physical nodes",
            )
        identity_locations[identity] = location
        location_identities[location] = identity

    for boundary in validated_boundaries:
        register_fresh_identity_location(boundary.location, boundary.identity)
        for ancestor in boundary.ancestors:
            register_fresh_identity_location(ancestor.location, ancestor.identity)
    replica = current.replica_state
    for state in (
        replica.index.storage,
        replica.refs.head,
        replica.refs.loose_ref,
        replica.refs.packed_refs,
        *replica.refs.reflogs,
        *replica.refs.unrelated_metadata,
        *replica.working_nodes,
    ):
        if type(state) is NodeState:
            register_fresh_identity_location(state.location, state.identity)
    for parent in validated_parents:
        register_fresh_identity_location(parent.location, parent.identity)
    for state in validated_storage:
        if type(state) is NodeState:
            register_fresh_identity_location(state.location, state.identity)
    _corrected_validate_control(current.control)
    return current


def _validate_fresh_initial(bound: BoundSpecification, current: FreshObservation) -> None:
    spec = bound.spec
    _corrected_validate_fresh_observation(bound, current)
    if current.identity != spec.identity:
        raise SnapshotRejected("stale_observation", "fresh operation identity changed")
    if current.replica_state != spec.baseline:
        raise SnapshotRejected("baseline_conflict", "fresh replica state differs from baseline")
    if current.boundaries != spec.boundaries:
        raise SnapshotRejected("protected_boundary_changed", "fresh protected boundaries changed")
    expected_parents = tuple(
        ParentObservation(binding.location, binding.reference.identity)
        for binding in _corrected_initial_parent_bindings(spec)
        if type(binding.reference) is ExistingNode
    )
    if current.parents != expected_parents:
        raise SnapshotRejected("baseline_conflict", "fresh parent identities differ")
    destination_ids = {destination.object_id for destination in spec.destination_objects}
    expected_storage = (
        tuple(slot.initial for slot in spec.preparation_slots)
        + tuple(marker.initial for marker in spec.ownership_markers)
        + tuple(slot.initial for slot in spec.quarantine_slots)
        + tuple(destination.storage for destination in spec.destination_objects)
        + tuple(
            AbsentState(_corrected_object_location(item.object_id))
            for item in spec.required_objects
            if item.object_id not in destination_ids
        )
    )

    def storage_key(state: NodeState | AbsentState) -> tuple[str, bytes]:
        return state.location.namespace, state.location.raw_path

    current_keys = tuple(storage_key(state) for state in current.storage)
    if len(current_keys) != len(set(current_keys)):
        raise SnapshotRejected("duplicate_location", "fresh storage location is duplicated")
    if tuple(sorted(current.storage, key=storage_key)) != tuple(
        sorted(expected_storage, key=storage_key)
    ):
        raise SnapshotRejected("baseline_conflict", "fresh auxiliary storage differs from baseline")
    if current.control != ControlState(
        None,
        None,
        None,
        (),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        (),
    ):
        raise SnapshotRejected("foreign_control_state", "fresh control state is not initial")


def _validate_retained_inputs(bound: BoundSpecification, retained: RetainedInputs) -> None:
    identity = bound.spec.identity
    if type(retained) is not RetainedInputs:
        raise SnapshotRejected("invalid_input_type", "retained input type is invalid")
    if type(retained.acknowledgment) is not OriginalAcknowledgmentFact:
        raise SnapshotRejected("invalid_input_type", "acknowledgment fact type is invalid")
    _corrected_require_str(retained.acknowledgment.receipt_id, "acknowledgment.receipt_id")
    _corrected_require_str(
        retained.acknowledgment.execution_owner_id,
        "acknowledgment.execution_owner_id",
    )
    if type(retained.drain) is not DrainFact:
        raise SnapshotRejected("invalid_input_type", "drain fact type is invalid")
    _corrected_require_str(retained.drain.receipt_id, "drain.receipt_id")
    _corrected_require_str(retained.drain.physical_target_id, "drain.physical_target_id")
    _corrected_require_int(retained.drain.replica_generation, "drain.replica_generation")
    if type(retained.export) is not ExportFact:
        raise SnapshotRejected("invalid_input_type", "export fact type is invalid")
    _corrected_require_str(retained.export.receipt_id, "export.receipt_id")
    _corrected_require_str(retained.export.physical_target_id, "export.physical_target_id")
    _corrected_require_int(retained.export.replica_generation, "export.replica_generation")
    _corrected_require_str(
        retained.export.specification_fingerprint,
        "export.specification_fingerprint",
    )
    if type(retained.inventory) is not InventoryFact:
        raise SnapshotRejected("invalid_input_type", "inventory fact type is invalid")
    for field_name in (
        "receipt_id",
        "original_owner_id",
        "object_format",
        "specification_fingerprint",
    ):
        _corrected_require_str(getattr(retained.inventory, field_name), f"inventory.{field_name}")
    for field_name in ("required_object_ids", "destination_object_ids"):
        values = _corrected_require_tuple(
            getattr(retained.inventory, field_name), f"inventory.{field_name}"
        )
        for value in values:
            _corrected_require_str(value, f"inventory.{field_name}")
    expected_acknowledgment = OriginalAcknowledgmentFact(
        identity.acknowledgment_receipt_id,
        identity.selected_authority,
    )
    expected_drain = DrainFact(
        identity.drain_receipt_id,
        identity.physical_target_id,
        identity.replica_generation,
    )
    expected_export = ExportFact(
        identity.export_receipt_id,
        identity.physical_target_id,
        identity.replica_generation,
        bound.fingerprint,
    )
    expected_inventory = InventoryFact(
        identity.inventory_receipt_id,
        identity.selected_authority,
        identity.object_format,
        tuple(item.object_id for item in bound.spec.required_objects),
        tuple(item.object_id for item in bound.spec.destination_objects),
        bound.fingerprint,
    )
    if retained.acknowledgment != expected_acknowledgment:
        raise SnapshotRejected("acknowledgment_unavailable", "original acknowledgment is not exact")
    if retained.drain != expected_drain:
        raise SnapshotRejected("drain_unverified", "drain receipt is not exact")
    if retained.export != expected_export:
        raise SnapshotRejected("export_stale", "export receipt is not exact")
    if retained.inventory != expected_inventory:
        raise SnapshotRejected("inventory_stale", "inventory receipt is not exact")


def _corrected_expected_storage_key(
    state: NodeState | DesiredNodeState | AbsentState,
) -> tuple[str, bytes]:
    return state.location.namespace, state.location.raw_path


def _corrected_control_receipt(
    spec: ReconciliationSpec,
    suffix: str,
    owner: str,
) -> ControlReceipt:
    return ControlReceipt(f"{spec.identity.operation_id}:{suffix}", owner)


def _corrected_object_location(object_id: str) -> Location:
    return Location(
        "admin",
        f"objects/{object_id[:2]}/{object_id[2:]}".encode("ascii"),
    )


def _corrected_initial_expected(spec: ReconciliationSpec) -> ExpectedObservation:
    destination_ids = {destination.object_id for destination in spec.destination_objects}
    missing_objects = tuple(
        AbsentState(_corrected_object_location(item.object_id))
        for item in spec.required_objects
        if item.object_id not in destination_ids
    )
    storage: tuple[NodeState | DesiredNodeState | AbsentState, ...] = (
        tuple(slot.initial for slot in spec.preparation_slots)
        + tuple(marker.initial for marker in spec.ownership_markers)
        + tuple(slot.initial for slot in spec.quarantine_slots)
        + tuple(destination.storage for destination in spec.destination_objects)
        + missing_objects
    )
    return ExpectedObservation(
        spec.identity,
        spec.baseline,
        spec.boundaries,
        _corrected_initial_parent_bindings(spec),
        tuple(sorted(storage, key=_corrected_expected_storage_key)),
        ControlState(
            None,
            None,
            None,
            (),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (),
        ),
    )


def _corrected_replace_expected_parents(
    image: ExpectedObservation,
    *,
    remove_reference: ExistingNode | PreparedNode | None = None,
    add: ParentBinding | None = None,
) -> ExpectedObservation:
    parents = list(image.parents)
    if remove_reference is not None:
        parents = [parent for parent in parents if parent.reference != remove_reference]
    if add is not None and add not in parents:
        conflict = next(
            (parent for parent in parents if parent.location == add.location and parent != add),
            None,
        )
        if conflict is not None:
            raise SnapshotRejected(
                "invalid_plan_integrity", "parent transition aliases an observed location"
            )
        parents.append(add)
    return replace(
        image,
        parents=tuple(sorted(parents, key=_corrected_parent_binding_key)),
    )


def _corrected_replace_expected_storage(
    image: ExpectedObservation,
    location: Location,
    replacement: NodeState | DesiredNodeState | AbsentState,
) -> ExpectedObservation:
    states = {_corrected_expected_storage_key(state): state for state in image.storage}
    states[(location.namespace, location.raw_path)] = replacement
    return replace(
        image,
        storage=tuple(sorted(states.values(), key=_corrected_expected_storage_key)),
    )


def _corrected_current_existing_state(
    image: ExpectedObservation,
    location: Location,
    identity: NodeIdentity,
) -> DesiredNodeState:
    """Read one existing node from the current symbolic image."""
    replica = _corrected_ensure_desired_replica(image.replica_state)
    candidates = [
        state
        for state, _field_name in _corrected_static_registrations(replica)
        if state.location == location
    ]
    candidates.extend(state for state in image.storage if state.location == location)
    for state in candidates:
        if type(state) is NodeState and state.identity == identity:
            return DesiredNodeState(
                state.location,
                ExistingNode(state.identity),
                state.kind,
                state.content,
                state.executable,
                state.nlink,
                state.privilege_bits,
            )
        if (
            type(state) is DesiredNodeState
            and type(state.reference) is ExistingNode
            and state.reference.identity == identity
        ):
            return state
    raise SnapshotRejected(
        "invalid_plan_integrity",
        "quarantine source is unavailable in the current image",
    )


def _corrected_adjust_working_parent_nlink(
    image: ExpectedObservation,
    parent: ParentBinding,
    delta: int,
) -> ExpectedObservation:
    """Adjust a represented working directory after moving one child directory."""
    replica = _corrected_ensure_desired_replica(image.replica_state)
    parent_nodes = [node for node in replica.working_nodes if node.location == parent.location]
    if not parent_nodes:
        return image
    if len(parent_nodes) != 1 or parent_nodes[0].kind != "directory":
        raise SnapshotRejected(
            "invalid_plan_integrity",
            "a moved directory has no unique directory parent",
        )
    parent_node = parent_nodes[0]
    adjusted_nlink = parent_node.nlink + delta
    if adjusted_nlink < 2:
        raise SnapshotRejected(
            "invalid_plan_integrity",
            "directory parent link count falls below two",
        )
    adjusted_node = replace(parent_node, nlink=adjusted_nlink)
    adjusted_manifest = tuple(
        (
            replace(entry, nlink=adjusted_nlink)
            if entry.raw_path == parent.location.raw_path
            else entry
        )
        for entry in replica.manifest
    )
    replica = replace(
        replica,
        manifest=adjusted_manifest,
        working_nodes=tuple(
            adjusted_node if node.location == parent.location else node
            for node in replica.working_nodes
        ),
    )
    return replace(image, replica_state=replica)


def _corrected_advance_expected(
    image: ExpectedObservation,
    spec: ReconciliationSpec,
    owner: str,
    effect_kind: str,
    payload: bytes | None,
    slot_id: str | None,
) -> ExpectedObservation:
    control = image.control
    if effect_kind == "commit_application_intent":
        control = replace(
            control,
            application_intent=_corrected_control_receipt(
                spec, "application-intent", "application"
            ),
        )
    elif effect_kind == "commit_reservation":
        control = replace(
            control,
            reservation=ControlReceipt(spec.identity.reservation_id, "application"),
        )
    elif effect_kind == "accept_broker_intent":
        control = replace(
            control,
            broker_acceptance=_corrected_control_receipt(spec, "broker-acceptance", "broker"),
        )
    elif effect_kind == "close_admission_gate":
        control = replace(
            control,
            admission_gate=_corrected_control_receipt(spec, "admission-gate-closed", "broker"),
        )
    elif effect_kind == "record_drain":
        control = replace(control, drain=ControlReceipt(spec.identity.drain_receipt_id, "broker"))
    elif effect_kind == "validate_export":
        control = replace(
            control,
            export_validation=ControlReceipt(spec.identity.export_receipt_id, "application"),
        )
    elif effect_kind == "validate_inventory":
        control = replace(
            control,
            inventory_validation=ControlReceipt(spec.identity.inventory_receipt_id, "application"),
        )
    elif effect_kind == "accept_baseline":
        control = replace(
            control,
            baseline_acceptance=_corrected_control_receipt(
                spec, "baseline-acceptance", "application"
            ),
        )
    elif effect_kind == "verify_postimage":
        control = replace(
            control,
            verification=_corrected_control_receipt(spec, "postimage-verification", "application"),
        )
    elif effect_kind == "publish_generation":
        control = replace(
            control,
            publication=_corrected_control_receipt(spec, "generation-publication", "broker"),
        )
        image = replace(
            image,
            replica_state=replace(
                image.replica_state,
                generation=spec.desired.generation,
            ),
        )
    elif effect_kind == "commit_destination_binding":
        control = replace(
            control,
            destination_binding=ControlReceipt(spec.identity.destination_binding, "application"),
        )
    elif effect_kind == "acknowledge_destination_binding":
        control = replace(
            control,
            binding_acknowledgment=_corrected_control_receipt(
                spec, "binding-acknowledgment", "broker"
            ),
        )
    elif effect_kind == "record_release_intent":
        control = replace(
            control,
            release_intent=_corrected_control_receipt(spec, "release-intent", "application"),
        )
    elif effect_kind == "physical_release":
        control = replace(
            control,
            physical_release=_corrected_control_receipt(spec, "physical-release", "broker"),
        )
    elif effect_kind == "logical_release":
        control = replace(
            control,
            logical_release=_corrected_control_receipt(spec, "logical-release", "application"),
        )
    elif effect_kind == "admit_readers":
        control = replace(
            control,
            admission=_corrected_control_receipt(spec, "reader-admission", "broker"),
        )
    elif effect_kind == "record_marker_exclusion":
        if slot_id is None:
            raise SnapshotRejected("invalid_plan_integrity", "marker exclusion slot is absent")
        exclusion = _corrected_control_receipt(spec, f"exclusion:{slot_id}", "broker")
        control = replace(
            control,
            exclusion_receipts=tuple(
                sorted(
                    control.exclusion_receipts + (exclusion,),
                    key=lambda receipt: receipt.receipt_id,
                )
            ),
        )
    physical_move_effects = {
        "create_ownership_marker",
        "materialize_preparation",
        "promote_required_object",
        "quarantine_working_node",
        "install_working_node",
        "restore_working_node",
        "quarantine_index",
        "install_index",
        "quarantine_reflog",
        "install_reflog",
        "quarantine_selected_ref",
        "install_selected_ref",
        "remove_ownership_marker",
        "record_marker_exclusion",
    }
    if effect_kind not in physical_move_effects:
        effect_suffix = effect_kind if slot_id is None else f"{effect_kind}:{slot_id}"
        effect_receipt = _corrected_control_receipt(spec, f"effect:{effect_suffix}", owner)
        control = replace(control, effect_receipts=control.effect_receipts + (effect_receipt,))
    image = replace(image, control=control)

    if effect_kind == "create_ownership_marker":
        marker = next(item for item in spec.ownership_markers if item.slot_id == slot_id)
        if payload is None:
            raise SnapshotRejected("invalid_plan_integrity", "ownership payload is absent")
        desired = DesiredNodeState(
            marker.destination,
            PreparedNode(marker.slot_id),
            "file",
            payload,
            marker.executable,
            1,
            False,
        )
        image = _corrected_replace_expected_storage(image, marker.destination, desired)
    elif effect_kind == "materialize_preparation":
        slot = next(item for item in spec.preparation_slots if item.slot_id == slot_id)
        desired = DesiredNodeState(
            slot.destination,
            PreparedNode(slot.slot_id),
            slot.desired_kind,
            slot.desired_content,
            slot.executable,
            2 if slot.desired_kind == "directory" else 1,
            False,
        )
        image = _corrected_replace_expected_storage(image, slot.destination, desired)
    elif effect_kind == "promote_required_object":
        required = next(item for item in spec.required_objects if item.source_slot_id == slot_id)
        slot = next(item for item in spec.preparation_slots if item.slot_id == slot_id)
        image = _corrected_replace_expected_storage(
            image,
            slot.destination,
            AbsentState(slot.destination),
        )
        object_location = _corrected_object_location(required.object_id)
        image = _corrected_replace_expected_storage(
            image,
            object_location,
            DesiredNodeState(
                object_location,
                PreparedNode(slot.slot_id),
                slot.desired_kind,
                slot.desired_content,
                slot.executable,
                1,
                False,
            ),
        )
    elif effect_kind in {
        "quarantine_working_node",
        "quarantine_index",
        "quarantine_reflog",
        "quarantine_selected_ref",
    }:
        quarantine = next(item for item in spec.quarantine_slots if item.slot_id == slot_id)
        source = _corrected_current_existing_state(
            image,
            quarantine.source,
            quarantine.source_identity,
        )
        image = _corrected_replace_expected_storage(
            image,
            quarantine.destination,
            DesiredNodeState(
                quarantine.destination,
                source.reference,
                source.kind,
                source.content,
                source.executable,
                source.nlink,
                source.privilege_bits,
            ),
        )
        replica = _corrected_ensure_desired_replica(image.replica_state)
        if effect_kind == "quarantine_working_node":
            replica = replace(
                replica,
                manifest=tuple(
                    entry
                    for entry in replica.manifest
                    if entry.raw_path != quarantine.source.raw_path
                ),
                working_nodes=tuple(
                    node for node in replica.working_nodes if node.location != quarantine.source
                ),
            )
        elif effect_kind == "quarantine_index":
            replica = replace(
                replica,
                index=DesiredIndexState(AbsentState(quarantine.source), (), False),
            )
        elif effect_kind == "quarantine_reflog":
            replica = replace(
                replica,
                refs=replace(
                    replica.refs,
                    reflogs=tuple(
                        (
                            AbsentState(quarantine.source)
                            if state.location == quarantine.source
                            else state
                        )
                        for state in replica.refs.reflogs
                    ),
                ),
            )
        else:
            replica = replace(
                replica,
                refs=replace(
                    replica.refs,
                    resolved_oid=replica.refs.packed_fallback_oid,
                    loose_ref=AbsentState(quarantine.source),
                ),
            )
        image = replace(image, replica_state=replica)
        if source.kind == "directory":
            image = _corrected_adjust_working_parent_nlink(
                image,
                quarantine.source_parent,
                -1,
            )
            image = _corrected_replace_expected_parents(
                image,
                remove_reference=source.reference,
            )
    elif effect_kind == "install_working_node":
        desired = next(
            node
            for node in spec.desired.working_nodes
            if type(node.reference) is PreparedNode and node.reference.slot_id == slot_id
        )
        slot = next(item for item in spec.preparation_slots if item.slot_id == slot_id)
        image = _corrected_replace_expected_storage(
            image,
            slot.destination,
            AbsentState(slot.destination),
        )
        replica = _corrected_ensure_desired_replica(image.replica_state)
        manifest_entry = next(
            entry for entry in spec.desired.manifest if entry.raw_path == desired.location.raw_path
        )
        installed = desired
        if desired.kind == "directory":
            installed = replace(desired, nlink=2)
            manifest_entry = replace(manifest_entry, nlink=2)
        replica = replace(
            replica,
            manifest=tuple(
                sorted(replica.manifest + (manifest_entry,), key=lambda entry: entry.raw_path)
            ),
            working_nodes=tuple(
                sorted(
                    replica.working_nodes + (installed,),
                    key=lambda node: _corrected_expected_storage_key(node),
                )
            ),
        )
        image = replace(image, replica_state=replica)
        if desired.kind == "directory":
            image = _corrected_adjust_working_parent_nlink(
                image,
                slot.installation_parent,
                1,
            )
            image = _corrected_replace_expected_parents(
                image,
                add=ParentBinding(desired.location, PreparedNode(slot.slot_id)),
            )
    elif effect_kind == "restore_working_node":
        quarantine = next(item for item in spec.quarantine_slots if item.slot_id == slot_id)
        desired = next(
            node
            for node in spec.desired.working_nodes
            if type(node.reference) is ExistingNode
            and node.reference.identity == quarantine.source_identity
        )
        stored = _corrected_current_existing_state(
            image,
            quarantine.destination,
            quarantine.source_identity,
        )
        image = _corrected_replace_expected_storage(
            image,
            quarantine.destination,
            AbsentState(quarantine.destination),
        )
        replica = _corrected_ensure_desired_replica(image.replica_state)
        restored = replace(desired, nlink=stored.nlink)
        manifest_entry = next(
            entry for entry in spec.desired.manifest if entry.raw_path == desired.location.raw_path
        )
        manifest_entry = replace(manifest_entry, nlink=stored.nlink)
        replica = replace(
            replica,
            manifest=tuple(
                sorted(replica.manifest + (manifest_entry,), key=lambda entry: entry.raw_path)
            ),
            working_nodes=tuple(
                sorted(
                    replica.working_nodes + (restored,),
                    key=lambda node: _corrected_expected_storage_key(node),
                )
            ),
        )
        image = replace(image, replica_state=replica)
        if desired.kind == "directory":
            restoration_parent = quarantine.restoration_parent
            if restoration_parent is None:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "directory restoration has no parent binding",
                )
            image = _corrected_adjust_working_parent_nlink(
                image,
                restoration_parent,
                1,
            )
            image = _corrected_replace_expected_parents(
                image,
                add=ParentBinding(desired.location, ExistingNode(quarantine.source_identity)),
            )
    elif effect_kind in {"install_index", "install_reflog", "install_selected_ref"}:
        slot = next(item for item in spec.preparation_slots if item.slot_id == slot_id)
        image = _corrected_replace_expected_storage(
            image,
            slot.destination,
            AbsentState(slot.destination),
        )
        replica = _corrected_ensure_desired_replica(image.replica_state)
        if effect_kind == "install_index":
            replica = replace(replica, index=spec.desired.index)
        elif effect_kind == "install_reflog":
            desired_reflog = next(
                state
                for state in spec.desired.refs.reflogs
                if type(state) is DesiredNodeState
                and type(state.reference) is PreparedNode
                and state.reference.slot_id == slot_id
            )
            remaining = tuple(
                state for state in replica.refs.reflogs if state.location != desired_reflog.location
            )
            replica = replace(
                replica,
                refs=replace(
                    replica.refs,
                    reflogs=tuple(
                        sorted(
                            remaining + (desired_reflog,),
                            key=_corrected_expected_storage_key,
                        )
                    ),
                ),
            )
        else:
            replica = replace(
                replica,
                refs=replace(
                    replica.refs,
                    resolved_oid=spec.desired.refs.resolved_oid,
                    loose_ref=spec.desired.refs.loose_ref,
                ),
            )
        image = replace(image, replica_state=replica)
    elif effect_kind == "select_packed_fallback":
        replica = _corrected_ensure_desired_replica(image.replica_state)
        image = replace(
            image,
            replica_state=replace(
                replica,
                refs=replace(
                    replica.refs,
                    resolved_oid=replica.refs.packed_fallback_oid,
                ),
            ),
        )
    elif effect_kind == "finalize_index_absence":
        replica = _corrected_ensure_desired_replica(image.replica_state)
        image = replace(image, replica_state=replace(replica, index=spec.desired.index))
    elif effect_kind == "acknowledge_working_install":
        replica = _corrected_ensure_desired_replica(image.replica_state)
        if (
            replica.manifest != spec.desired.manifest
            or replica.working_nodes != spec.desired.working_nodes
        ):
            raise SnapshotRejected(
                "invalid_plan_integrity",
                "working acknowledgment cannot synthesize physical state",
            )
    elif effect_kind == "acknowledge_index":
        replica = _corrected_ensure_desired_replica(image.replica_state)
        if replica.index != spec.desired.index:
            raise SnapshotRejected(
                "invalid_plan_integrity",
                "index acknowledgment cannot synthesize physical state",
            )
    elif effect_kind == "acknowledge_metadata":
        replica = _corrected_ensure_desired_replica(image.replica_state)
        if replica.refs != spec.desired.refs:
            raise SnapshotRejected(
                "invalid_plan_integrity",
                "metadata acknowledgment cannot synthesize physical ref state",
            )
    elif effect_kind == "remove_ownership_marker":
        marker = next(item for item in spec.ownership_markers if item.slot_id == slot_id)
        image = _corrected_replace_expected_storage(
            image,
            marker.destination,
            AbsentState(marker.destination),
        )
    return image


def _corrected_resolve_node(
    node: DesiredNodeState,
    preparations: dict[str, PreparationFact],
) -> NodeState:
    reference = node.reference
    if isinstance(reference, ExistingNode):
        identity = reference.identity
    else:
        preparation = preparations.get(reference.slot_id)
        if preparation is None:
            raise SnapshotRejected(
                "lost_preparation_identity",
                f"prepared identity for {reference.slot_id} is unavailable",
            )
        identity = preparation.identity
    return NodeState(
        node.location,
        identity,
        node.kind,
        node.content,
        node.executable,
        node.nlink,
        node.privilege_bits,
    )


def _corrected_resolve_storage(
    state: NodeState | DesiredNodeState | AbsentState,
    preparations: dict[str, PreparationFact],
) -> NodeState | AbsentState:
    if isinstance(state, DesiredNodeState):
        return _corrected_resolve_node(state, preparations)
    if isinstance(state, (NodeState, AbsentState)):
        return state
    raise SnapshotRejected("invalid_input_type", "expected storage type is invalid")


def _corrected_resolve_replica(
    replica: ReplicaState | DesiredReplicaState,
    preparations: dict[str, PreparationFact],
) -> ReplicaState:
    if isinstance(replica, ReplicaState):
        return replica
    index = IndexState(
        _corrected_resolve_storage(replica.index.storage, preparations),
        replica.index.entries,
        replica.index.sparse_index,
    )
    refs = RefState(
        replica.refs.selected_ref,
        replica.refs.head_binding,
        replica.refs.resolved_oid,
        replica.refs.packed_fallback_oid,
        _corrected_resolve_node(replica.refs.head, preparations),
        _corrected_resolve_storage(replica.refs.loose_ref, preparations),
        _corrected_resolve_storage(replica.refs.packed_refs, preparations),
        tuple(_corrected_resolve_storage(state, preparations) for state in replica.refs.reflogs),
        tuple(
            _corrected_resolve_storage(state, preparations)
            for state in replica.refs.unrelated_metadata
        ),
    )
    return ReplicaState(
        replica.generation,
        replica.replica_generation,
        index,
        refs,
        replica.manifest,
        tuple(_corrected_resolve_node(node, preparations) for node in replica.working_nodes),
    )


def _corrected_resolve_parent(
    binding: ParentBinding,
    preparations: dict[str, PreparationFact],
) -> ParentObservation:
    reference = binding.reference
    if isinstance(reference, ExistingNode):
        identity = reference.identity
    else:
        preparation = preparations.get(reference.slot_id)
        if preparation is None:
            raise SnapshotRejected(
                "lost_preparation_identity",
                f"prepared parent identity for {reference.slot_id} is unavailable",
            )
        identity = preparation.identity
    return ParentObservation(binding.location, identity)


def materialize_expected_observation(
    expected: ExpectedObservation,
    preparation_facts: tuple[PreparationFact, ...],
) -> FreshObservation:
    """Resolve desired-node references using only durable preparation facts."""
    if type(expected) is not ExpectedObservation or type(preparation_facts) is not tuple:
        raise SnapshotRejected("invalid_input_type", "expected image inputs have invalid types")
    preparations: dict[str, PreparationFact] = {}
    for fact in preparation_facts:
        if type(fact) is not PreparationFact:
            raise SnapshotRejected("invalid_input_type", "preparation fact type is invalid")
        if fact.slot_id in preparations:
            raise SnapshotRejected("invalid_journal", "preparation slot identity is duplicated")
        preparations[fact.slot_id] = fact
    storage = tuple(_corrected_resolve_storage(state, preparations) for state in expected.storage)
    parents = tuple(
        sorted(
            (_corrected_resolve_parent(parent, preparations) for parent in expected.parents),
            key=lambda parent: (
                parent.location.namespace,
                parent.location.raw_path,
                parent.identity.filesystem_id,
                parent.identity.node_id,
            ),
        )
    )
    return FreshObservation(
        expected.identity,
        _corrected_resolve_replica(expected.replica_state, preparations),
        expected.boundaries,
        parents,
        tuple(sorted(storage, key=_corrected_storage_key)),
        expected.control,
    )


def observation_fingerprint(observation: FreshObservation) -> str:
    """Fingerprint one complete concrete fresh observation."""
    if type(observation) is not FreshObservation:
        raise SnapshotRejected("invalid_input_type", "fresh observation type is invalid")
    return _protocol_fingerprint(
        b"yinshi.workspace-replica.observation\x00v1\x00",
        observation,
    )


def _corrected_desired_node_from_concrete(node: NodeState) -> DesiredNodeState:
    return DesiredNodeState(
        node.location,
        ExistingNode(node.identity),
        node.kind,
        node.content,
        node.executable,
        node.nlink,
        node.privilege_bits,
    )


def _corrected_desired_storage_from_concrete(
    state: NodeState | AbsentState,
) -> DesiredNodeState | AbsentState:
    if isinstance(state, NodeState):
        return _corrected_desired_node_from_concrete(state)
    return state


def _corrected_ref_as_desired(refs: RefState) -> DesiredRefState:
    return DesiredRefState(
        refs.selected_ref,
        refs.head_binding,
        refs.resolved_oid,
        refs.packed_fallback_oid,
        _corrected_desired_node_from_concrete(refs.head),
        _corrected_desired_storage_from_concrete(refs.loose_ref),
        _corrected_desired_storage_from_concrete(refs.packed_refs),
        tuple(_corrected_desired_storage_from_concrete(state) for state in refs.reflogs),
        tuple(_corrected_desired_storage_from_concrete(state) for state in refs.unrelated_metadata),
    )


def _corrected_replica_as_desired(replica: ReplicaState) -> DesiredReplicaState:
    return DesiredReplicaState(
        replica.generation,
        replica.replica_generation,
        DesiredIndexState(
            _corrected_desired_storage_from_concrete(replica.index.storage),
            replica.index.entries,
            replica.index.sparse_index,
        ),
        _corrected_ref_as_desired(replica.refs),
        replica.manifest,
        tuple(_corrected_desired_node_from_concrete(node) for node in replica.working_nodes),
    )


def _corrected_ensure_desired_replica(
    replica: ReplicaState | DesiredReplicaState,
) -> DesiredReplicaState:
    if isinstance(replica, DesiredReplicaState):
        return replica
    return _corrected_replica_as_desired(replica)


def _corrected_compile_transitions(
    spec: ReconciliationSpec,
) -> tuple[_TransitionStep, ...]:
    """Compile the sole normalized transition inventory for binding and planning."""
    action_inputs: list[
        tuple[
            int,
            str,
            str,
            tuple[str, ...],
            tuple[ParentBinding, ...],
            bytes | None,
            str | None,
        ]
    ] = []

    def add(
        stage: int,
        owner: str,
        effect_kind: str,
        synchronization: tuple[str, ...],
        payload: bytes | None = None,
        prepared_slot_id: str | None = None,
        parent_synchronizations: tuple[ParentBinding, ...] = (),
    ) -> None:
        distinct_parents = tuple(
            sorted(set(parent_synchronizations), key=_corrected_parent_binding_key)
        )
        action_inputs.append(
            (
                stage,
                owner,
                effect_kind,
                synchronization,
                distinct_parents,
                payload,
                prepared_slot_id,
            )
        )

    add(1, "application", "commit_application_intent", ("application-journal",))
    add(1, "application", "commit_reservation", ("application-journal",))
    add(2, "broker", "accept_broker_intent", ("broker-journal",))
    for marker in spec.ownership_markers:
        add(
            2,
            "broker",
            "create_ownership_marker",
            (f"content:{marker.slot_id}",),
            b"",
            marker.slot_id,
            (marker.parent,),
        )
        add(
            2,
            "broker",
            "record_marker_exclusion",
            ("broker-journal",),
            None,
            marker.slot_id,
        )
    add(3, "broker", "close_admission_gate", ("broker-journal",))
    add(3, "broker", "record_drain", ("broker-journal",))
    add(4, "application", "validate_original_acknowledgment", ("application-journal",))
    add(4, "application", "validate_export", ("application-journal",))
    add(4, "application", "validate_inventory", ("application-journal",))
    add(5, "application", "accept_baseline", ("application-journal",))
    for preparation_slot in spec.preparation_slots:
        requirements = (f"node:{preparation_slot.slot_id}",)
        add(
            6,
            "broker",
            "materialize_preparation",
            requirements,
            preparation_slot.desired_content,
            preparation_slot.slot_id,
            (preparation_slot.materialization_parent,),
        )
    add(6, "application", "acknowledge_preparation", ("application-journal",))
    destination_ids = {item.object_id for item in spec.destination_objects}
    slots_by_id = {slot.slot_id: slot for slot in spec.preparation_slots}
    for required in spec.required_objects:
        if required.object_id not in destination_ids:
            add(
                7,
                "broker",
                "promote_required_object",
                (f"content:{required.object_id}",),
                None,
                required.source_slot_id,
                (
                    slots_by_id[required.source_slot_id].materialization_parent,
                    slots_by_id[required.source_slot_id].installation_parent,
                ),
            )
    add(7, "application", "acknowledge_object_promotion", ("application-journal",))
    baseline_working_by_identity = {node.identity: node for node in spec.baseline.working_nodes}
    working_quarantines = [
        slot for slot in spec.quarantine_slots if slot.source.namespace == "working"
    ]
    working_quarantines.sort(
        key=lambda slot: (
            baseline_working_by_identity[slot.source_identity].kind == "directory",
            -slot.source.raw_path.count(b"/"),
            slot.source.raw_path,
        )
    )
    for working_quarantine in working_quarantines:
        add(
            8,
            "broker",
            "quarantine_working_node",
            (),
            None,
            working_quarantine.slot_id,
            (working_quarantine.source_parent, working_quarantine.destination_parent),
        )
    add(8, "application", "acknowledge_working_quarantine", ("application-journal",))
    quarantine_by_identity = {slot.source_identity: slot for slot in working_quarantines}
    working_installations = [
        node
        for node in spec.desired.working_nodes
        if isinstance(node.reference, PreparedNode)
        or (
            isinstance(node.reference, ExistingNode)
            and node.reference.identity in quarantine_by_identity
        )
    ]
    working_installations.sort(
        key=lambda node: (
            node.kind != "directory",
            node.location.raw_path.count(b"/") if node.kind == "directory" else 0,
            node.location.raw_path,
        )
    )
    for node in working_installations:
        reference = node.reference
        if isinstance(reference, PreparedNode):
            effect_kind = "install_working_node"
            source_id = reference.slot_id
        else:
            effect_kind = "restore_working_node"
            source_id = quarantine_by_identity[reference.identity].slot_id
        if isinstance(reference, PreparedNode):
            source_parent = slots_by_id[source_id].materialization_parent
            destination_parent = slots_by_id[source_id].installation_parent
        else:
            quarantine = quarantine_by_identity[reference.identity]
            source_parent = quarantine.destination_parent
            restoration_parent = quarantine.restoration_parent
            if restoration_parent is None:
                raise SnapshotRejected(
                    "invalid_specification", "restored node has no destination parent"
                )
            destination_parent = restoration_parent
        add(
            9,
            "broker",
            effect_kind,
            (f"node:{source_id}",),
            None,
            source_id,
            (source_parent, destination_parent),
        )
    add(9, "application", "acknowledge_working_install", ("application-journal",))

    baseline_index_as_desired = DesiredIndexState(
        _corrected_desired_storage_from_concrete(spec.baseline.index.storage),
        _corrected_canonical_index_entries(spec.baseline.index.entries),
        spec.baseline.index.sparse_index,
    )
    desired_index_for_comparison = replace(
        spec.desired.index,
        entries=_corrected_canonical_index_entries(spec.desired.index.entries),
    )
    if desired_index_for_comparison != baseline_index_as_desired:
        if type(spec.baseline.index.storage) is NodeState:
            index_quarantine = next(
                item
                for item in spec.quarantine_slots
                if item.source == spec.baseline.index.storage.location
                and item.source_identity == spec.baseline.index.storage.identity
            )
            add(
                10,
                "broker",
                "quarantine_index",
                (),
                None,
                index_quarantine.slot_id,
                (index_quarantine.source_parent, index_quarantine.destination_parent),
            )
        if type(spec.desired.index.storage) is DesiredNodeState:
            reference = spec.desired.index.storage.reference
            if not isinstance(reference, PreparedNode):
                raise SnapshotRejected("invalid_specification", "changed index is not prepared")
            add(
                10,
                "broker",
                "install_index",
                (f"node:{reference.slot_id}",),
                None,
                reference.slot_id,
                (
                    slots_by_id[reference.slot_id].materialization_parent,
                    slots_by_id[reference.slot_id].installation_parent,
                ),
            )
        else:
            add(
                10,
                "broker",
                "finalize_index_absence",
                (),
                parent_synchronizations=(index_quarantine.source_parent,),
            )
    add(10, "application", "acknowledge_index", ("application-journal",))

    baseline_reflogs = {
        _corrected_storage_key(state): state for state in spec.baseline.refs.reflogs
    }
    desired_reflogs = {
        _corrected_expected_storage_key(state): state for state in spec.desired.refs.reflogs
    }
    for location_key in sorted(set(baseline_reflogs) | set(desired_reflogs)):
        baseline_state = baseline_reflogs.get(location_key)
        desired_state = desired_reflogs.get(location_key)
        unchanged = (
            baseline_state is not None
            and desired_state == _corrected_desired_storage_from_concrete(baseline_state)
        )
        if unchanged:
            continue
        if type(baseline_state) is NodeState:
            reflog_quarantine = next(
                item
                for item in spec.quarantine_slots
                if item.source == baseline_state.location
                and item.source_identity == baseline_state.identity
            )
            add(
                11,
                "broker",
                "quarantine_reflog",
                (),
                None,
                reflog_quarantine.slot_id,
                (reflog_quarantine.source_parent, reflog_quarantine.destination_parent),
            )
        if type(desired_state) is DesiredNodeState:
            reference = desired_state.reference
            if not isinstance(reference, PreparedNode):
                raise SnapshotRejected("invalid_specification", "changed reflog is not prepared")
            add(
                11,
                "broker",
                "install_reflog",
                (f"node:{reference.slot_id}",),
                None,
                reference.slot_id,
                (
                    slots_by_id[reference.slot_id].materialization_parent,
                    slots_by_id[reference.slot_id].installation_parent,
                ),
            )

    baseline_loose = spec.baseline.refs.loose_ref
    desired_loose = spec.desired.refs.loose_ref
    if desired_loose != _corrected_desired_storage_from_concrete(baseline_loose):
        if type(baseline_loose) is NodeState:
            selected_ref_quarantine = next(
                item
                for item in spec.quarantine_slots
                if item.source == baseline_loose.location
                and item.source_identity == baseline_loose.identity
            )
            add(
                11,
                "broker",
                "quarantine_selected_ref",
                (),
                None,
                selected_ref_quarantine.slot_id,
                (
                    selected_ref_quarantine.source_parent,
                    selected_ref_quarantine.destination_parent,
                ),
            )
        if type(desired_loose) is DesiredNodeState:
            reference = desired_loose.reference
            if not isinstance(reference, PreparedNode):
                raise SnapshotRejected("invalid_specification", "changed loose ref is not prepared")
            add(
                11,
                "broker",
                "install_selected_ref",
                (f"node:{reference.slot_id}",),
                None,
                reference.slot_id,
                (
                    slots_by_id[reference.slot_id].materialization_parent,
                    slots_by_id[reference.slot_id].installation_parent,
                ),
            )
        else:
            add(11, "application", "select_packed_fallback", ("application-journal",))
    add(11, "application", "acknowledge_metadata", ("application-journal",))
    add(12, "application", "verify_postimage", ("application-journal",))
    add(13, "broker", "publish_generation", ("broker-journal",))
    add(14, "application", "commit_destination_binding", ("application-journal",))
    add(15, "broker", "acknowledge_destination_binding", ("broker-journal",))
    add(16, "application", "record_release_intent", ("application-journal",))
    for marker in spec.ownership_markers:
        add(
            16,
            "broker",
            "remove_ownership_marker",
            (),
            None,
            marker.slot_id,
            (marker.parent,),
        )
    add(16, "broker", "physical_release", ("broker-journal",))
    add(16, "application", "logical_release", ("application-journal",))
    add(16, "broker", "admit_readers", ("broker-journal",))

    if len(action_inputs) > spec.limits.max_transitions:
        raise SnapshotRejected("inventory_limit_exceeded", "transition limit exceeded")
    return tuple(
        _TransitionStep(
            stage,
            owner,
            effect_kind,
            synchronization,
            parent_synchronizations,
            payload,
            slot_id,
        )
        for (
            stage,
            owner,
            effect_kind,
            synchronization,
            parent_synchronizations,
            payload,
            slot_id,
        ) in action_inputs
    )


def _derive_protocol_plan(bound: BoundSpecification) -> ProtocolPlan:
    spec = bound.spec
    transitions = _corrected_compile_transitions(spec)
    if len(transitions) != bound.transition_count:
        raise SnapshotRejected(
            "invalid_plan_integrity",
            "stored transition count differs from the compiled transition inventory",
        )
    actions: list[ProtocolAction] = []
    image = _corrected_initial_expected(spec)
    _corrected_validate_expected_image(image)
    for ordinal, transition in enumerate(transitions):
        effect_payload = transition.payload
        if transition.effect_kind == "create_ownership_marker":
            if transition.prepared_slot_id is None:
                raise SnapshotRejected("invalid_plan_integrity", "ownership marker slot is absent")
            marker = next(
                item
                for item in spec.ownership_markers
                if item.slot_id == transition.prepared_slot_id
            )
            effect_payload = (
                marker.payload_prefix + b"\x00fingerprint=" + bound.fingerprint.encode("ascii")
            )
        action_id = f"{bound.fingerprint}:{ordinal:04d}:{transition.effect_kind}"
        preimage = image
        postimage = _corrected_advance_expected(
            preimage,
            spec,
            transition.owner,
            transition.effect_kind,
            effect_payload,
            transition.prepared_slot_id,
        )
        _corrected_validate_expected_image(postimage)
        creates_prepared_identity = transition.effect_kind in {
            "create_ownership_marker",
            "materialize_preparation",
        }
        actions.append(
            ProtocolAction(
                action_id,
                bound.fingerprint,
                transition.stage,
                transition.owner,
                transition.effect_kind,
                preimage,
                (postimage,),
                transition.synchronization,
                transition.parent_synchronizations,
                postimage,
                effect_payload,
                transition.prepared_slot_id,
                creates_prepared_identity,
            )
        )
        image = postimage
    return ProtocolPlan(bound.fingerprint, tuple(actions))


def plan_reconciliation(
    bound: BoundSpecification,
    current: FreshObservation,
    retained: RetainedInputs,
) -> ProtocolPlan:
    """Derive the corrected protocol only from bound input and fresh observation."""
    if type(bound) is not BoundSpecification:
        raise SnapshotRejected("invalid_input_type", "bound specification type is invalid")
    rebound = bind_specification(bound.spec)
    if rebound != bound:
        raise SnapshotRejected("specification_fingerprint_mismatch", "bound specification changed")
    _validate_fresh_initial(bound, current)
    _validate_retained_inputs(bound, retained)
    return _derive_protocol_plan(bound)


@dataclass(frozen=True)
class ReplayDecision:
    """Next safe journal or effect step selected from fresh state."""

    kind: str
    action: ProtocolAction | None
    missing_requirements: tuple[str, ...] = ()


ProtocolReplayDecision = ReplayDecision


def _corrected_resolved_synchronization_requirements(
    action: ProtocolAction,
    preparations: dict[str, PreparationFact],
) -> tuple[str, ...]:
    requirements = list(action.synchronization_requirements)
    for binding in action.parent_synchronizations:
        parent = _corrected_resolve_parent(binding, preparations)
        digest = _protocol_fingerprint(
            b"yinshi.workspace-replica.parent-sync\x00v1\x00",
            (action.fingerprint, action.action_id, parent.location, parent.identity),
        )
        requirements.append(
            f"parent:{action.fingerprint.removeprefix('sha256:')[:12]}:{digest.removeprefix('sha256:')}"
        )
    if len(requirements) != len(set(requirements)):
        raise SnapshotRejected(
            "invalid_plan_integrity", "synchronization requirement is duplicated"
        )
    return tuple(requirements)


def resolve_synchronization_requirements(
    action: ProtocolAction,
    preparation_facts: tuple[PreparationFact, ...],
) -> tuple[str, ...]:
    """Resolve parent durability requirements from immutable preparation facts."""
    if type(action) is not ActionContract or type(preparation_facts) is not tuple:
        raise SnapshotRejected("invalid_input_type", "synchronization inputs are invalid")
    preparations: dict[str, PreparationFact] = {}
    for fact in preparation_facts:
        if type(fact) is not PreparationFact:
            raise SnapshotRejected("invalid_input_type", "preparation fact type is invalid")
        _corrected_require_str(fact.fingerprint, "preparation_fact.fingerprint")
        _corrected_require_str(fact.slot_id, "preparation_fact.slot_id")
        _corrected_validate_identity(fact.identity, "preparation_fact.identity")
        _corrected_require_str(
            fact.creation_receipt_id,
            "preparation_fact.creation_receipt_id",
        )
        if type(fact.journal_position) is not JournalPosition:
            raise SnapshotRejected(
                "invalid_input_type",
                "preparation_fact.journal_position type is invalid",
            )
        _corrected_require_str(
            fact.journal_position.journal_id,
            "preparation_fact.journal_position.journal_id",
        )
        _corrected_require_int(
            fact.journal_position.sequence,
            "preparation_fact.journal_position.sequence",
            minimum=1,
        )
        _corrected_require_str(
            fact.journal_position.append_receipt_id,
            "preparation_fact.journal_position.append_receipt_id",
        )
        if fact.fingerprint != action.fingerprint:
            raise SnapshotRejected("foreign_journal", "preparation fact is foreign")
        if fact.slot_id in preparations:
            raise SnapshotRejected("invalid_journal", "preparation fact is duplicated")
        preparations[fact.slot_id] = fact
    return _corrected_resolved_synchronization_requirements(action, preparations)


def _corrected_validate_journal(
    bound: BoundSpecification,
    plan: ProtocolPlan,
    journal: ReconciliationJournal,
) -> tuple[
    dict[str, IntentFact],
    dict[str, PreparationFact],
    dict[tuple[str, str], SynchronizationFact],
    dict[str, CompletionFact],
]:
    if type(journal) is not ReconciliationJournal:
        raise SnapshotRejected("invalid_input_type", "journal type is invalid")
    _validate_retained_inputs(bound, journal.retained_inputs)
    for name in ("intents", "preparations", "synchronizations", "completions"):
        _corrected_require_tuple(getattr(journal, name), f"journal.{name}")
    actions = {action.action_id: action for action in plan.actions}
    intents: dict[str, IntentFact] = {}
    for intent_fact in journal.intents:
        if type(intent_fact) is not IntentFact:
            raise SnapshotRejected("invalid_input_type", "intent fact type is invalid")
        _corrected_require_str(intent_fact.fingerprint, "intent.fingerprint")
        _corrected_require_str(intent_fact.action_id, "intent.action_id")
        _corrected_require_str(intent_fact.receipt_id, "intent.receipt_id")
        if intent_fact.fingerprint != bound.fingerprint or intent_fact.action_id not in actions:
            raise SnapshotRejected("foreign_journal", "intent fact is foreign")
        if intent_fact.action_id in intents:
            raise SnapshotRejected("invalid_journal", "intent fact is duplicated")
        intents[intent_fact.action_id] = intent_fact

    producers = {
        action.prepared_slot_id: action
        for action in plan.actions
        if action.creates_prepared_identity
    }
    expected_parents = {
        slot.slot_id: slot.materialization_parent for slot in bound.spec.preparation_slots
    } | {marker.slot_id: marker.parent for marker in bound.spec.ownership_markers}
    slots_by_id = {slot.slot_id: slot for slot in bound.spec.preparation_slots}
    retained_identities = set(_corrected_collect_baseline_nodes(bound.spec.baseline))
    retained_identities.update(boundary.identity for boundary in bound.spec.boundaries)
    retained_identities.update(
        ancestor.identity for boundary in bound.spec.boundaries for ancestor in boundary.ancestors
    )
    retained_identities.update(
        destination.storage.identity for destination in bound.spec.destination_objects
    )
    retained_identities.update(
        binding.reference.identity
        for binding in _corrected_initial_parent_bindings(bound.spec)
        if type(binding.reference) is ExistingNode
    )
    journal_sequences: dict[int, str] = {}
    journal_receipts: set[str] = set()

    def validate_journal_position(position: object, field_name: str) -> JournalPosition:
        if type(position) is not JournalPosition:
            raise SnapshotRejected("invalid_input_type", f"{field_name} type is invalid")
        journal_id = _corrected_require_str(position.journal_id, f"{field_name}.journal_id")
        sequence = _corrected_require_int(position.sequence, f"{field_name}.sequence", minimum=1)
        append_receipt_id = _corrected_require_str(
            position.append_receipt_id,
            f"{field_name}.append_receipt_id",
        )
        if journal_id != bound.spec.identity.operation_id:
            raise SnapshotRejected("foreign_journal", f"{field_name} journal is foreign")
        if sequence in journal_sequences or append_receipt_id in journal_receipts:
            raise SnapshotRejected("invalid_journal", "journal position is duplicated")
        journal_sequences[sequence] = append_receipt_id
        journal_receipts.add(append_receipt_id)
        return position

    prepared_identities: set[NodeIdentity] = set()
    preparations: dict[str, PreparationFact] = {}
    for preparation_fact in journal.preparations:
        if type(preparation_fact) is not PreparationFact:
            raise SnapshotRejected("invalid_input_type", "preparation fact type is invalid")
        _corrected_require_str(preparation_fact.fingerprint, "preparation_fact.fingerprint")
        _corrected_require_str(preparation_fact.slot_id, "preparation_fact.slot_id")
        _corrected_validate_identity(preparation_fact.identity, "preparation_fact.identity")
        _corrected_require_str(
            preparation_fact.creation_receipt_id,
            "preparation_fact.creation_receipt_id",
        )
        validate_journal_position(
            preparation_fact.journal_position,
            "preparation_fact.journal_position",
        )
        if (
            preparation_fact.fingerprint != bound.fingerprint
            or preparation_fact.slot_id not in producers
        ):
            raise SnapshotRejected("foreign_journal", "preparation fact is foreign")
        if (
            preparation_fact.identity in retained_identities
            or preparation_fact.identity in prepared_identities
        ):
            raise SnapshotRejected(
                "ambiguous_node_identity",
                "prepared identity aliases another retained physical node",
            )
        if preparation_fact.identity.filesystem_id != _corrected_parent_filesystem(
            expected_parents[preparation_fact.slot_id], slots_by_id
        ):
            raise SnapshotRejected(
                "cross_filesystem_transition",
                "prepared identity is on an unexpected filesystem",
            )
        if preparation_fact.slot_id in preparations:
            raise SnapshotRejected("invalid_journal", "preparation fact is duplicated")
        producer = producers[preparation_fact.slot_id]
        if producer.action_id not in intents:
            raise SnapshotRejected("impossible_replay_order", "preparation precedes its intent")
        preparations[preparation_fact.slot_id] = preparation_fact
        prepared_identities.add(preparation_fact.identity)

    requirements_by_action = {
        action.action_id: _corrected_resolved_synchronization_requirements(action, preparations)
        for action in plan.actions
    }
    synchronizations: dict[tuple[str, str], SynchronizationFact] = {}
    for synchronization_fact in journal.synchronizations:
        if type(synchronization_fact) is not SynchronizationFact:
            raise SnapshotRejected("invalid_input_type", "synchronization fact type is invalid")
        _corrected_require_str(synchronization_fact.fingerprint, "synchronization.fingerprint")
        _corrected_require_str(synchronization_fact.action_id, "synchronization.action_id")
        _corrected_require_str(synchronization_fact.requirement, "synchronization.requirement")
        _corrected_require_str(synchronization_fact.receipt_id, "synchronization.receipt_id")
        if synchronization_fact.creation_receipt_id is not None:
            _corrected_require_str(
                synchronization_fact.creation_receipt_id,
                "synchronization.creation_receipt_id",
            )
        synchronization_position = validate_journal_position(
            synchronization_fact.journal_position,
            "synchronization.journal_position",
        )
        action = actions.get(synchronization_fact.action_id)
        if synchronization_fact.fingerprint != bound.fingerprint or action is None:
            raise SnapshotRejected("foreign_journal", "synchronization fact is foreign")
        if synchronization_fact.requirement not in requirements_by_action[action.action_id]:
            raise SnapshotRejected("invalid_journal", "synchronization requirement is unknown")
        key = (synchronization_fact.action_id, synchronization_fact.requirement)
        if key in synchronizations:
            raise SnapshotRejected("invalid_journal", "synchronization fact is duplicated")
        if synchronization_fact.action_id not in intents:
            raise SnapshotRejected("impossible_replay_order", "synchronization precedes intent")
        if action.creates_prepared_identity:
            prepared_slot_id = action.prepared_slot_id
            if prepared_slot_id is None:
                raise SnapshotRejected(
                    "invalid_plan_integrity",
                    "creation action has no preparation slot",
                )
            preparation = preparations.get(prepared_slot_id)
            if (
                preparation is None
                or synchronization_fact.creation_receipt_id != preparation.creation_receipt_id
                or synchronization_position.sequence <= preparation.journal_position.sequence
            ):
                raise SnapshotRejected(
                    "impossible_replay_order",
                    "creation synchronization is not ordered after its post-effect identity",
                )
        elif synchronization_fact.creation_receipt_id is not None:
            raise SnapshotRejected(
                "invalid_journal",
                "non-creation synchronization carries a creation receipt",
            )
        synchronizations[key] = synchronization_fact

    completions: dict[str, CompletionFact] = {}
    preparation_tuple = tuple(preparations.values())
    for completion_fact in journal.completions:
        if type(completion_fact) is not CompletionFact:
            raise SnapshotRejected("invalid_input_type", "completion fact type is invalid")
        _corrected_require_str(completion_fact.fingerprint, "completion.fingerprint")
        _corrected_require_str(completion_fact.action_id, "completion.action_id")
        _corrected_require_str(
            completion_fact.postimage_fingerprint,
            "completion.postimage_fingerprint",
        )
        _corrected_require_str(completion_fact.receipt_id, "completion.receipt_id")
        action = actions.get(completion_fact.action_id)
        if completion_fact.fingerprint != bound.fingerprint or action is None:
            raise SnapshotRejected("foreign_journal", "completion fact is foreign")
        expected_postimage = materialize_expected_observation(
            action.completion_postimage,
            preparation_tuple,
        )
        if completion_fact.postimage_fingerprint != observation_fingerprint(expected_postimage):
            raise SnapshotRejected("invalid_journal", "completion postimage is not exact")
        if completion_fact.action_id in completions:
            raise SnapshotRejected("invalid_journal", "completion fact is duplicated")
        if completion_fact.action_id not in intents:
            raise SnapshotRejected("impossible_replay_order", "completion precedes intent")
        for requirement in requirements_by_action[action.action_id]:
            if (completion_fact.action_id, requirement) not in synchronizations:
                raise SnapshotRejected(
                    "impossible_replay_order",
                    "completion precedes required synchronization",
                )
        completions[completion_fact.action_id] = completion_fact
    return intents, preparations, synchronizations, completions


def replay_reconciliation(
    bound: BoundSpecification,
    journal: ReconciliationJournal,
    fresh: FreshObservation,
    cached_plan: ProtocolPlan | None = None,
) -> ProtocolReplayDecision:
    """Re-derive protocol and classify fresh state without trusting cached observations."""
    if type(bound) is not BoundSpecification or type(fresh) is not FreshObservation:
        raise SnapshotRejected("invalid_input_type", "replay inputs have invalid types")
    _corrected_validate_fresh_observation(bound, fresh)
    rebound = bind_specification(bound.spec)
    if rebound != bound:
        raise SnapshotRejected("specification_fingerprint_mismatch", "bound specification changed")
    plan = _derive_protocol_plan(bound)
    if cached_plan is not None and (type(cached_plan) is not ProtocolPlan or cached_plan != plan):
        raise SnapshotRejected("invalid_plan_integrity", "cached plan differs from derived plan")
    intents, preparations, synchronizations, completions = _corrected_validate_journal(
        bound,
        plan,
        journal,
    )
    action_indexes = {action.action_id: index for index, action in enumerate(plan.actions)}
    completed_indexes = {action_indexes[action_id] for action_id in completions}
    completed_count = 0
    while completed_count in completed_indexes:
        completed_count += 1
    if any(index >= completed_count for index in completed_indexes):
        raise SnapshotRejected("impossible_replay_order", "completion facts are not a prefix")
    allowed_indexes = set(range(completed_count + (completed_count < len(plan.actions))))
    for action_id in intents:
        if action_indexes[action_id] not in allowed_indexes:
            raise SnapshotRejected("impossible_replay_order", "future intent is present")
    for action_id, _ in synchronizations:
        if action_indexes[action_id] not in allowed_indexes:
            raise SnapshotRejected("impossible_replay_order", "future synchronization is present")

    preparation_tuple = tuple(preparations.values())
    if completed_count == len(plan.actions):
        final = materialize_expected_observation(
            plan.actions[-1].completion_postimage,
            preparation_tuple,
        )
        if fresh != final:
            raise SnapshotRejected("foreign_replay_state", "fresh final state is not exact")
        return ProtocolReplayDecision("complete", None)

    action = plan.actions[completed_count]
    preimage = materialize_expected_observation(action.preimage, preparation_tuple)
    intent = intents.get(action.action_id)
    if intent is None:
        if any(
            producer_action.action_id == action.action_id
            for slot_id, producer_action in {
                candidate.prepared_slot_id: candidate
                for candidate in plan.actions
                if candidate.creates_prepared_identity
            }.items()
            if slot_id in preparations
        ):
            raise SnapshotRejected("impossible_replay_order", "preparation exists without intent")
        if fresh != preimage:
            raise SnapshotRejected("foreign_replay_state", "fresh state advanced without intent")
        return ProtocolReplayDecision("record_intent", action)

    current_synchronizations = {
        requirement for action_id, requirement in synchronizations if action_id == action.action_id
    }
    if action.creates_prepared_identity:
        if action.prepared_slot_id in preparations:
            if fresh == preimage:
                raise SnapshotRejected(
                    "lost_preparation_identity",
                    "recorded creation identity is absent at the fresh destination",
                )
        elif fresh != preimage:
            raise SnapshotRejected(
                "lost_preparation_identity",
                "materialized node identity was not durably recorded",
            )
        else:
            return ProtocolReplayDecision("perform_effect", action)
    if not current_synchronizations and fresh == preimage:
        return ProtocolReplayDecision("perform_effect", action)

    postimages = tuple(
        materialize_expected_observation(expected, preparation_tuple)
        for expected in action.permitted_effect_postimages
    )
    if fresh not in postimages:
        if fresh == preimage and not current_synchronizations:
            return ProtocolReplayDecision("perform_effect", action)
        raise SnapshotRejected("foreign_replay_state", "fresh state is not an allowed action image")
    action_requirements = _corrected_resolved_synchronization_requirements(action, preparations)
    missing = tuple(
        requirement
        for requirement in action_requirements
        if requirement not in current_synchronizations
    )
    if missing:
        return ProtocolReplayDecision("retry_synchronization", action, missing)
    completion = materialize_expected_observation(
        action.completion_postimage,
        preparation_tuple,
    )
    if fresh != completion:
        raise SnapshotRejected("foreign_replay_state", "completion postimage is not exact")
    return ProtocolReplayDecision("record_completion", action)
