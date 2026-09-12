"""Raw-byte path identity, snapshot validation, planning and replay behavior
for the replica reconciliation core."""

from __future__ import annotations

from dataclasses import replace

import pytest

from yinshi.services.workspace_replica import (
    IndexEntry,
    ManifestEntry,
    ReconciliationSpec,
    SnapshotRejected,
    canonical_path_base64,
    raw_path_from_base64,
)

INCARNATION = "inc-1"
BINDING = "bind-1"
SELECTED_REF = "refs/heads/main"
HEAD_BINDING = "symbolic:refs/heads/main"
REF_OID = "ref2222222222222222222222222222222222222222"


def _index(
    path: bytes,
    oid: str = "obj-1",
    mode: str = "100644",
    stage: int = 0,
    intent_to_add: bool = False,
    skip_worktree: bool = False,
    assume_unchanged: bool = False,
) -> IndexEntry:
    return IndexEntry(
        raw_path=path,
        stage=stage,
        mode=mode,
        object_id=oid,
        intent_to_add=intent_to_add,
        skip_worktree=skip_worktree,
        assume_unchanged=assume_unchanged,
    )


def _file(
    path: bytes,
    content: bytes = b"x",
    executable: bool = False,
    nlink: int = 1,
    kind: str = "file",
    privilege_bits: bool = False,
    mode: int | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        raw_path=path,
        kind=kind,
        content=content,
        executable=executable if mode is None else bool(mode & 0o111),
        nlink=nlink,
        privilege_bits=privilege_bits,
    )


@pytest.mark.parametrize(
    "raw_path",
    [
        b"plain.txt",
        b"weird\nname\xff",
        b"tab\tseparated.txt",
        b"line one\nline two",
        "caf\u00e9.txt".encode("utf-8"),
        b"\xf0\x9f\x8c\xb2/pine.txt",
        b"trailing space .txt",
    ],
)
def test_canonical_base64_round_trip_preserves_raw_bytes(raw_path: bytes) -> None:
    """Round-tripping raw path bytes through canonical base64 is lossless."""
    encoded = canonical_path_base64(raw_path)
    assert raw_path_from_base64(encoded) == raw_path


def test_canonical_base64_never_normalizes_unicode() -> None:
    """NFC and NFD spellings stay distinct identities end to end."""
    nfc = "caf\u00e9.txt".encode("utf-8")
    nfd = "cafe\u0301.txt".encode("utf-8")
    assert nfc != nfd
    assert canonical_path_base64(nfc) != canonical_path_base64(nfd)
    assert raw_path_from_base64(canonical_path_base64(nfc)) == nfc
    assert raw_path_from_base64(canonical_path_base64(nfd)) == nfd


def test_canonical_base64_uses_standard_alphabet_with_padding() -> None:
    """The encoding is plain RFC 4648 base64 with padding."""
    assert canonical_path_base64(b"hello/world.txt") == "aGVsbG8vd29ybGQudHh0"
    assert canonical_path_base64(b"a") == "YQ=="


@pytest.mark.parametrize(
    "encoded",
    [
        "aGV sbG8=",  # whitespace is never canonical
        "aGV\tsbG8=",  # tabs are never canonical
        "not base64!!",
        "YQ=",  # wrong padding length
        "YQ===",  # excess padding
        "ab==",  # non-zero discarded bits do not re-encode identically
    ],
)
def test_base64_decode_rejects_non_canonical_forms(encoded: str) -> None:
    """Strict decoding refuses anything that is not canonical base64."""
    with pytest.raises(SnapshotRejected) as excinfo:
        raw_path_from_base64(encoded)
    assert excinfo.value.reason_code == "invalid_base64_path"


def test_canonical_base64_rejects_mutable_path_bytes() -> None:
    """Mutable byte containers cannot enter a durable path identity."""
    with pytest.raises(SnapshotRejected) as excinfo:
        canonical_path_base64(bytearray(b"mutable"))  # type: ignore[arg-type]
    assert excinfo.value.reason_code == "invalid_input_type"


@pytest.mark.parametrize("ancestor_kind", ["file", "symlink"])
def test_directory_descendant_under_non_directory_is_refused(ancestor_kind: str) -> None:
    """Every descendant requires each explicit ancestor to be a directory."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    ancestor = _file(b"a", kind=ancestor_kind)
    descendant = _file(b"a/b", content=b"", kind="directory")
    ancestor_node = module.NodeState(
        module.Location("working", ancestor.raw_path),
        module.NodeIdentity("fs-working", "ancestor"),
        ancestor.kind,
        ancestor.content,
        ancestor.executable,
        ancestor.nlink,
        ancestor.privilege_bits,
    )
    descendant_node = module.NodeState(
        module.Location("working", descendant.raw_path),
        module.NodeIdentity("fs-working", "descendant"),
        descendant.kind,
        descendant.content,
        descendant.executable,
        descendant.nlink,
        descendant.privilege_bits,
    )
    baseline = replace(
        base.baseline,
        manifest=(ancestor, descendant),
        working_nodes=(ancestor_node, descendant_node),
    )
    desired = replace(
        base.desired,
        manifest=(ancestor, descendant),
        working_nodes=tuple(
            module.DesiredNodeState(
                node.location,
                module.ExistingNode(node.identity),
                node.kind,
                node.content,
                node.executable,
                node.nlink,
                node.privilege_bits,
            )
            for node in (ancestor_node, descendant_node)
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(base, baseline=baseline, desired=desired))
    assert excinfo.value.reason_code == "incomplete_topology"


def test_binding_ports_raw_path_index_and_node_semantics() -> None:
    """Corrected binding preserves required raw-path, index, link, and node refusals."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    invalid_specs = (
        replace(
            base,
            baseline=replace(
                base.baseline,
                index=replace(base.baseline.index, entries=(_index(b"../escape", oid="1" * 40),)),
            ),
        ),
        replace(
            base,
            baseline=replace(
                base.baseline,
                index=replace(
                    base.baseline.index, entries=(_index(b"keep.txt", oid="1" * 40, mode="160000"),)
                ),
            ),
        ),
        replace(
            base,
            baseline=replace(
                base.baseline,
                index=replace(
                    base.baseline.index,
                    entries=(_index(b"keep.txt", oid="1" * 40, skip_worktree=True),),
                ),
            ),
        ),
        replace(
            base,
            baseline=replace(base.baseline, index=replace(base.baseline.index, sparse_index=True)),
        ),
        replace(
            base,
            baseline=replace(
                base.baseline,
                index=replace(base.baseline.index, entries=base.baseline.index.entries * 2),
            ),
        ),
        replace(base, baseline=replace(base.baseline, manifest=(_file(b"../escape"),))),
        replace(base, baseline=replace(base.baseline, manifest=base.baseline.manifest * 2)),
        replace(
            base,
            baseline=replace(
                base.baseline,
                manifest=(_file(b"keep.txt", nlink=2),),
                working_nodes=(replace(base.baseline.working_nodes[0], nlink=2),),
            ),
        ),
        replace(
            base,
            baseline=replace(base.baseline, manifest=(_file(b"keep.txt", privilege_bits=True),)),
        ),
        replace(base, baseline=replace(base.baseline, manifest=(_file(b"keep.txt", kind="fifo"),))),
        replace(
            base,
            baseline=replace(
                base.baseline, manifest=(_file(b"dir", content=b"x", kind="directory"),)
            ),
        ),
    )
    for candidate in invalid_specs:
        with pytest.raises(SnapshotRejected):
            module.bind_specification(candidate)

    symlink_entry = _file(b"link", content=b"../../opaque-target", kind="symlink")
    symlink_node = module.NodeState(
        module.Location("working", b"link"),
        module.NodeIdentity("fs-working", "link"),
        "symlink",
        b"../../opaque-target",
        False,
        1,
        False,
    )
    directory_entry = _file(b"dir", content=b"", kind="directory", nlink=2)
    child_entry = _file(b"dir/child", content=b"child")
    directory_node = module.NodeState(
        module.Location("working", b"dir"),
        module.NodeIdentity("fs-working", "dir"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    child_node = module.NodeState(
        module.Location("working", b"dir/child"),
        module.NodeIdentity("fs-working", "child"),
        "file",
        b"child",
        False,
        1,
        False,
    )
    for manifest, nodes in (
        ((symlink_entry,), (symlink_node,)),
        ((directory_entry, child_entry), (directory_node, child_node)),
    ):
        desired_nodes = tuple(
            module.DesiredNodeState(
                node.location,
                module.ExistingNode(node.identity),
                node.kind,
                node.content,
                node.executable,
                node.nlink,
                node.privilege_bits,
            )
            for node in nodes
        )
        candidate = replace(
            base,
            baseline=replace(base.baseline, manifest=manifest, working_nodes=nodes),
            desired=replace(base.desired, manifest=manifest, working_nodes=desired_nodes),
        )
        assert module.bind_specification(candidate).spec.baseline.manifest == tuple(
            sorted(manifest, key=lambda entry: entry.raw_path)
        )


@pytest.mark.parametrize("target", ["index", "manifest"])
@pytest.mark.parametrize(
    "raw_path",
    [
        b"",
        b"/etc/passwd",
        b"a/../b.txt",
        b"..",
        b".git/config",
        b"dir/.git/HEAD",
        b".git",
        b"a\x00b",
        b"a//b",
        b"a/./b",
        b"a/",
        b"./a",
    ],
)
def test_binding_refuses_every_unsafe_raw_path(target: str, raw_path: bytes) -> None:
    """Corrected binding rejects every unsafe component for index and working paths."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    if target == "index":
        baseline = replace(
            base.baseline,
            index=replace(
                base.baseline.index,
                entries=(_index(raw_path, oid="1" * 40),),
            ),
        )
    else:
        entry = _file(raw_path)
        node = module.NodeState(
            module.Location("working", raw_path),
            module.NodeIdentity("fs-working", "unsafe-node"),
            "file",
            b"x",
            False,
            1,
            False,
        )
        baseline = replace(base.baseline, manifest=(entry,), working_nodes=(node,))
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(base, baseline=baseline))
    assert excinfo.value.reason_code == "unsafe_path"


@pytest.mark.parametrize(
    "label,entries,sparse,reason_code",
    [
        (
            "stage-out-of-range",
            (_index(b"staged", oid="1" * 40, stage=4),),
            False,
            "invalid_index_stage",
        ),
        (
            "negative-stage",
            (_index(b"staged", oid="1" * 40, stage=-1),),
            False,
            "invalid_index_stage",
        ),
        ("sparse", (_index(b"keep.txt", oid="1" * 40),), True, "sparse_index_unsupported"),
        (
            "gitlink",
            (_index(b"sub", oid="1" * 40, mode="160000"),),
            False,
            "unsupported_index_mode",
        ),
        (
            "group-write",
            (_index(b"mode", oid="1" * 40, mode="100664"),),
            False,
            "unsupported_index_mode",
        ),
        ("tree", (_index(b"tree", oid="1" * 40, mode="40000"),), False, "unsupported_index_mode"),
        (
            "malformed-mode",
            (_index(b"bad", oid="1" * 40, mode="120000x"),),
            False,
            "unsupported_index_mode",
        ),
        (
            "duplicate",
            (_index(b"dup", oid="1" * 40), _index(b"dup", oid="2" * 40)),
            False,
            "duplicate_index_entry",
        ),
        (
            "duplicate-conflict-stage",
            (
                _index(b"merge", oid="1" * 40, stage=2),
                _index(b"merge", oid="1" * 40, stage=2, mode="100755"),
            ),
            False,
            "duplicate_index_entry",
        ),
        (
            "mixed-stage-zero-and-conflict",
            (
                _index(b"merge", oid="1" * 40),
                _index(b"merge", oid="1" * 40, stage=1),
            ),
            False,
            "unmerged_index",
        ),
    ],
)
def test_binding_refuses_each_unsupported_index_state(
    label: str,
    entries: tuple[IndexEntry, ...],
    sparse: bool,
    reason_code: str,
) -> None:
    """Corrected binding preserves exact refusals for every unsupported index state."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    candidate = replace(
        base,
        baseline=replace(
            base.baseline,
            index=replace(base.baseline.index, entries=entries, sparse_index=sparse),
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == reason_code, label


def _conflict_spec(entries: tuple[IndexEntry, ...]) -> ReconciliationSpec:
    """Build one corrected specification with replaced index entries."""
    base = _corrected_spec()
    assert isinstance(base, ReconciliationSpec)
    return replace(
        base,
        baseline=replace(
            base.baseline,
            index=replace(base.baseline.index, entries=entries),
        ),
        desired=replace(
            base.desired,
            index=replace(base.desired.index, entries=entries),
        ),
    )


def test_binding_accepts_unique_conflict_stages_sharing_one_path() -> None:
    """One path can contain unique conflict stages instead of stage zero."""
    import yinshi.services.workspace_replica as module

    keep = _index(b"keep.txt", oid="1" * 40)
    conflicts = (
        _index(b"merge", oid="1" * 40, stage=1),
        _index(b"merge", oid="1" * 40, stage=2),
        _index(b"merge", oid="1" * 40, stage=3),
    )
    bound = module.bind_specification(
        _conflict_spec((conflicts[1], keep, conflicts[0], conflicts[2]))
    )
    assert bound.spec.baseline.index.entries == (keep, *conflicts)
    assert bound.spec.desired.index.entries == (keep, *conflicts)


def test_binding_rejects_boolean_index_stage() -> None:
    """A Boolean cannot enter the numeric index-stage domain."""
    import yinshi.services.workspace_replica as module

    entry = replace(_index(b"stage", oid="1" * 40), stage=True)
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(_conflict_spec((entry,)))
    assert excinfo.value.reason_code == "invalid_input_type"


@pytest.mark.parametrize("flag", ["intent_to_add", "skip_worktree", "assume_unchanged"])
def test_binding_preserves_supported_index_flags(flag: str) -> None:
    """Binding accepts each modeled index flag without changing it."""
    import yinshi.services.workspace_replica as module

    entry = replace(_index(b"flagged", oid="1" * 40), **{flag: True})
    bound = module.bind_specification(_conflict_spec((entry,)))
    assert getattr(bound.spec.baseline.index.entries[0], flag) is True
    assert getattr(bound.spec.desired.index.entries[0], flag) is True


def test_conflict_entry_order_does_not_change_bound_fingerprints() -> None:
    """Conflict entries bind to one fingerprint in any input order."""
    import yinshi.services.workspace_replica as module

    conflicts = (
        _index(b"merge", oid="1" * 40, stage=1),
        _index(b"merge", oid="1" * 40, stage=2),
        _index(b"merge", oid="1" * 40, stage=3),
    )
    forward = module.bind_specification(_conflict_spec(conflicts))
    backward = module.bind_specification(_conflict_spec(tuple(reversed(conflicts))))
    assert forward.fingerprint == backward.fingerprint
    assert forward.baseline_fingerprint == backward.baseline_fingerprint
    assert forward.desired_fingerprint == backward.desired_fingerprint


def test_unchanged_index_storage_accepts_equivalent_entry_order() -> None:
    """An order-only semantic difference does not require an index transition."""
    import yinshi.services.workspace_replica as module

    conflicts = (
        _index(b"merge", oid="1" * 40, stage=1),
        _index(b"merge", oid="1" * 40, stage=2),
        _index(b"merge", oid="1" * 40, stage=3),
    )
    candidate = _conflict_spec(conflicts)
    candidate = replace(
        candidate,
        desired=replace(
            candidate.desired,
            index=replace(candidate.desired.index, entries=tuple(reversed(conflicts))),
        ),
    )
    bound = module.bind_specification(candidate)
    assert bound.spec.baseline.index.entries == conflicts
    assert bound.spec.desired.index.entries == conflicts


@pytest.mark.parametrize("field", ["intent_to_add", "skip_worktree", "assume_unchanged"])
def test_stage_and_flags_stay_in_canonical_index_fingerprints(field: str) -> None:
    """Stage and each modeled flag distinguish canonical fingerprints."""
    import yinshi.services.workspace_replica as module

    stage_zero = _index(b"merge", oid="1" * 40)
    changed_flag = replace(stage_zero, **{field: True})
    domain = b"yinshi.workspace-replica.test\x00"
    assert module._protocol_fingerprint(domain, stage_zero) != module._protocol_fingerprint(
        domain, replace(stage_zero, stage=1)
    )
    assert module._protocol_fingerprint(domain, stage_zero) != module._protocol_fingerprint(
        domain, changed_flag
    )
    bound_zero = module.bind_specification(_conflict_spec((stage_zero,)))
    bound_stage = module.bind_specification(_conflict_spec((replace(stage_zero, stage=1),)))
    bound_flag = module.bind_specification(_conflict_spec((changed_flag,)))
    assert bound_zero.fingerprint != bound_stage.fingerprint
    assert bound_zero.baseline_fingerprint != bound_flag.baseline_fingerprint
    assert bound_zero.desired_fingerprint != bound_flag.desired_fingerprint
    assert bound_zero.fingerprint != bound_flag.fingerprint


def test_conflict_entries_preserve_exact_index_storage_bytes() -> None:
    """Binding does not rewrite index storage bytes for conflict entries."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    assert isinstance(base, ReconciliationSpec)
    exact_bytes = b"\x00\xffindex\x00bytes-not-derived-from-entries"
    index_node = replace(base.baseline.index.storage, content=exact_bytes)
    desired_index_node = module.DesiredNodeState(
        index_node.location,
        module.ExistingNode(index_node.identity),
        index_node.kind,
        exact_bytes,
        index_node.executable,
        index_node.nlink,
        index_node.privilege_bits,
    )
    conflicts = (
        _index(b"merge", oid="1" * 40, stage=1),
        _index(b"merge", oid="1" * 40, stage=2),
    )
    candidate = replace(
        base,
        baseline=replace(
            base.baseline,
            index=replace(
                base.baseline.index,
                storage=index_node,
                entries=(base.baseline.index.entries[0], *conflicts),
            ),
        ),
        desired=replace(
            base.desired,
            index=replace(
                base.desired.index,
                storage=desired_index_node,
                entries=(base.desired.index.entries[0], *conflicts),
            ),
        ),
    )
    bound = module.bind_specification(candidate)
    assert bound.spec.baseline.index.storage == index_node
    assert bound.spec.desired.index.storage == desired_index_node
    assert bound.spec.baseline.index.entries == (base.baseline.index.entries[0], *conflicts)
    assert bound.spec.desired.index.entries == (base.desired.index.entries[0], *conflicts)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_binding_refuses_shared_non_directory_node_identity(kind: str) -> None:
    """Files and symlinks with multiple links cannot enter replacement planning."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    content = b"bytes" if kind == "file" else b"../opaque"
    entry = _file(b"shared", content=content, kind=kind, nlink=2)
    node = module.NodeState(
        module.Location("working", b"shared"),
        module.NodeIdentity("fs-working", "shared"),
        kind,
        content,
        False,
        2,
        False,
    )
    desired_node = module.DesiredNodeState(
        node.location,
        module.ExistingNode(node.identity),
        kind,
        content,
        False,
        2,
        False,
    )
    candidate = replace(
        base,
        baseline=replace(base.baseline, manifest=(entry,), working_nodes=(node,)),
        desired=replace(base.desired, manifest=(entry,), working_nodes=(desired_node,)),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "unsupported_hardlink"


def test_binding_preserves_precise_manifest_semantics_and_valid_directory_states() -> None:
    """Manifest validation rejects each unsafe semantic while keeping valid opaque nodes."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()

    def node(entry: ManifestEntry, node_id: str) -> object:
        return module.NodeState(
            module.Location("working", entry.raw_path),
            module.NodeIdentity("fs-working", node_id),
            entry.kind,
            entry.content,
            entry.executable,
            entry.nlink,
            entry.privilege_bits,
        )

    duplicate_one = _file(b"duplicate", b"one")
    duplicate_two = _file(b"duplicate", b"two")
    invalid = (
        (
            "duplicate",
            (duplicate_one, duplicate_two),
            (node(duplicate_one, "duplicate-one"), node(duplicate_two, "duplicate-two")),
            "duplicate_manifest_entry",
        ),
        (
            "privilege",
            (_file(b"privilege", privilege_bits=True),),
            (node(_file(b"privilege", privilege_bits=True), "privilege"),),
            "unsupported_privilege_bits",
        ),
        (
            "fifo",
            (_file(b"special", kind="fifo"),),
            (node(_file(b"special", kind="fifo"), "fifo"),),
            "unsupported_kind",
        ),
        (
            "socket",
            (_file(b"special", kind="socket"),),
            (node(_file(b"special", kind="socket"), "socket"),),
            "unsupported_kind",
        ),
        (
            "device",
            (_file(b"special", kind="device"),),
            (node(_file(b"special", kind="device"), "device"),),
            "unsupported_kind",
        ),
        (
            "directory-content",
            (_file(b"directory", content=b"junk", kind="directory"),),
            (node(_file(b"directory", content=b"junk", kind="directory"), "directory"),),
            "invalid_manifest",
        ),
    )
    for label, manifest, nodes, reason_code in invalid:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.bind_specification(
                replace(
                    base,
                    baseline=replace(base.baseline, manifest=manifest, working_nodes=nodes),
                )
            )
        assert excinfo.value.reason_code == reason_code, label

    empty_directory = _file(b"empty", content=b"", kind="directory", nlink=3)
    opaque_symlink = _file(b"link", content=b"../../etc/passwd", kind="symlink")
    for entry in (empty_directory, opaque_symlink):
        concrete = node(entry, entry.kind)
        desired = module.DesiredNodeState(
            concrete.location,
            module.ExistingNode(concrete.identity),
            concrete.kind,
            concrete.content,
            concrete.executable,
            concrete.nlink,
            concrete.privilege_bits,
        )
        candidate = replace(
            base,
            baseline=replace(base.baseline, manifest=(entry,), working_nodes=(concrete,)),
            desired=replace(base.desired, manifest=(entry,), working_nodes=(desired,)),
        )
        assert module.bind_specification(candidate).spec.baseline.manifest == (entry,)


def test_desired_node_reference_never_fabricates_future_node_identity() -> None:
    """Desired nodes reference existing identities or preparation slots only."""
    import yinshi.services.workspace_replica as module

    existing = module.ExistingNode(module.NodeIdentity("fs-1", "node-1"))
    prepared = module.PreparedNode("slot-1")

    assert existing.identity == module.NodeIdentity("fs-1", "node-1")
    assert prepared.slot_id == "slot-1"
    assert not hasattr(prepared, "identity")


def _corrected_spec(**identity_overrides: object) -> object:
    """Build one complete bound specification for corrected-contract tests."""
    import hashlib

    import yinshi.services.workspace_replica as module

    def location(namespace: str, raw_path: bytes) -> object:
        return module.Location(namespace, raw_path)

    def identity(filesystem_id: str, node_id: str) -> object:
        return module.NodeIdentity(filesystem_id, node_id)

    def parent(
        namespace: str,
        path: bytes,
        filesystem_id: str,
        node_id: str,
    ) -> object:
        return module.ParentBinding(
            location(namespace, path),
            module.ExistingNode(identity(filesystem_id, node_id)),
        )

    def node(
        namespace: str, path: bytes, node_id: str, content: bytes, kind: str = "file"
    ) -> object:
        return module.NodeState(
            location(namespace, path),
            identity(f"fs-{namespace}", node_id),
            kind,
            content,
            False,
            1,
            False,
        )

    index_entry = _index(b"keep.txt", oid="1" * 40)
    manifest_entry = _file(b"keep.txt", b"same")
    working_node = node("working", b"keep.txt", "working-keep", b"same")
    index_node = node("admin", b"index", "index", b"index-bytes")
    head_node = node("admin", b"HEAD", "head", HEAD_BINDING.encode("ascii"))
    loose_node = node("admin", SELECTED_REF.encode("ascii"), "loose", ("2" * 40).encode("ascii"))
    reflog_node = node("admin", b"logs/refs/heads/main", "reflog", b"old-log")
    unrelated_node = node("admin", b"ORIG_HEAD", "orig-head", b"original")
    packed_absent = module.AbsentState(location("admin", b"packed-refs"))
    baseline_refs = module.RefState(
        SELECTED_REF.encode("ascii"),
        HEAD_BINDING.encode("ascii"),
        "2" * 40,
        None,
        head_node,
        loose_node,
        packed_absent,
        (reflog_node,),
        (unrelated_node,),
    )
    baseline = module.ReplicaState(
        7,
        3,
        module.IndexState(index_node, (index_entry,), False),
        baseline_refs,
        (manifest_entry,),
        (working_node,),
    )

    def desired_node(concrete: object) -> object:
        return module.DesiredNodeState(
            concrete.location,
            module.ExistingNode(concrete.identity),
            concrete.kind,
            concrete.content,
            concrete.executable,
            concrete.nlink,
            concrete.privilege_bits,
        )

    desired = module.DesiredReplicaState(
        8,
        3,
        module.DesiredIndexState(desired_node(index_node), (index_entry,), False),
        module.DesiredRefState(
            SELECTED_REF.encode("ascii"),
            HEAD_BINDING.encode("ascii"),
            "2" * 40,
            None,
            desired_node(head_node),
            desired_node(loose_node),
            packed_absent,
            (desired_node(reflog_node),),
            (desired_node(unrelated_node),),
        ),
        (manifest_entry,),
        (desired_node(working_node),),
    )
    operation_values = {
        "incarnation": INCARNATION,
        "binding_id": BINDING,
        "selected_authority": "execution-owner-1",
        "operation_id": "operation-1",
        "reservation_id": "reservation-1",
        "destination_binding": "destination-1",
        "physical_target_id": "target-1",
        "generation": 7,
        "replica_generation": 3,
        "acknowledgment_receipt_id": "ack-1",
        "drain_receipt_id": "drain-1",
        "export_receipt_id": "export-1",
        "object_format": "sha1",
        "inventory_receipt_id": "inventory-1",
    }
    operation_values.update(identity_overrides)
    operation = module.OperationIdentity(**operation_values)
    boundaries = tuple(
        module.Boundary(
            f"{role}-boundary",
            role,
            location(role if role != "root" else "working", f"{role}-root".encode("ascii")),
            identity(f"fs-{role if role != 'root' else 'working'}", f"{role}-root"),
            (),
        )
        for role in ("root", "admin", "registration", "excluded", "private")
    )
    object_specs = (
        ("1" * 40, "blob", b"blob-storage", "blob-source"),
        ("2" * 40, "commit", b"commit-storage", "commit-source"),
    )
    preparation_slots = tuple(
        module.PreparationSlot(
            slot_id,
            location("private", f"prepare/{slot_id}".encode("ascii")),
            parent("private", b"prepare", "fs-admin", "prepare-parent"),
            parent(
                "admin",
                f"objects/{object_id[:2]}".encode("ascii"),
                "fs-admin",
                f"object-parent-{object_id[:2]}",
            ),
            module.AbsentState(location("private", f"prepare/{slot_id}".encode("ascii"))),
            "file",
            storage_bytes,
            False,
        )
        for object_id, _, storage_bytes, slot_id in object_specs
    )
    required_objects = tuple(
        module.RequiredObject(
            object_id,
            kind,
            len(storage_bytes),
            "sha256:" + hashlib.sha256(storage_bytes).hexdigest(),
            slot_id,
        )
        for object_id, kind, storage_bytes, slot_id in object_specs
    )
    destination_objects = tuple(
        module.DestinationObject(
            object_id,
            node(
                "admin",
                f"objects/{object_id[:2]}/{object_id[2:]}".encode("ascii"),
                f"object-{index}",
                storage_bytes,
            ),
            parent(
                "admin",
                f"objects/{object_id[:2]}".encode("ascii"),
                "fs-admin",
                f"object-parent-{object_id[:2]}",
            ),
            "sha256:" + hashlib.sha256(storage_bytes).hexdigest(),
            f"object-sync-{index}",
            f"object-parent-sync-{index}",
        )
        for index, (object_id, _, storage_bytes, _) in enumerate(object_specs)
    )
    marker_location = location("private", b"markers/operation-1")
    marker = module.OwnershipMarkerSpec(
        "ownership-marker",
        marker_location,
        parent("private", b"markers", "fs-private", "marker-parent"),
        module.AbsentState(marker_location),
        b"yinshi-owner-v1",
        False,
    )
    return module.ReconciliationSpec(
        1,
        operation,
        baseline,
        desired,
        boundaries,
        preparation_slots,
        (),
        (marker,),
        required_objects,
        destination_objects,
        module.InventoryLimits(256, 16, 65536, 16, 128),
    )


def test_bound_specification_fingerprint_changes_with_operation_identity() -> None:
    """A versioned fingerprint binds the complete immutable specification."""
    import yinshi.services.workspace_replica as module

    first = module.bind_specification(_corrected_spec())
    changed = module.bind_specification(_corrected_spec(destination_binding="destination-2"))

    assert first.fingerprint.startswith("sha256:")
    assert len(first.fingerprint) == 71
    assert first.fingerprint != changed.fingerprint
    assert first.baseline_fingerprint != first.desired_fingerprint


def test_bound_specification_rejects_boolean_generation() -> None:
    """Boolean values cannot pass as exact integer generations."""
    import yinshi.services.workspace_replica as module

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(_corrected_spec(generation=True))
    assert excinfo.value.reason_code == "invalid_input_type"


def test_binding_fails_closed_for_wrong_desired_dataclass() -> None:
    """A foreign desired object rejects cleanly instead of raising AttributeError."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(spec, desired=spec.limits))
    assert excinfo.value.reason_code == "invalid_input_type"


def test_binding_rejects_subclassed_desired_node() -> None:
    """Subclassed protocol dataclasses can never stand in for exact types."""
    import yinshi.services.workspace_replica as module

    class ForeignDesiredNodeState(module.DesiredNodeState):
        pass

    spec = _corrected_spec()
    node = spec.desired.working_nodes[0]
    alien = ForeignDesiredNodeState(
        node.location,
        node.reference,
        node.kind,
        node.content,
        node.executable,
        node.nlink,
        node.privilege_bits,
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(spec, desired=replace(spec.desired, working_nodes=(alien,)))
        )
    assert excinfo.value.reason_code == "invalid_input_type"


def test_binding_fails_closed_for_unhashable_node_kinds() -> None:
    """Unhashable kinds reject with invalid_input_type before membership tests."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    baseline_node = spec.baseline.working_nodes[0]
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                spec,
                baseline=replace(
                    spec.baseline,
                    working_nodes=(replace(baseline_node, kind=["file"]),),
                ),
            )
        )
    assert excinfo.value.reason_code == "invalid_input_type"

    manifest_entry = replace(spec.baseline.manifest[0], kind=["file"])
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(spec, baseline=replace(spec.baseline, manifest=(manifest_entry,)))
        )
    assert excinfo.value.reason_code == "invalid_input_type"

    desired_head = replace(spec.desired.refs.head, kind=["file"])
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                spec,
                desired=replace(
                    spec.desired,
                    refs=replace(spec.desired.refs, head=desired_head),
                ),
            )
        )
    assert excinfo.value.reason_code == "invalid_input_type"


@pytest.mark.parametrize(
    "mutation",
    [
        "namespace",
        "object_format",
        "role",
        "desired_kind",
        "object_kind",
        "source_slot_id",
    ],
)
def test_binding_fails_closed_for_unhashable_scalar_fields(mutation: str) -> None:
    """Unhashable scalar fields reject before set membership tests run."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    if mutation == "namespace":
        node = spec.baseline.working_nodes[0]
        broken_location = replace(node.location, namespace=["working"])
        candidate = replace(
            spec,
            baseline=replace(
                spec.baseline,
                working_nodes=(replace(node, location=broken_location),),
            ),
        )
    elif mutation == "object_format":
        candidate = _corrected_spec(object_format=["sha1"])
    elif mutation == "role":
        boundary = replace(spec.boundaries[0], role=["root"])
        candidate = replace(spec, boundaries=(boundary,) + spec.boundaries[1:])
    elif mutation == "desired_kind":
        slot = replace(spec.preparation_slots[0], desired_kind=["file"])
        candidate = replace(spec, preparation_slots=(slot,) + spec.preparation_slots[1:])
    elif mutation == "object_kind":
        required = replace(spec.required_objects[0], kind=["blob"])
        candidate = replace(spec, required_objects=(required,) + spec.required_objects[1:])
    else:
        required = replace(spec.required_objects[0], source_slot_id=["blob-source"])
        candidate = replace(spec, required_objects=(required,) + spec.required_objects[1:])
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_input_type"


def test_binding_rejects_executable_desired_symlink() -> None:
    """Desired executable symlinks reject during exact node validation."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    slot_destination = module.Location("private", b"prepare-working/link-new")
    slot = module.PreparationSlot(
        "link-new",
        slot_destination,
        module.ParentBinding(
            module.Location("private", b"prepare-working"),
            module.ExistingNode(module.NodeIdentity("fs-working", "prepare-working-parent")),
        ),
        module.ParentBinding(
            module.Location("working", b"root-root"),
            module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
        ),
        module.AbsentState(slot_destination),
        "symlink",
        b"../../opaque-target",
        True,
    )
    link_node = module.DesiredNodeState(
        module.Location("working", b"link"),
        module.PreparedNode("link-new"),
        "symlink",
        b"../../opaque-target",
        True,
        1,
        False,
    )
    link_entry = _file(b"link", b"../../opaque-target", kind="symlink", executable=True)
    candidate = replace(
        spec,
        preparation_slots=spec.preparation_slots + (slot,),
        desired=replace(
            spec.desired,
            manifest=spec.desired.manifest + (link_entry,),
            working_nodes=(spec.desired.working_nodes[0], link_node),
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_node_state"


def test_binding_rejects_executable_preparation_symlink_slot() -> None:
    """A preparation slot cannot promise an executable symlink payload."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    slot_destination = module.Location("private", b"prepare/loose-exec")
    slot = module.PreparationSlot(
        "loose-exec",
        slot_destination,
        module.ParentBinding(
            module.Location("private", b"prepare"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "prepare-parent")),
        ),
        module.ParentBinding(
            module.Location("admin", b"refs/heads"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "refs-heads-parent")),
        ),
        module.AbsentState(slot_destination),
        "symlink",
        ("3" * 40).encode("ascii"),
        True,
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(spec, preparation_slots=spec.preparation_slots + (slot,)))
    assert excinfo.value.reason_code == "invalid_node_state"


def test_binding_rejects_privileged_desired_admin_node() -> None:
    """Desired privilege bits reject with unsupported_privilege_bits directly."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    baseline_reflog = spec.baseline.refs.reflogs[0]
    slot_destination = module.Location("private", b"prepare/reflog-new")
    slot = module.PreparationSlot(
        "reflog-new",
        slot_destination,
        module.ParentBinding(
            module.Location("private", b"prepare"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "prepare-parent")),
        ),
        module.ParentBinding(
            module.Location("admin", b"logs/refs/heads"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "reflogs-heads-parent")),
        ),
        module.AbsentState(slot_destination),
        "file",
        b"new-log",
        False,
    )
    privileged_reflog = module.DesiredNodeState(
        baseline_reflog.location,
        module.PreparedNode("reflog-new"),
        "file",
        b"new-log",
        False,
        1,
        True,
    )
    candidate = replace(
        spec,
        preparation_slots=spec.preparation_slots + (slot,),
        desired=replace(
            spec.desired,
            refs=replace(spec.desired.refs, reflogs=(privileged_reflog,)),
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "unsupported_privilege_bits"


def test_binding_rejects_preparation_slot_directory_with_content() -> None:
    """Preparation slots cannot promise bytes for a desired directory."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    baseline_reflog = spec.baseline.refs.reflogs[0]
    slot_destination = module.Location("private", b"prepare/reflog-dir")
    slot = module.PreparationSlot(
        "reflog-dir",
        slot_destination,
        module.ParentBinding(
            module.Location("private", b"prepare"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "prepare-parent")),
        ),
        module.ParentBinding(
            module.Location("admin", b"logs/refs/heads"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "reflogs-heads-parent")),
        ),
        module.AbsentState(slot_destination),
        "directory",
        b"junk",
        False,
    )
    directory_reflog = module.DesiredNodeState(
        baseline_reflog.location,
        module.PreparedNode("reflog-dir"),
        "directory",
        b"junk",
        False,
        1,
        False,
    )
    candidate = replace(
        spec,
        preparation_slots=spec.preparation_slots + (slot,),
        desired=replace(
            spec.desired,
            refs=replace(spec.desired.refs, reflogs=(directory_reflog,)),
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_node_state"


def test_binding_fails_closed_for_cyclic_foreign_dataclass() -> None:
    """Cyclic foreign dataclasses reject instead of exhausting the stack."""
    from dataclasses import dataclass as dataclass_component

    import yinshi.services.workspace_replica as module

    @dataclass_component
    class CyclicForeign:
        loop: object = None

    cyclic = CyclicForeign()
    cyclic.loop = cyclic
    spec = _corrected_spec()
    baseline_node = replace(spec.baseline.working_nodes[0], privilege_bits=cyclic)
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(spec, baseline=replace(spec.baseline, working_nodes=(baseline_node,)))
        )
    assert excinfo.value.reason_code == "invalid_input_type"


def _corrected_initial_observation(bound: object) -> object:
    """Build the complete fresh initial observation bound by the fixture."""
    import yinshi.services.workspace_replica as module

    spec = bound.spec
    storage = tuple(slot.initial for slot in spec.preparation_slots)
    storage += tuple(marker.initial for marker in spec.ownership_markers)
    storage += tuple(slot.initial for slot in spec.quarantine_slots)
    storage += tuple(destination.storage for destination in spec.destination_objects)
    destination_ids = {destination.object_id for destination in spec.destination_objects}
    storage += tuple(
        module.AbsentState(
            module.Location(
                "admin",
                f"objects/{item.object_id[:2]}/{item.object_id[2:]}".encode("ascii"),
            )
        )
        for item in spec.required_objects
        if item.object_id not in destination_ids
    )
    parents = tuple(
        module.ParentObservation(binding.location, binding.reference.identity)
        for binding in module._corrected_initial_parent_bindings(spec)
        if isinstance(binding.reference, module.ExistingNode)
    )
    return module.FreshObservation(
        spec.identity,
        spec.baseline,
        spec.boundaries,
        parents,
        storage,
        module.ControlState(
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


def test_initial_observation_requires_every_no_replace_destination_absent() -> None:
    """Fresh planning observes absence at quarantine, marker, and preparation destinations."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    retained = _corrected_retained_inputs(bound)
    complete = _corrected_initial_observation(bound)
    quarantine_locations = {slot.destination for slot in bound.spec.quarantine_slots}
    incomplete = replace(
        complete,
        storage=tuple(
            state for state in complete.storage if state.location not in quarantine_locations
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, incomplete, retained)
    assert excinfo.value.reason_code == "baseline_conflict"

    assert module.plan_reconciliation(bound, complete, retained).actions

    marker = bound.spec.ownership_markers[0]
    occupied_marker = module.NodeState(
        marker.destination,
        module.NodeIdentity(marker.parent.reference.identity.filesystem_id, "foreign-marker"),
        "file",
        b"foreign",
        False,
        1,
        False,
    )
    occupied_storage = tuple(
        occupied_marker if state.location == marker.destination else state
        for state in complete.storage
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            bound,
            replace(complete, storage=occupied_storage),
            retained,
        )
    assert excinfo.value.reason_code == "baseline_conflict"

    duplicate = replace(complete, storage=complete.storage + (marker.initial,))
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, duplicate, retained)
    assert excinfo.value.reason_code == "duplicate_location"

    foreign_initial = module.NodeState(
        marker.destination,
        module.NodeIdentity(marker.parent.reference.identity.filesystem_id, "foreign-initial"),
        "file",
        b"foreign",
        False,
        1,
        False,
    )
    foreign_marker = replace(marker, initial=foreign_initial)
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(bound.spec, ownership_markers=(foreign_marker,)))
    assert excinfo.value.reason_code == "invalid_specification"

    preparation = bound.spec.preparation_slots[0]
    occupied_preparation = replace(
        preparation,
        initial=module.NodeState(
            preparation.destination,
            module.NodeIdentity(
                preparation.materialization_parent.reference.identity.filesystem_id,
                "foreign-preparation",
            ),
            "file",
            preparation.desired_content,
            preparation.executable,
            1,
            False,
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                bound.spec,
                preparation_slots=(
                    occupied_preparation,
                    *bound.spec.preparation_slots[1:],
                ),
            )
        )
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_mutation_of_a_protected_boundary_ancestor() -> None:
    """A protected ancestor cannot also be a node moved by reconciliation."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    protected_ancestor = replace(
        candidate.baseline.working_nodes[0],
        kind="directory",
        content=b"",
        nlink=2,
    )
    changed_manifest = replace(
        candidate.baseline.manifest[0],
        kind="directory",
        content=b"",
        nlink=2,
    )
    working_slot_index = next(
        index
        for index, slot in enumerate(candidate.preparation_slots)
        if slot.slot_id == "working-new"
    )
    working_slots = list(candidate.preparation_slots)
    working_slots[working_slot_index] = replace(
        working_slots[working_slot_index],
        desired_kind="directory",
        desired_content=b"",
    )
    candidate = replace(
        candidate,
        baseline=replace(
            candidate.baseline,
            manifest=(changed_manifest,),
            working_nodes=(protected_ancestor,),
        ),
        desired=replace(
            candidate.desired,
            manifest=(changed_manifest,),
            working_nodes=(
                replace(
                    candidate.desired.working_nodes[0],
                    kind="directory",
                    content=b"",
                    nlink=2,
                ),
            ),
        ),
        preparation_slots=tuple(working_slots),
    )
    excluded_index = next(
        index for index, boundary in enumerate(candidate.boundaries) if boundary.role == "excluded"
    )
    boundaries = list(candidate.boundaries)
    boundaries[excluded_index] = replace(
        boundaries[excluded_index], ancestors=(protected_ancestor,)
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, boundaries=tuple(boundaries)))
    assert excinfo.value.reason_code == "protected_topology_conflict"


def test_fresh_and_retained_inputs_require_exact_recursive_types() -> None:
    """Mutable bytes and numeric lookalikes cannot authorize planning or replay."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    current = _corrected_initial_observation(bound)
    retained = _corrected_retained_inputs(bound)
    baseline_node = current.replica_state.working_nodes[0]
    malformed_node = replace(baseline_node, content=bytearray(baseline_node.content), nlink=True)
    malformed_replica = replace(current.replica_state, working_nodes=(malformed_node,))

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            bound,
            replace(current, replica_state=malformed_replica),
            retained,
        )
    assert excinfo.value.reason_code == "invalid_input_type"

    malformed_retained = replace(
        retained,
        drain=replace(retained.drain, replica_generation=float(retained.drain.replica_generation)),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, current, malformed_retained)
    assert excinfo.value.reason_code == "invalid_input_type"

    malformed_control = replace(current.control, exclusion_receipts=[])
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            bound,
            replace(current, control=malformed_control),
            retained,
        )
    assert excinfo.value.reason_code == "invalid_input_type"


def test_replay_journal_facts_require_exact_recursive_types() -> None:
    """Malformed journal identity fields fail as types before protocol comparison."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    current = _corrected_initial_observation(bound)
    retained = _corrected_retained_inputs(bound)
    malformed = module.ReconciliationJournal(
        retained,
        (module.IntentFact(bound.fingerprint, True, "intent-1"),),
        (),
        (),
        (),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, malformed, current)
    assert excinfo.value.reason_code == "invalid_input_type"


def test_replay_rejects_prepared_identity_aliasing_a_retained_preimage() -> None:
    """A created marker cannot reuse any concrete identity retained by the operation."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    marker_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "create_ownership_marker"
    )
    marker_action = plan.actions[marker_index]
    prior_actions = plan.actions[:marker_index]
    intents = tuple(
        module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
        for index, action in enumerate((*prior_actions, marker_action))
    )
    synchronizations = tuple(
        module.SynchronizationFact(
            bound.fingerprint,
            action.action_id,
            requirement,
            f"sync-{index}-{requirement}",
            _corrected_synchronization_position(
                bound,
                plan,
                action,
                requirement,
            ),
        )
        for index, action in enumerate(prior_actions)
        for requirement in action.synchronization_requirements
    )
    completions = tuple(
        module.CompletionFact(
            bound.fingerprint,
            action.action_id,
            module.observation_fingerprint(
                module.materialize_expected_observation(action.completion_postimage, ())
            ),
            f"completion-{index}",
        )
        for index, action in enumerate(prior_actions)
    )
    alias = next(
        boundary.identity for boundary in bound.spec.boundaries if boundary.role == "private"
    )
    preparation = module.PreparationFact(
        bound.fingerprint,
        marker_action.prepared_slot_id,
        alias,
        "marker-created",
        _corrected_preparation_position(bound, marker_action.prepared_slot_id),
    )
    fresh = module.materialize_expected_observation(
        marker_action.permitted_effect_postimages[0],
        (preparation,),
    )
    journal = module.ReconciliationJournal(
        retained,
        intents,
        (preparation,),
        synchronizations,
        completions,
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, journal, fresh)
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def _replay_journal_with_aliasing_preparation(
    bound: object,
    retained: object,
    slot_id: str,
    alias_identity: object,
) -> object:
    """Journal holding one producer intent and one aliasing preparation fact."""
    import yinshi.services.workspace_replica as module

    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        retained,
    )
    producer = next(
        action
        for action in plan.actions
        if action.creates_prepared_identity and action.prepared_slot_id == slot_id
    )
    return module.ReconciliationJournal(
        retained,
        (module.IntentFact(bound.fingerprint, producer.action_id, f"intent-{slot_id}"),),
        (
            module.PreparationFact(
                bound.fingerprint,
                slot_id,
                alias_identity,
                f"creation-{slot_id}",
                _corrected_preparation_position(bound, slot_id),
            ),
        ),
        (),
        (),
    )


def test_replay_rejects_marker_preparation_identity_equal_to_marker_parent() -> None:
    """A marker preparation fact cannot reuse its existing marker parent identity."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    marker_parent = bound.spec.ownership_markers[0].parent.reference.identity
    journal = _replay_journal_with_aliasing_preparation(
        bound,
        retained,
        "ownership-marker",
        marker_parent,
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, journal, _corrected_initial_observation(bound))
    assert excinfo.value.reason_code == "ambiguous_node_identity"


@pytest.mark.parametrize(
    "collector",
    [
        "marker parent",
        "preparation materialization parent",
        "preparation installation parent",
        "object destination parent",
        "quarantine source parent",
        "quarantine destination parent",
        "namespace-root installation parent",
    ],
)
def test_replay_rejects_prepared_identity_aliasing_every_initial_parent(
    collector: str,
) -> None:
    """Every concrete initial parent joins the retained replay identity map."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    retained = _corrected_retained_inputs(bound)
    slots = {slot.slot_id: slot for slot in bound.spec.preparation_slots}
    spec = bound.spec
    if collector == "marker parent":
        alias_identity = spec.ownership_markers[0].parent.reference.identity
        slot_id = "ownership-marker"
    elif collector == "preparation materialization parent":
        alias_identity = slots["index-new"].materialization_parent.reference.identity
        slot_id = "index-new"
    elif collector == "preparation installation parent":
        alias_identity = slots["blob-source"].installation_parent.reference.identity
        slot_id = "blob-source"
    elif collector == "object destination parent":
        alias_identity = spec.destination_objects[1].parent.reference.identity
        slot_id = "commit-new"
    elif collector == "quarantine source parent":
        alias_identity = spec.quarantine_slots[2].source_parent.reference.identity
        slot_id = "loose-new"
    elif collector == "quarantine destination parent":
        alias_identity = spec.quarantine_slots[0].destination_parent.reference.identity
        slot_id = "working-new"
    else:
        alias_identity = slots["working-new"].installation_parent.reference.identity
        slot_id = "working-new"
    journal = _replay_journal_with_aliasing_preparation(
        bound,
        retained,
        slot_id,
        alias_identity,
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, journal, _corrected_initial_observation(bound))
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_replay_rejects_impossible_marker_image_instead_of_retrying_synchronization() -> None:
    """A marker image reusing its parent identity is refused, never retried."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    marker_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "create_ownership_marker"
    )
    marker_action = plan.actions[marker_index]
    prior_actions = plan.actions[:marker_index]
    alias = module.PreparationFact(
        bound.fingerprint,
        marker_action.prepared_slot_id,
        bound.spec.ownership_markers[0].parent.reference.identity,
        "marker-created",
        _corrected_preparation_position(bound, marker_action.prepared_slot_id),
    )
    fresh = module.materialize_expected_observation(
        marker_action.permitted_effect_postimages[0],
        (alias,),
    )
    journal = module.ReconciliationJournal(
        retained,
        tuple(
            module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
            for index, action in enumerate((*prior_actions, marker_action))
        ),
        (alias,),
        tuple(
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                requirement,
                f"sync-{index}-{requirement}",
                _corrected_synchronization_position(
                    bound,
                    plan,
                    action,
                    requirement,
                ),
            )
            for index, action in enumerate(prior_actions)
            for requirement in action.synchronization_requirements
        ),
        tuple(
            module.CompletionFact(
                bound.fingerprint,
                action.action_id,
                module.observation_fingerprint(
                    module.materialize_expected_observation(action.completion_postimage, ())
                ),
                f"completion-{index}",
            )
            for index, action in enumerate(prior_actions)
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, journal, fresh)
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_plan_rejects_fresh_sections_that_alias_one_physical_identity() -> None:
    """A fresh parent identity cannot equal another section's node identity."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    complete = _corrected_initial_observation(bound)
    object_identity = bound.spec.destination_objects[0].storage.identity
    parents = tuple(
        replace(parent, identity=object_identity) if index == 0 else parent
        for index, parent in enumerate(complete.parents)
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, replace(complete, parents=parents), retained)
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_plan_rejects_fresh_sections_that_split_one_location_identity() -> None:
    """One fresh location cannot carry different identities in two sections."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    complete = _corrected_initial_observation(bound)
    working_location = bound.spec.baseline.working_nodes[0].location
    parents = tuple(
        replace(parent, location=working_location) if index == 0 else parent
        for index, parent in enumerate(complete.parents)
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, replace(complete, parents=parents), retained)
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_binding_rejects_static_identity_aliases_across_physical_storage() -> None:
    """One concrete identity cannot describe different retained locations or bytes."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    destination = candidate.destination_objects[0]
    aliased_destination = replace(
        destination,
        storage=replace(
            destination.storage,
            identity=candidate.baseline.index.storage.identity,
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                candidate,
                destination_objects=(
                    aliased_destination,
                    *candidate.destination_objects[1:],
                ),
            )
        )
    assert excinfo.value.reason_code == "ambiguous_node_identity"

    boundaries = list(candidate.boundaries)
    boundaries[1] = replace(boundaries[1], identity=boundaries[0].identity)
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, boundaries=tuple(boundaries)))
    assert excinfo.value.reason_code == "ambiguous_node_identity"

    index_node = candidate.baseline.index.storage
    conflicting_ancestor = replace(index_node, kind="directory", content=b"", nlink=2)
    boundaries = list(candidate.boundaries)
    boundaries[3] = replace(
        boundaries[3],
        ancestors=(conflicting_ancestor,),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, boundaries=tuple(boundaries)))
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_binding_rejects_stale_selected_reference_semantics() -> None:
    """Resolved OID must match the concrete selected loose-ref source."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    stale_refs = replace(
        candidate.desired.refs,
        resolved_oid=candidate.baseline.refs.resolved_oid,
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(candidate, desired=replace(candidate.desired, refs=stale_refs))
        )
    assert excinfo.value.reason_code == "invalid_ref_semantics"


def test_loose_ref_crash_images_track_the_selected_physical_source() -> None:
    """Quarantine and install crash images derive ref semantics from physical facts."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    parent_by_slot = {
        slot.slot_id: slot.materialization_parent for slot in bound.spec.preparation_slots
    } | {marker.slot_id: marker.parent for marker in bound.spec.ownership_markers}
    preparations = tuple(
        module.PreparationFact(
            bound.fingerprint,
            slot_id,
            module.NodeIdentity(
                parent.reference.identity.filesystem_id,
                f"physical-{slot_id}",
            ),
            f"creation-{slot_id}",
            _corrected_preparation_position(bound, slot_id),
        )
        for slot_id, parent in sorted(parent_by_slot.items())
    )

    def journal_through_intent(action_index: int) -> object:
        prior = plan.actions[:action_index]
        current_action = plan.actions[action_index]
        intents = tuple(
            module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
            for index, action in enumerate((*prior, current_action))
        )
        synchronizations = tuple(
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                requirement,
                f"sync-{index}-{requirement}",
                _corrected_synchronization_position(
                    bound,
                    plan,
                    action,
                    requirement,
                    preparations,
                ),
                _corrected_creation_receipt(action, preparations),
            )
            for index, action in enumerate(prior)
            for requirement in module.resolve_synchronization_requirements(
                action,
                preparations,
            )
        )
        completions = tuple(
            module.CompletionFact(
                bound.fingerprint,
                action.action_id,
                module.observation_fingerprint(
                    module.materialize_expected_observation(
                        action.completion_postimage,
                        preparations,
                    )
                ),
                f"completion-{index}",
            )
            for index, action in enumerate(prior)
        )
        created_slots = {
            action.prepared_slot_id
            for action in (*prior, current_action)
            if action.creates_prepared_identity
        }
        return module.ReconciliationJournal(
            retained,
            intents,
            tuple(fact for fact in preparations if fact.slot_id in created_slots),
            synchronizations,
            completions,
        )

    quarantine_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "quarantine_selected_ref"
    )
    quarantine_action = plan.actions[quarantine_index]
    quarantine_slot = next(
        slot
        for slot in bound.spec.quarantine_slots
        if slot.slot_id == quarantine_action.prepared_slot_id
    )
    old_loose = bound.spec.baseline.refs.loose_ref
    quarantine_preimage = module.materialize_expected_observation(
        quarantine_action.preimage,
        preparations,
    )
    quarantine_storage = tuple(
        (
            module.NodeState(
                quarantine_slot.destination,
                old_loose.identity,
                old_loose.kind,
                old_loose.content,
                old_loose.executable,
                old_loose.nlink,
                old_loose.privilege_bits,
            )
            if state.location == quarantine_slot.destination
            else state
        )
        for state in quarantine_preimage.storage
    )
    quarantine_refs = replace(
        quarantine_preimage.replica_state.refs,
        resolved_oid=None,
        loose_ref=module.AbsentState(quarantine_slot.source),
    )
    quarantined = replace(
        quarantine_preimage,
        replica_state=replace(quarantine_preimage.replica_state, refs=quarantine_refs),
        storage=quarantine_storage,
    )
    assert (
        module.replay_reconciliation(
            bound,
            journal_through_intent(quarantine_index),
            quarantined,
        ).kind
        == "retry_synchronization"
    )

    install_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "install_selected_ref"
    )
    install_action = plan.actions[install_index]
    install_slot = next(
        slot
        for slot in bound.spec.preparation_slots
        if slot.slot_id == install_action.prepared_slot_id
    )
    install_fact = next(fact for fact in preparations if fact.slot_id == install_slot.slot_id)
    install_preimage = module.materialize_expected_observation(
        install_action.preimage,
        preparations,
    )
    install_storage = tuple(
        (
            module.AbsentState(install_slot.destination)
            if state.location == install_slot.destination
            else state
        )
        for state in install_preimage.storage
    )
    desired_loose = bound.spec.desired.refs.loose_ref
    installed_refs = replace(
        install_preimage.replica_state.refs,
        resolved_oid=bound.spec.desired.refs.resolved_oid,
        loose_ref=module.NodeState(
            desired_loose.location,
            install_fact.identity,
            desired_loose.kind,
            desired_loose.content,
            desired_loose.executable,
            desired_loose.nlink,
            desired_loose.privilege_bits,
        ),
    )
    installed = replace(
        install_preimage,
        replica_state=replace(install_preimage.replica_state, refs=installed_refs),
        storage=install_storage,
    )
    assert (
        module.replay_reconciliation(
            bound,
            journal_through_intent(install_index),
            installed,
        ).kind
        == "retry_synchronization"
    )


def test_binding_rejects_index_entries_without_physical_index_storage() -> None:
    """Absent index storage cannot retain semantic entries from a prior image."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    absent_index = replace(
        candidate.desired.index,
        storage=module.AbsentState(candidate.baseline.index.storage.location),
        entries=candidate.baseline.index.entries,
    )
    preparation_slots = tuple(
        slot for slot in candidate.preparation_slots if slot.slot_id != "index-new"
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                candidate,
                desired=replace(candidate.desired, index=absent_index),
                preparation_slots=preparation_slots,
            )
        )
    assert excinfo.value.reason_code == "invalid_index_semantics"


def test_marker_presence_does_not_fabricate_broker_exclusion_receipt() -> None:
    """Physical marker creation and durable broker exclusion are separate actions."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    marker_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "create_ownership_marker"
    )
    marker_action = plan.actions[marker_index]
    marker_postimage = marker_action.permitted_effect_postimages[0]
    assert marker_postimage.control.exclusion_receipts == (
        marker_action.preimage.control.exclusion_receipts
    )
    exclusion_action = plan.actions[marker_index + 1]
    assert exclusion_action.effect_kind == "record_marker_exclusion"
    assert exclusion_action.preimage == marker_action.completion_postimage
    assert len(exclusion_action.completion_postimage.control.exclusion_receipts) == 1

    prior_actions = plan.actions[:marker_index]
    preparation = module.PreparationFact(
        bound.fingerprint,
        marker_action.prepared_slot_id,
        module.NodeIdentity("fs-private", "physical-marker"),
        "marker-created",
        _corrected_preparation_position(bound, marker_action.prepared_slot_id),
    )
    marker_spec = bound.spec.ownership_markers[0]
    marker_preimage = module.materialize_expected_observation(marker_action.preimage, ())
    marker_storage = tuple(
        (
            module.NodeState(
                marker_spec.destination,
                preparation.identity,
                "file",
                marker_action.effect_payload,
                marker_spec.executable,
                1,
                False,
            )
            if state.location == marker_spec.destination
            else state
        )
        for state in marker_preimage.storage
    )
    physical_marker = replace(marker_preimage, storage=marker_storage)
    intents = tuple(
        module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
        for index, action in enumerate((*prior_actions, marker_action))
    )
    synchronizations = tuple(
        module.SynchronizationFact(
            bound.fingerprint,
            action.action_id,
            requirement,
            f"sync-{index}-{requirement}",
            _corrected_synchronization_position(
                bound,
                plan,
                action,
                requirement,
            ),
        )
        for index, action in enumerate(prior_actions)
        for requirement in action.synchronization_requirements
    )
    completions = tuple(
        module.CompletionFact(
            bound.fingerprint,
            action.action_id,
            module.observation_fingerprint(
                module.materialize_expected_observation(action.completion_postimage, ())
            ),
            f"completion-{index}",
        )
        for index, action in enumerate(prior_actions)
    )
    journal = module.ReconciliationJournal(
        retained,
        intents,
        (preparation,),
        synchronizations,
        completions,
    )
    replay = module.replay_reconciliation(bound, journal, physical_marker)
    assert replay.kind == "retry_synchronization"
    assert physical_marker.control.exclusion_receipts == ()


def test_path_changes_bind_and_observe_each_distinct_parent() -> None:
    """Move durability binds concrete source and destination directory identities."""
    from dataclasses import fields

    import yinshi.services.workspace_replica as module

    assert hasattr(module, "ParentBinding")
    assert hasattr(module, "ParentObservation")
    assert "parent_synchronizations" in {field.name for field in fields(module.ActionContract)}

    bound = module.bind_specification(_corrected_changed_spec())
    retained = _corrected_retained_inputs(bound)
    current = _corrected_initial_observation(bound)
    missing_parent = replace(current, parents=current.parents[1:])
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, missing_parent, retained)
    assert excinfo.value.reason_code == "baseline_conflict"

    plan = module.plan_reconciliation(bound, current, retained)
    preparations = tuple(
        module.PreparationFact(
            bound.fingerprint,
            action.prepared_slot_id,
            module.NodeIdentity(
                next(
                    binding.reference.identity.filesystem_id
                    for binding in action.parent_synchronizations
                    if isinstance(binding.reference, module.ExistingNode)
                ),
                f"prepared-{action.prepared_slot_id}",
            ),
            f"creation-{action.prepared_slot_id}",
            _corrected_preparation_position(bound, action.prepared_slot_id),
        )
        for action in plan.actions
        if action.creates_prepared_identity
    )
    for effect_kind in ("quarantine_working_node", "install_working_node"):
        action = next(action for action in plan.actions if action.effect_kind == effect_kind)
        requirements = module.resolve_synchronization_requirements(action, preparations)
        parent_requirements = tuple(
            requirement for requirement in requirements if requirement.startswith("parent:")
        )
        assert len(action.parent_synchronizations) == 2
        assert len(parent_requirements) == 2
        assert len(set(parent_requirements)) == 2
        assert all(bound.fingerprint.split(":", 1)[1][:12] in item for item in parent_requirements)


def test_public_parent_sync_resolver_validates_every_preparation_fact_field() -> None:
    """Public synchronization resolution rejects malformed preparation facts recursively."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    action = plan.actions[0]
    valid = module.PreparationFact(
        bound.fingerprint,
        "unused-slot",
        module.NodeIdentity("fs-private", "unused-node"),
        "creation-receipt",
        _corrected_journal_position(bound, 1, "unused-preparation"),
    )
    malformed = (
        replace(valid, fingerprint=1),
        replace(valid, slot_id=1),
        replace(valid, identity=replace(valid.identity, node_id=1)),
        replace(valid, creation_receipt_id=1),
        replace(valid, journal_position=1),
        replace(valid, journal_position=replace(valid.journal_position, journal_id=1)),
        replace(valid, journal_position=replace(valid.journal_position, sequence="1")),
        replace(
            valid,
            journal_position=replace(valid.journal_position, append_receipt_id=1),
        ),
    )

    for fact in malformed:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.resolve_synchronization_requirements(action, (fact,))
        assert excinfo.value.reason_code == "invalid_input_type"


def _corrected_retained_inputs(bound: object) -> object:
    """Build external facts while preserving their original receipt ownership."""
    import yinshi.services.workspace_replica as module

    identity = bound.spec.identity
    return module.RetainedInputs(
        module.OriginalAcknowledgmentFact(
            identity.acknowledgment_receipt_id, identity.selected_authority
        ),
        module.DrainFact(
            identity.drain_receipt_id, identity.physical_target_id, identity.replica_generation
        ),
        module.ExportFact(
            identity.export_receipt_id,
            identity.physical_target_id,
            identity.replica_generation,
            bound.fingerprint,
        ),
        module.InventoryFact(
            identity.inventory_receipt_id,
            identity.selected_authority,
            identity.object_format,
            tuple(item.object_id for item in bound.spec.required_objects),
            tuple(item.object_id for item in bound.spec.destination_objects),
            bound.fingerprint,
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("incarnation", "other-incarnation"),
        ("binding_id", "other-binding"),
        ("selected_authority", "other-authority"),
        ("operation_id", "other-operation"),
        ("reservation_id", "other-reservation"),
        ("destination_binding", "other-destination"),
        ("physical_target_id", "other-target"),
        ("generation", 8),
        ("replica_generation", 4),
        ("acknowledgment_receipt_id", "other-ack"),
        ("drain_receipt_id", "other-drain"),
        ("export_receipt_id", "other-export"),
        ("object_format", "sha256"),
        ("inventory_receipt_id", "other-inventory"),
    ],
)
def test_every_fresh_operation_identity_field_must_match_bound_specification(
    field: str,
    value: object,
) -> None:
    """Fresh observations from another exact operation cannot authorize planning."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    fresh = _corrected_initial_observation(bound)
    foreign = replace(fresh, identity=replace(fresh.identity, **{field: value}))

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, foreign, _corrected_retained_inputs(bound))
    assert excinfo.value.reason_code == "stale_observation"


@pytest.mark.parametrize(
    "fact_name,field,value,reason_code",
    [
        ("acknowledgment", "receipt_id", "wrong", "acknowledgment_unavailable"),
        ("acknowledgment", "execution_owner_id", "wrong", "acknowledgment_unavailable"),
        ("drain", "receipt_id", "wrong", "drain_unverified"),
        ("drain", "physical_target_id", "wrong", "drain_unverified"),
        ("drain", "replica_generation", 4, "drain_unverified"),
        ("export", "receipt_id", "wrong", "export_stale"),
        ("export", "physical_target_id", "wrong", "export_stale"),
        ("export", "replica_generation", 4, "export_stale"),
        ("export", "specification_fingerprint", "sha256:" + "0" * 64, "export_stale"),
        ("inventory", "receipt_id", "wrong", "inventory_stale"),
        ("inventory", "original_owner_id", "wrong", "inventory_stale"),
        ("inventory", "object_format", "sha256", "inventory_stale"),
        ("inventory", "required_object_ids", (), "inventory_stale"),
        ("inventory", "destination_object_ids", (), "inventory_stale"),
        ("inventory", "specification_fingerprint", "sha256:" + "0" * 64, "inventory_stale"),
    ],
)
def test_every_retained_fact_field_binds_exact_external_authority(
    fact_name: str,
    field: str,
    value: object,
    reason_code: str,
) -> None:
    """No retained receipt field can be changed or re-owned by reconciliation."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    conflicting_fact = replace(getattr(retained, fact_name), **{field: value})
    conflicting = replace(retained, **{fact_name: conflicting_fact})

    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            bound,
            _corrected_initial_observation(bound),
            conflicting,
        )
    assert excinfo.value.reason_code == reason_code


def test_plan_derives_all_protocol_stages_and_marker_payload_after_binding() -> None:
    """Planning derives all stages and fingerprint-bearing marker bytes from fresh state."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )

    assert tuple(sorted({action.stage for action in plan.actions})) == tuple(range(1, 17))
    assert all(action.fingerprint == bound.fingerprint for action in plan.actions)
    assert all(
        action.synchronization_requirements or action.parent_synchronizations
        for action in plan.actions
    )
    marker_action = next(
        action for action in plan.actions if action.effect_kind == "create_ownership_marker"
    )
    assert (
        marker_action.effect_payload
        == b"yinshi-owner-v1\x00fingerprint=" + bound.fingerprint.encode("ascii")
    )
    release_kinds = [action.effect_kind for action in plan.actions if action.stage == 16]
    assert release_kinds.index("physical_release") < release_kinds.index("logical_release")
    assert release_kinds.index("logical_release") < release_kinds.index("admit_readers")


def test_plan_reports_only_canonical_content_mutation_actions() -> None:
    """The read-only count excludes preparation, markers, and control effects."""
    import yinshi.services.workspace_replica as module

    unchanged_bound = module.bind_specification(_corrected_spec())
    unchanged_plan = module.plan_reconciliation(
        unchanged_bound,
        _corrected_initial_observation(unchanged_bound),
        _corrected_retained_inputs(unchanged_bound),
    )
    assert any(action.effect_kind == "materialize_preparation" for action in unchanged_plan.actions)
    assert any(action.effect_kind == "create_ownership_marker" for action in unchanged_plan.actions)
    assert unchanged_plan.content_mutation_count == 0

    changed_bound = module.bind_specification(_corrected_changed_spec())
    changed_plan = module.plan_reconciliation(
        changed_bound,
        _corrected_initial_observation(changed_bound),
        _corrected_retained_inputs(changed_bound),
    )
    assert changed_plan.content_mutation_count == 9

    promotion_spec = replace(
        _corrected_spec(),
        destination_objects=_corrected_spec().destination_objects[1:],
    )
    promotion_bound = module.bind_specification(promotion_spec)
    promotion_plan = module.plan_reconciliation(
        promotion_bound,
        _corrected_initial_observation(promotion_bound),
        _corrected_retained_inputs(promotion_bound),
    )
    assert promotion_plan.content_mutation_count == 1


def test_binding_refuses_malformed_types_topology_identities_and_inventory() -> None:
    """Binding rejects malformed input before it can produce an authorization digest."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    baseline = base.baseline
    desired = base.desired
    child_manifest = _file(b"missing/child", b"child")
    child_node = module.NodeState(
        module.Location("working", b"missing/child"),
        module.NodeIdentity("fs-working", "child"),
        "file",
        b"child",
        False,
        1,
        False,
    )
    child_desired = module.DesiredNodeState(
        child_node.location,
        module.ExistingNode(child_node.identity),
        "file",
        b"child",
        False,
        1,
        False,
    )
    missing_parent = replace(
        base,
        baseline=replace(
            baseline,
            manifest=baseline.manifest + (child_manifest,),
            working_nodes=baseline.working_nodes + (child_node,),
        ),
        desired=replace(
            desired,
            manifest=desired.manifest + (child_manifest,),
            working_nodes=desired.working_nodes + (child_desired,),
        ),
    )
    prefix_entries = (_index(b"collision", oid="1" * 40), _index(b"collision/child", oid="1" * 40))
    unknown_existing = replace(
        base,
        desired=replace(
            desired,
            working_nodes=(
                replace(
                    desired.working_nodes[0],
                    reference=module.ExistingNode(module.NodeIdentity("fs-working", "unknown")),
                ),
            ),
        ),
    )
    unknown_prepared = replace(
        base,
        desired=replace(
            desired,
            working_nodes=(
                replace(desired.working_nodes[0], reference=module.PreparedNode("unknown-slot")),
            ),
        ),
    )
    cases = (
        ("list-collection", replace(base, boundaries=list(base.boundaries)), "invalid_input_type"),
        (
            "nested-bool",
            replace(
                base, baseline=replace(baseline, index=replace(baseline.index, sparse_index=0))
            ),
            "invalid_input_type",
        ),
        (
            "baseline-generation",
            replace(base, baseline=replace(baseline, generation=6)),
            "generation_mismatch",
        ),
        (
            "desired-generation",
            replace(base, desired=replace(desired, generation=9)),
            "generation_mismatch",
        ),
        (
            "replica-generation",
            replace(base, desired=replace(desired, replica_generation=4)),
            "generation_mismatch",
        ),
        (
            "boundary-coverage",
            replace(base, boundaries=base.boundaries[:-1]),
            "incomplete_topology",
        ),
        ("missing-parent", missing_parent, "incomplete_topology"),
        (
            "index-prefix",
            replace(
                base,
                baseline=replace(baseline, index=replace(baseline.index, entries=prefix_entries)),
            ),
            "index_prefix_collision",
        ),
        ("unknown-existing", unknown_existing, "unexplained_desired_node"),
        ("unknown-prepared", unknown_prepared, "missing_preparation_slot"),
        (
            "object-coverage",
            replace(base, required_objects=base.required_objects[:1]),
            "incomplete_object_inventory",
        ),
        (
            "object-limit",
            replace(base, limits=replace(base.limits, max_objects=1)),
            "inventory_limit_exceeded",
        ),
        (
            "cross-filesystem-preparation",
            replace(
                base,
                preparation_slots=(
                    replace(
                        base.preparation_slots[0],
                        materialization_parent=replace(
                            base.preparation_slots[0].materialization_parent,
                            reference=module.ExistingNode(
                                module.NodeIdentity("fs-wrong", "parent")
                            ),
                        ),
                    ),
                )
                + base.preparation_slots[1:],
            ),
            "cross_filesystem_transition",
        ),
    )
    for label, candidate, reason_code in cases:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.bind_specification(candidate)
        assert excinfo.value.reason_code == reason_code, label


def test_canonical_binding_ignores_collection_order_without_digest_self_reference() -> None:
    """Canonical binding sorts sets while marker payload derivation stays outside its input."""
    import yinshi.services.workspace_replica as module

    original = _corrected_spec()
    permuted = replace(
        original,
        boundaries=tuple(reversed(original.boundaries)),
        preparation_slots=tuple(reversed(original.preparation_slots)),
        required_objects=tuple(reversed(original.required_objects)),
        destination_objects=tuple(reversed(original.destination_objects)),
    )
    first = module.bind_specification(original)
    second = module.bind_specification(permuted)

    assert first == second
    assert module.bind_specification(first.spec) == first
    first_plan = module.plan_reconciliation(
        first,
        _corrected_initial_observation(first),
        _corrected_retained_inputs(first),
    )
    second_plan = module.plan_reconciliation(
        second,
        _corrected_initial_observation(second),
        _corrected_retained_inputs(second),
    )
    assert first_plan == second_plan
    marker = next(
        action for action in first_plan.actions if action.effect_kind == "create_ownership_marker"
    )
    assert marker.effect_payload.count(first.fingerprint.encode("ascii")) == 1
    assert first.spec.ownership_markers[0].payload_prefix == b"yinshi-owner-v1"


def test_canonical_binding_keys_unordered_admin_collections_before_comparison() -> None:
    """Independent enumeration order is irrelevant, while duplicate locations fail closed."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    baseline_reflog = base.baseline.refs.reflogs[0]
    baseline_metadata = base.baseline.refs.unrelated_metadata[0]
    extra_reflog = replace(
        baseline_reflog,
        location=module.Location("admin", b"logs/refs/heads/other"),
        identity=module.NodeIdentity("fs-admin", "reflog-other"),
        content=b"other-log",
    )
    extra_metadata = replace(
        baseline_metadata,
        location=module.Location("admin", b"FETCH_HEAD"),
        identity=module.NodeIdentity("fs-admin", "fetch-head"),
        content=b"fetch",
    )

    def desired(concrete: object) -> object:
        return module.DesiredNodeState(
            concrete.location,
            module.ExistingNode(concrete.identity),
            concrete.kind,
            concrete.content,
            concrete.executable,
            concrete.nlink,
            concrete.privilege_bits,
        )

    candidate = replace(
        base,
        baseline=replace(
            base.baseline,
            refs=replace(
                base.baseline.refs,
                reflogs=(baseline_reflog, extra_reflog),
                unrelated_metadata=(baseline_metadata, extra_metadata),
            ),
        ),
        desired=replace(
            base.desired,
            refs=replace(
                base.desired.refs,
                reflogs=(desired(extra_reflog), desired(baseline_reflog)),
                unrelated_metadata=(
                    desired(extra_metadata),
                    desired(baseline_metadata),
                ),
            ),
        ),
    )
    independently_permuted = replace(
        candidate,
        baseline=replace(
            candidate.baseline,
            refs=replace(
                candidate.baseline.refs,
                reflogs=tuple(reversed(candidate.baseline.refs.reflogs)),
                unrelated_metadata=tuple(reversed(candidate.baseline.refs.unrelated_metadata)),
            ),
        ),
        desired=replace(
            candidate.desired,
            refs=replace(
                candidate.desired.refs,
                reflogs=tuple(reversed(candidate.desired.refs.reflogs)),
            ),
        ),
    )

    first = module.bind_specification(candidate)
    second = module.bind_specification(independently_permuted)
    assert first == second
    assert module.plan_reconciliation(
        first,
        _corrected_initial_observation(first),
        _corrected_retained_inputs(first),
    ) == module.plan_reconciliation(
        second,
        _corrected_initial_observation(second),
        _corrected_retained_inputs(second),
    )

    duplicate = replace(
        base,
        baseline=replace(
            base.baseline,
            refs=replace(
                base.baseline.refs,
                reflogs=(baseline_reflog, baseline_reflog),
            ),
        ),
        desired=replace(
            base.desired,
            refs=replace(
                base.desired.refs,
                reflogs=(desired(baseline_reflog), desired(baseline_reflog)),
            ),
        ),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(duplicate)
    assert excinfo.value.reason_code == "duplicate_location"


def test_replay_recognizes_every_internal_crash_boundary_from_fresh_state() -> None:
    """Every action replays intent, effect, sync, and completion from fresh observations."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    parent_by_slot = {
        slot.slot_id: slot.materialization_parent for slot in bound.spec.preparation_slots
    } | {marker.slot_id: marker.parent for marker in bound.spec.ownership_markers}
    all_preparations = tuple(
        module.PreparationFact(
            bound.fingerprint,
            slot_id,
            module.NodeIdentity(
                parent.reference.identity.filesystem_id,
                f"prepared-{slot_id}",
            ),
            f"creation-{slot_id}",
            _corrected_preparation_position(bound, slot_id),
        )
        for slot_id, parent in sorted(parent_by_slot.items())
    )

    def preparation_prefix(last_action_index: int) -> tuple[object, ...]:
        created = {
            action.prepared_slot_id
            for action in plan.actions[: last_action_index + 1]
            if action.creates_prepared_identity
        }
        return tuple(fact for fact in all_preparations if fact.slot_id in created)

    def completed_journal(count: int) -> object:
        preparations = preparation_prefix(count - 1)
        intents = tuple(
            module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
            for index, action in enumerate(plan.actions[:count])
        )
        synchronizations = tuple(
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                requirement,
                f"sync-{index}-{requirement}",
                _corrected_synchronization_position(
                    bound,
                    plan,
                    action,
                    requirement,
                    preparations,
                ),
                _corrected_creation_receipt(action, preparations),
            )
            for index, action in enumerate(plan.actions[:count])
            for requirement in module.resolve_synchronization_requirements(
                action,
                preparations,
            )
        )
        completions = tuple(
            module.CompletionFact(
                bound.fingerprint,
                action.action_id,
                module.observation_fingerprint(
                    module.materialize_expected_observation(
                        action.completion_postimage,
                        preparations,
                    )
                ),
                f"completion-{index}",
            )
            for index, action in enumerate(plan.actions[:count])
        )
        return module.ReconciliationJournal(
            retained,
            intents,
            preparations,
            synchronizations,
            completions,
        )

    for index, action in enumerate(plan.actions):
        prefix = completed_journal(index)
        prior_preparations = prefix.preparations
        preimage = module.materialize_expected_observation(action.preimage, prior_preparations)
        assert module.replay_reconciliation(bound, prefix, preimage).kind == "record_intent"

        intent = module.IntentFact(bound.fingerprint, action.action_id, f"intent-current-{index}")
        intended = replace(prefix, intents=prefix.intents + (intent,))
        assert module.replay_reconciliation(bound, intended, preimage).kind == "perform_effect"

        effect_preparations = prior_preparations
        if action.creates_prepared_identity:
            created_fact = next(
                fact for fact in all_preparations if fact.slot_id == action.prepared_slot_id
            )
            effect_preparations += (created_fact,)
            with pytest.raises(SnapshotRejected) as excinfo:
                module.replay_reconciliation(
                    bound,
                    intended,
                    module.materialize_expected_observation(
                        action.permitted_effect_postimages[0],
                        effect_preparations,
                    ),
                )
            assert excinfo.value.reason_code == "lost_preparation_identity"
        effected = replace(intended, preparations=effect_preparations)
        postimage = module.materialize_expected_observation(
            action.permitted_effect_postimages[0],
            effect_preparations,
        )
        assert (
            module.replay_reconciliation(bound, effected, postimage).kind == "retry_synchronization"
        )

        syncs = tuple(
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                requirement,
                f"sync-current-{index}-{requirement}",
                _corrected_synchronization_position(
                    bound,
                    plan,
                    action,
                    requirement,
                    effect_preparations,
                ),
                _corrected_creation_receipt(action, effect_preparations),
            )
            for requirement in module.resolve_synchronization_requirements(
                action,
                effect_preparations,
            )
        )
        synchronized = replace(effected, synchronizations=effected.synchronizations + syncs)
        assert (
            module.replay_reconciliation(bound, synchronized, postimage).kind == "record_completion"
        )

    completed = completed_journal(len(plan.actions))
    final_observation = module.materialize_expected_observation(
        plan.actions[-1].completion_postimage,
        completed.preparations,
    )
    assert module.replay_reconciliation(bound, completed, final_observation).kind == "complete"


def test_replay_creation_actions_handle_preparation_facts_before_preimage_retry() -> None:
    """Creation replay honors fact and presence combinations instead of blind retries."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    all_preparations = _corrected_preparation_facts(bound)
    creation_actions = [
        (index, action)
        for index, action in enumerate(plan.actions)
        if action.creates_prepared_identity
    ]
    assert {action.effect_kind for _index, action in creation_actions} == {
        "materialize_preparation",
        "create_ownership_marker",
    }

    for index, action in creation_actions:
        prefix = _corrected_completed_journal(bound, plan, retained, index)
        preimage = module.materialize_expected_observation(action.preimage, prefix.preparations)
        intent = module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
        intended = replace(prefix, intents=prefix.intents + (intent,))
        fact = next(fact for fact in all_preparations if fact.slot_id == action.prepared_slot_id)
        present = module.materialize_expected_observation(
            action.permitted_effect_postimages[0],
            prefix.preparations + (fact,),
        )

        assert module.replay_reconciliation(bound, intended, preimage).kind == "perform_effect"

        with pytest.raises(SnapshotRejected) as excinfo:
            module.replay_reconciliation(bound, intended, present)
        assert excinfo.value.reason_code == "lost_preparation_identity"

        effected = replace(intended, preparations=intended.preparations + (fact,))
        recorded = module.materialize_expected_observation(
            action.permitted_effect_postimages[0],
            effected.preparations,
        )
        assert (
            module.replay_reconciliation(bound, effected, recorded).kind == "retry_synchronization"
        )

        with pytest.raises(SnapshotRejected) as excinfo:
            module.replay_reconciliation(bound, effected, preimage)
        assert excinfo.value.reason_code == "lost_preparation_identity"


def _corrected_changed_spec() -> object:
    """Build a full content mutation across objects, worktree, index, and refs."""
    import hashlib

    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    baseline = base.baseline

    def parent(
        namespace: str,
        path: bytes,
        filesystem_id: str,
        node_id: str,
    ) -> object:
        return module.ParentBinding(
            module.Location(namespace, path),
            module.ExistingNode(module.NodeIdentity(filesystem_id, node_id)),
        )

    working_parent = parent("working", b"root-root", "fs-working", "root-root")
    admin_parent = parent("admin", b"admin-root", "fs-admin", "admin-root")
    refs_parent = parent("admin", b"refs/heads", "fs-admin", "refs-heads-parent")
    reflogs_parent = parent("admin", b"logs/refs/heads", "fs-admin", "reflogs-heads-parent")
    admin_preparation_parent = parent("private", b"prepare", "fs-admin", "prepare-parent")
    working_preparation_parent = parent(
        "private", b"prepare-working", "fs-working", "prepare-working-parent"
    )
    object_parent = parent("admin", b"objects/33", "fs-admin", "object-parent-33")
    working_destination = module.Location("private", b"prepare-working/working-new")
    working_slot = module.PreparationSlot(
        "working-new",
        working_destination,
        working_preparation_parent,
        working_parent,
        module.AbsentState(working_destination),
        "file",
        b"changed",
        False,
    )
    index_destination = module.Location("private", b"prepare/index-new")
    index_slot = module.PreparationSlot(
        "index-new",
        index_destination,
        admin_preparation_parent,
        admin_parent,
        module.AbsentState(index_destination),
        "file",
        b"new-index",
        False,
    )
    loose_destination = module.Location("private", b"prepare/loose-new")
    loose_slot = module.PreparationSlot(
        "loose-new",
        loose_destination,
        admin_preparation_parent,
        refs_parent,
        module.AbsentState(loose_destination),
        "file",
        ("3" * 40).encode("ascii"),
        False,
    )
    reflog_destination = module.Location("private", b"prepare/reflog-new")
    reflog_slot = module.PreparationSlot(
        "reflog-new",
        reflog_destination,
        admin_preparation_parent,
        reflogs_parent,
        module.AbsentState(reflog_destination),
        "file",
        b"new-log",
        False,
    )
    object_destination = module.Location("private", b"prepare/commit-new")
    object_slot = module.PreparationSlot(
        "commit-new",
        object_destination,
        admin_preparation_parent,
        object_parent,
        module.AbsentState(object_destination),
        "file",
        b"commit-three-storage",
        False,
    )
    desired_manifest = (_file(b"keep.txt", b"changed"),)
    desired_working = (
        module.DesiredNodeState(
            module.Location("working", b"keep.txt"),
            module.PreparedNode("working-new"),
            "file",
            b"changed",
            False,
            1,
            False,
        ),
    )
    desired_index = module.DesiredIndexState(
        module.DesiredNodeState(
            baseline.index.storage.location,
            module.PreparedNode("index-new"),
            "file",
            b"new-index",
            False,
            1,
            False,
        ),
        baseline.index.entries,
        False,
    )
    refs = base.desired.refs
    desired_refs = replace(
        refs,
        resolved_oid="3" * 40,
        loose_ref=module.DesiredNodeState(
            baseline.refs.loose_ref.location,
            module.PreparedNode("loose-new"),
            "file",
            ("3" * 40).encode("ascii"),
            False,
            1,
            False,
        ),
        reflogs=(
            module.DesiredNodeState(
                baseline.refs.reflogs[0].location,
                module.PreparedNode("reflog-new"),
                "file",
                b"new-log",
                False,
                1,
                False,
            ),
        ),
    )
    desired = replace(
        base.desired,
        index=desired_index,
        refs=desired_refs,
        manifest=desired_manifest,
        working_nodes=desired_working,
    )
    quarantine_sources = (
        ("working-old", baseline.working_nodes[0], working_parent),
        ("index-old", baseline.index.storage, admin_parent),
        ("loose-old", baseline.refs.loose_ref, refs_parent),
        ("reflog-old", baseline.refs.reflogs[0], reflogs_parent),
    )
    quarantine = tuple(
        module.QuarantineSlot(
            slot_id,
            source.location,
            source.identity,
            source_parent,
            module.Location(
                "excluded",
                f"quarantine-{source.identity.filesystem_id}/{slot_id}".encode("ascii"),
            ),
            parent(
                "excluded",
                f"quarantine-{source.identity.filesystem_id}".encode("ascii"),
                source.identity.filesystem_id,
                f"quarantine-parent-{source.identity.filesystem_id}",
            ),
            None,
            module.AbsentState(
                module.Location(
                    "excluded",
                    f"quarantine-{source.identity.filesystem_id}/{slot_id}".encode("ascii"),
                )
            ),
        )
        for slot_id, source, source_parent in quarantine_sources
    )
    required = base.required_objects + (
        module.RequiredObject(
            "3" * 40,
            "commit",
            len(object_slot.desired_content),
            "sha256:" + hashlib.sha256(b"semantic-commit-three").hexdigest(),
            object_slot.slot_id,
        ),
    )
    return replace(
        base,
        desired=desired,
        preparation_slots=base.preparation_slots
        + (working_slot, index_slot, loose_slot, reflog_slot, object_slot),
        quarantine_slots=quarantine,
        required_objects=required,
    )


def test_replacing_directory_quarantines_and_restores_unchanged_descendants() -> None:
    """Directory replacement expands to descendants without fabricating child identities."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    directory_entry = _file(b"tree", content=b"", kind="directory", nlink=2)
    child_entry = _file(b"tree/child", content=b"child")
    directory = module.NodeState(
        module.Location("working", b"tree"),
        module.NodeIdentity("fs-working", "old-directory"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    child = module.NodeState(
        module.Location("working", b"tree/child"),
        module.NodeIdentity("fs-working", "old-child"),
        "file",
        b"child",
        False,
        1,
        False,
    )
    working_parent = module.ParentBinding(
        module.Location("working", b"root-root"),
        module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
    )
    preparation_parent = module.ParentBinding(
        module.Location("private", b"prepare-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "prepare-working-parent")),
    )
    directory_destination = module.Location("private", b"prepare-working/new-directory")
    directory_slot = module.PreparationSlot(
        "new-directory",
        directory_destination,
        preparation_parent,
        working_parent,
        module.AbsentState(directory_destination),
        "directory",
        b"",
        False,
    )
    desired_directory = module.DesiredNodeState(
        directory.location,
        module.PreparedNode(directory_slot.slot_id),
        "directory",
        b"",
        False,
        2,
        False,
    )
    desired_child = module.DesiredNodeState(
        child.location,
        module.ExistingNode(child.identity),
        "file",
        b"child",
        False,
        1,
        False,
    )
    quarantine_parent = module.ParentBinding(
        module.Location("excluded", b"quarantine-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "quarantine-working-parent")),
    )
    source_parents = {
        "directory": working_parent,
        "child": module.ParentBinding(
            directory.location,
            module.ExistingNode(directory.identity),
        ),
    }
    restoration_parents = {
        "directory": None,
        "child": module.ParentBinding(
            directory.location,
            module.PreparedNode(directory_slot.slot_id),
        ),
    }
    quarantine = tuple(
        module.QuarantineSlot(
            f"quarantine-{name}",
            node.location,
            node.identity,
            source_parents[name],
            module.Location("excluded", f"quarantine-working/{name}".encode("ascii")),
            quarantine_parent,
            restoration_parents[name],
            module.AbsentState(
                module.Location("excluded", f"quarantine-working/{name}".encode("ascii"))
            ),
        )
        for name, node in (("directory", directory), ("child", child))
    )
    spec = replace(
        base,
        baseline=replace(
            base.baseline,
            manifest=(directory_entry, child_entry),
            working_nodes=(directory, child),
        ),
        desired=replace(
            base.desired,
            manifest=(directory_entry, child_entry),
            working_nodes=(desired_directory, desired_child),
        ),
        preparation_slots=base.preparation_slots + (directory_slot,),
        quarantine_slots=quarantine,
    )
    bound = module.bind_specification(spec)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    working_actions = [
        action
        for action in plan.actions
        if action.effect_kind
        in {
            "quarantine_working_node",
            "install_working_node",
            "restore_working_node",
        }
    ]

    assert [action.prepared_slot_id for action in working_actions] == [
        "quarantine-child",
        "quarantine-directory",
        "new-directory",
        "quarantine-child",
    ]
    assert [action.effect_kind for action in working_actions] == [
        "quarantine_working_node",
        "quarantine_working_node",
        "install_working_node",
        "restore_working_node",
    ]
    assert plan.content_mutation_count == 4


def test_every_action_kind_has_an_approved_application_or_broker_owner() -> None:
    """No protocol action invents filesystem as an independent authority."""
    import yinshi.services.workspace_replica as module

    expected_broker_effects = {
        "accept_broker_intent",
        "create_ownership_marker",
        "record_marker_exclusion",
        "close_admission_gate",
        "record_drain",
        "materialize_preparation",
        "promote_required_object",
        "quarantine_working_node",
        "install_working_node",
        "quarantine_index",
        "install_index",
        "quarantine_reflog",
        "install_reflog",
        "quarantine_selected_ref",
        "install_selected_ref",
        "publish_generation",
        "acknowledge_destination_binding",
        "remove_ownership_marker",
        "physical_release",
        "admit_readers",
    }
    plans = []
    for spec in (_corrected_spec(), _corrected_changed_spec()):
        bound = module.bind_specification(spec)
        plans.append(
            module.plan_reconciliation(
                bound,
                _corrected_initial_observation(bound),
                _corrected_retained_inputs(bound),
            )
        )

    actions = tuple(action for plan in plans for action in plan.actions)
    assert {action.owner for action in actions} <= {"application", "broker"}
    assert all(
        action.owner == "broker"
        for action in actions
        if action.effect_kind in expected_broker_effects
    )
    assert expected_broker_effects <= {action.effect_kind for action in actions}


def test_plan_models_object_working_index_and_ref_transitions_in_safe_order() -> None:
    """Mutation plan carries exact physical and logical transitions through stages 7 to 11."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    kinds = [action.effect_kind for action in plan.actions]

    assert "promote_required_object" in kinds
    assert "quarantine_working_node" in kinds
    assert kinds.index("quarantine_working_node") < kinds.index("install_working_node")
    assert kinds.index("quarantine_index") < kinds.index("install_index")
    assert kinds.index("quarantine_reflog") < kinds.index("install_reflog")
    assert kinds.index("install_reflog") < kinds.index("quarantine_selected_ref")
    assert kinds.index("quarantine_selected_ref") < kinds.index("install_selected_ref")
    assert all(
        action.owner == "broker"
        for action in plan.actions
        if 7 <= action.stage <= 11
        and action.effect_kind.startswith(("promote", "quarantine", "install"))
    )


def test_administrative_absence_empty_file_and_packed_fallback_stay_distinct() -> None:
    """Administrative storage models absence, empty files, and packed fallback independently."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    packed_node = module.NodeState(
        base.baseline.refs.packed_refs.location,
        module.NodeIdentity("fs-admin", "packed-refs"),
        "file",
        b"packed-ref-data",
        False,
        1,
        False,
    )
    fallback_baseline_refs = replace(
        base.baseline.refs,
        packed_fallback_oid="2" * 40,
        packed_refs=packed_node,
    )
    fallback_desired_refs = replace(
        base.desired.refs,
        packed_fallback_oid="2" * 40,
        packed_refs=module.DesiredNodeState(
            packed_node.location,
            module.ExistingNode(packed_node.identity),
            "file",
            packed_node.content,
            False,
            1,
            False,
        ),
        loose_ref=module.AbsentState(base.baseline.refs.loose_ref.location),
    )
    loose = base.baseline.refs.loose_ref
    quarantine_destination = module.Location("excluded", b"quarantine-admin/loose-fallback")
    quarantine = module.QuarantineSlot(
        "loose-fallback",
        loose.location,
        loose.identity,
        module.ParentBinding(
            module.Location("admin", b"refs/heads"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "refs-heads-parent")),
        ),
        quarantine_destination,
        module.ParentBinding(
            module.Location("excluded", b"quarantine-admin"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "quarantine-admin-parent")),
        ),
        None,
        module.AbsentState(quarantine_destination),
    )
    fallback = replace(
        base,
        baseline=replace(base.baseline, refs=fallback_baseline_refs),
        desired=replace(base.desired, refs=fallback_desired_refs),
        quarantine_slots=(quarantine,),
    )
    bound = module.bind_specification(fallback)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    kinds = [action.effect_kind for action in plan.actions]
    assert "quarantine_selected_ref" in kinds
    assert "select_packed_fallback" in kinds
    assert "install_selected_ref" not in kinds

    empty_node = module.NodeState(
        module.Location("admin", b"MERGE_HEAD"),
        module.NodeIdentity("fs-admin", "merge-head"),
        "file",
        b"",
        False,
        1,
        False,
    )
    empty = replace(
        base,
        baseline=replace(
            base.baseline,
            refs=replace(base.baseline.refs, unrelated_metadata=(empty_node,)),
        ),
        desired=replace(
            base.desired,
            refs=replace(
                base.desired.refs,
                unrelated_metadata=(
                    module.DesiredNodeState(
                        empty_node.location,
                        module.ExistingNode(empty_node.identity),
                        "file",
                        b"",
                        False,
                        1,
                        False,
                    ),
                ),
            ),
        ),
    )
    absent = replace(
        base,
        baseline=replace(
            base.baseline,
            refs=replace(
                base.baseline.refs,
                unrelated_metadata=(module.AbsentState(empty_node.location),),
            ),
        ),
        desired=replace(
            base.desired,
            refs=replace(
                base.desired.refs,
                unrelated_metadata=(module.AbsentState(empty_node.location),),
            ),
        ),
    )
    assert (
        module.bind_specification(empty).fingerprint
        != module.bind_specification(absent).fingerprint
    )


def _corrected_preparation_facts(bound: object) -> tuple[object, ...]:
    """Create deterministic concrete identities for every approved preparation slot."""
    import yinshi.services.workspace_replica as module

    parents = {slot.slot_id: slot.materialization_parent for slot in bound.spec.preparation_slots}
    parents.update({marker.slot_id: marker.parent for marker in bound.spec.ownership_markers})
    return tuple(
        module.PreparationFact(
            bound.fingerprint,
            slot_id,
            module.NodeIdentity(
                parent.reference.identity.filesystem_id,
                f"prepared-{slot_id}",
            ),
            f"creation-{slot_id}",
            _corrected_preparation_position(bound, slot_id),
        )
        for slot_id, parent in sorted(parents.items())
    )


def _corrected_journal_position(
    bound: object,
    sequence: int,
    label: str,
) -> object:
    """Create one unique position from the bound append-only journal."""
    import yinshi.services.workspace_replica as module

    return module.JournalPosition(
        bound.spec.identity.operation_id,
        sequence,
        f"journal-append-{label}-{sequence}",
    )


def _corrected_preparation_position(bound: object, slot_id: str) -> object:
    """Assign a preparation position from its protocol action order."""
    import yinshi.services.workspace_replica as module

    plan = module._derive_protocol_plan(bound)
    action_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.creates_prepared_identity and action.prepared_slot_id == slot_id
    )
    sequence = action_index * 1_000 + 100
    return _corrected_journal_position(bound, sequence, f"preparation-{slot_id}")


def _corrected_synchronization_position(
    bound: object,
    plan: object,
    action: object,
    requirement: str,
    preparations: tuple[object, ...] = (),
) -> object:
    """Assign one unique position after all preparation positions."""
    import yinshi.services.workspace_replica as module

    action_index = plan.actions.index(action)
    requirements = module.resolve_synchronization_requirements(action, preparations)
    requirement_index = requirements.index(requirement)
    sequence = action_index * 1_000 + 200 + requirement_index
    return _corrected_journal_position(
        bound,
        sequence,
        f"synchronization-{action_index}-{requirement_index}",
    )


def _corrected_creation_receipt(action: object, preparations: tuple[object, ...]) -> str | None:
    """Return the creation receipt that a synchronization must follow."""
    if not action.creates_prepared_identity:
        return None
    return next(
        fact.creation_receipt_id for fact in preparations if fact.slot_id == action.prepared_slot_id
    )


def _corrected_completed_journal(
    bound: object,
    plan: object,
    retained: object,
    count: int,
) -> object:
    """Build an exact durable prefix using independent concrete observation hashes."""
    import yinshi.services.workspace_replica as module

    all_preparations = _corrected_preparation_facts(bound)
    created = {
        action.prepared_slot_id
        for action in plan.actions[:count]
        if action.creates_prepared_identity
    }
    preparations = tuple(fact for fact in all_preparations if fact.slot_id in created)
    intents = tuple(
        module.IntentFact(bound.fingerprint, action.action_id, f"intent-{index}")
        for index, action in enumerate(plan.actions[:count])
    )
    synchronizations = tuple(
        module.SynchronizationFact(
            bound.fingerprint,
            action.action_id,
            requirement,
            f"sync-{index}-{requirement}",
            _corrected_synchronization_position(
                bound,
                plan,
                action,
                requirement,
                preparations,
            ),
            _corrected_creation_receipt(action, preparations),
        )
        for index, action in enumerate(plan.actions[:count])
        for requirement in module.resolve_synchronization_requirements(
            action,
            preparations,
        )
    )
    completions = tuple(
        module.CompletionFact(
            bound.fingerprint,
            action.action_id,
            module.observation_fingerprint(
                module.materialize_expected_observation(action.completion_postimage, preparations)
            ),
            f"completion-{index}",
        )
        for index, action in enumerate(plan.actions[:count])
    )
    return module.ReconciliationJournal(
        retained,
        intents,
        preparations,
        synchronizations,
        completions,
    )


def test_replay_refuses_foreign_facts_malformed_plans_and_lost_records() -> None:
    """Replay fails closed for every authority, plan-integrity, and fresh-state conflict."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    initial = _corrected_initial_observation(bound)
    plan = module.plan_reconciliation(bound, initial, retained)
    empty = module.ReconciliationJournal(retained, (), (), (), ())

    malformed_plans = (
        replace(plan, actions=()),
        replace(plan, actions=tuple(reversed(plan.actions))),
        replace(plan, actions=plan.actions + (plan.actions[-1],)),
        replace(
            plan,
            actions=(replace(plan.actions[0], fingerprint="sha256:" + "0" * 64),)
            + plan.actions[1:],
        ),
    )
    for malformed in malformed_plans:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.replay_reconciliation(bound, empty, initial, cached_plan=malformed)
        assert excinfo.value.reason_code == "invalid_plan_integrity"

    first_postimage = module.materialize_expected_observation(
        plan.actions[0].permitted_effect_postimages[0],
        (),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, empty, first_postimage)
    assert excinfo.value.reason_code == "foreign_replay_state"

    retained_conflicts = (
        (
            replace(retained, acknowledgment=replace(retained.acknowledgment, receipt_id="wrong")),
            "acknowledgment_unavailable",
        ),
        (
            replace(retained, drain=replace(retained.drain, replica_generation=4)),
            "drain_unverified",
        ),
        (
            replace(
                retained,
                export=replace(retained.export, specification_fingerprint="sha256:" + "0" * 64),
            ),
            "export_stale",
        ),
        (
            replace(retained, inventory=replace(retained.inventory, destination_object_ids=())),
            "inventory_stale",
        ),
    )
    for conflicting, reason_code in retained_conflicts:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.plan_reconciliation(bound, initial, conflicting)
        assert excinfo.value.reason_code == reason_code

    foreign_intent = module.IntentFact(
        "sha256:" + "0" * 64,
        plan.actions[0].action_id,
        "foreign-intent",
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(
            bound,
            replace(empty, intents=(foreign_intent,)),
            initial,
        )
    assert excinfo.value.reason_code == "foreign_journal"

    stale_replica = replace(
        initial, replica_state=replace(initial.replica_state, replica_generation=4)
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(bound, stale_replica, retained)
    assert excinfo.value.reason_code == "baseline_conflict"
    changed_boundary = replace(
        initial.boundaries[0],
        identity=module.NodeIdentity(initial.boundaries[0].identity.filesystem_id, "changed-root"),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            bound,
            replace(initial, boundaries=(changed_boundary,) + initial.boundaries[1:]),
            retained,
        )
    assert excinfo.value.reason_code == "protected_boundary_changed"


def test_physical_and_logical_release_complete_independently_before_admission() -> None:
    """Reader admission remains impossible until both release owners complete in order."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        retained,
    )
    physical_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "physical_release"
    )
    logical_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "logical_release"
    )
    admission_index = next(
        index for index, action in enumerate(plan.actions) if action.effect_kind == "admit_readers"
    )
    assert physical_index + 1 == logical_index
    assert logical_index + 1 == admission_index

    physical_journal = _corrected_completed_journal(
        bound,
        plan,
        retained,
        physical_index + 1,
    )
    physical_observation = module.materialize_expected_observation(
        plan.actions[physical_index].completion_postimage,
        physical_journal.preparations,
    )
    assert physical_observation.control.physical_release is not None
    assert physical_observation.control.physical_release.owner == "broker"
    assert physical_observation.control.logical_release is None
    assert physical_observation.control.admission is None
    physical_decision = module.replay_reconciliation(
        bound,
        physical_journal,
        physical_observation,
    )
    assert physical_decision.kind == "record_intent"
    assert physical_decision.action.effect_kind == "logical_release"

    logical_journal = _corrected_completed_journal(
        bound,
        plan,
        retained,
        logical_index + 1,
    )
    logical_observation = module.materialize_expected_observation(
        plan.actions[logical_index].completion_postimage,
        logical_journal.preparations,
    )
    assert logical_observation.control.physical_release is not None
    assert logical_observation.control.physical_release.owner == "broker"
    assert logical_observation.control.logical_release is not None
    assert logical_observation.control.logical_release.owner == "application"
    assert logical_observation.control.admission is None
    logical_decision = module.replay_reconciliation(
        bound,
        logical_journal,
        logical_observation,
    )
    assert logical_decision.kind == "record_intent"
    assert logical_decision.action.effect_kind == "admit_readers"


def test_binding_requires_exact_preparation_quarantine_and_object_locations() -> None:
    """Transition inventories must explain every changed node and exact object destination."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    working_slot_index = next(
        index
        for index, slot in enumerate(changed.preparation_slots)
        if slot.slot_id == "working-new"
    )
    bad_preparation_slots = list(changed.preparation_slots)
    bad_preparation_slots[working_slot_index] = replace(
        bad_preparation_slots[working_slot_index],
        desired_content=b"wrong-working-bytes",
    )
    first_destination = changed.destination_objects[0]
    misplaced_storage = replace(
        first_destination.storage,
        location=module.Location("admin", b"objects/wrong/location"),
    )
    misplaced_destination = replace(first_destination, storage=misplaced_storage)
    extra_location = module.Location("excluded", b"quarantine/unexplained")
    working_quarantine = next(
        slot for slot in changed.quarantine_slots if slot.slot_id == "working-old"
    )
    extra_quarantine = module.QuarantineSlot(
        "unexplained",
        changed.baseline.working_nodes[0].location,
        changed.baseline.working_nodes[0].identity,
        working_quarantine.source_parent,
        extra_location,
        module.ParentBinding(
            module.Location("excluded", b"quarantine"),
            module.ExistingNode(module.NodeIdentity("fs-working", "extra-parent")),
        ),
        None,
        module.AbsentState(extra_location),
    )
    cases = (
        (
            "missing-quarantine",
            replace(changed, quarantine_slots=changed.quarantine_slots[1:]),
            "missing_quarantine_slot",
        ),
        (
            "preparation-bytes",
            replace(changed, preparation_slots=tuple(bad_preparation_slots)),
            "preparation_content_mismatch",
        ),
        (
            "object-location",
            replace(
                changed,
                destination_objects=(misplaced_destination,) + changed.destination_objects[1:],
            ),
            "object_identity_conflict",
        ),
        (
            "extra-quarantine",
            replace(changed, quarantine_slots=changed.quarantine_slots + (extra_quarantine,)),
            "unexplained_quarantine_slot",
        ),
        (
            "transition-limit-before-expansion",
            replace(changed, limits=replace(changed.limits, max_transitions=35)),
            "inventory_limit_exceeded",
        ),
    )
    for label, candidate, reason_code in cases:
        with pytest.raises(SnapshotRejected) as excinfo:
            module.bind_specification(candidate)
        assert excinfo.value.reason_code == reason_code, label


def test_binding_rejects_quarantine_destination_equal_to_its_source() -> None:
    """A quarantine move onto its own source location is a contradictory transition."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    working_quarantine = next(
        slot for slot in changed.quarantine_slots if slot.slot_id == "working-old"
    )
    self_quarantine = replace(
        working_quarantine,
        destination=working_quarantine.source,
        initial=module.AbsentState(working_quarantine.source),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(changed, quarantine_slots=(self_quarantine,) + changed.quarantine_slots[1:])
        )
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_required_absent_destination_occupied_in_another_section() -> None:
    """One physical location cannot be required-absent and present in the same binding."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    marker = base.ownership_markers[0]
    occupied = module.NodeState(
        marker.destination,
        module.NodeIdentity("fs-private", "occupied-alias"),
        "file",
        b"occupied",
        False,
        1,
        False,
    )
    baseline = replace(
        base.baseline,
        refs=replace(base.baseline.refs, unrelated_metadata=(occupied,)),
    )
    desired_unrelated = (
        module.DesiredNodeState(
            occupied.location,
            module.ExistingNode(occupied.identity),
            occupied.kind,
            occupied.content,
            occupied.executable,
            occupied.nlink,
            occupied.privilege_bits,
        ),
    )
    desired = replace(
        base.desired,
        refs=replace(base.desired.refs, unrelated_metadata=desired_unrelated),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(base, baseline=baseline, desired=desired))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_orphan_protected_boundary_ancestor_chain() -> None:
    """A protected ancestor chain must connect through to its boundary location."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    detached = module.NodeState(
        module.Location("private", b"detached"),
        module.NodeIdentity("fs-private", "private-detached"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    boundaries = list(candidate.boundaries)
    private_index = next(
        index for index, boundary in enumerate(boundaries) if boundary.role == "private"
    )
    boundaries[private_index] = replace(boundaries[private_index], ancestors=(detached,))

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, boundaries=tuple(boundaries)))
    assert excinfo.value.reason_code == "incomplete_topology"


def test_binding_rejects_reordered_protected_boundary_ancestor_chain() -> None:
    """Protected ancestors must stay ordered outermost to innermost."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    outer = module.NodeState(
        module.Location("admin", b"admin-root"),
        module.NodeIdentity("fs-admin", "admin-outer"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    inner = module.NodeState(
        module.Location("admin", b"admin-root/nested"),
        module.NodeIdentity("fs-admin", "admin-inner"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    boundaries = list(candidate.boundaries)
    admin_index = next(
        index for index, boundary in enumerate(boundaries) if boundary.role == "admin"
    )
    boundaries[admin_index] = replace(
        boundaries[admin_index],
        location=module.Location("admin", b"admin-root/nested/deep"),
        ancestors=(inner, outer),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, boundaries=tuple(boundaries)))
    assert excinfo.value.reason_code == "incomplete_topology"


def test_binding_rejects_prepared_final_target_inside_protected_topology() -> None:
    """A prepared node cannot be installed onto a protected boundary location."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    candidate = replace(
        candidate,
        desired=replace(
            candidate.desired,
            working_nodes=(
                module.DesiredNodeState(
                    module.Location("working", b"root-root"),
                    module.PreparedNode("working-new"),
                    "file",
                    b"changed",
                    False,
                    1,
                    False,
                ),
            ),
            manifest=(_file(b"root-root", b"changed"),),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "protected_topology_conflict"


def test_binding_rejects_missing_object_promotion_into_protected_boundary() -> None:
    """A generated object destination cannot occupy a protected boundary location."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    promoted = candidate.required_objects[0].object_id
    generated = module.Location("admin", f"objects/{promoted[:2]}/{promoted[2:]}".encode("ascii"))
    boundaries = list(candidate.boundaries)
    admin_index = next(
        index for index, boundary in enumerate(boundaries) if boundary.role == "admin"
    )
    boundaries[admin_index] = replace(boundaries[admin_index], location=generated)

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                candidate,
                boundaries=tuple(boundaries),
                destination_objects=candidate.destination_objects[1:],
            )
        )
    assert excinfo.value.reason_code == "duplicate_location"


def test_binding_rejects_protected_identity_at_alternate_mutation_location() -> None:
    """A protected ancestor identity cannot label destination mutation storage."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    protected_ancestor = module.NodeState(
        module.Location("admin", b"admin-protected"),
        module.NodeIdentity("fs-admin", "aliased-protected"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    boundaries = list(candidate.boundaries)
    admin_index = next(
        index for index, boundary in enumerate(boundaries) if boundary.role == "admin"
    )
    boundaries[admin_index] = replace(boundaries[admin_index], ancestors=(protected_ancestor,))
    aliased = replace(
        candidate.destination_objects[0],
        storage=replace(
            candidate.destination_objects[0].storage,
            identity=protected_ancestor.identity,
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                candidate,
                boundaries=tuple(boundaries),
                destination_objects=(aliased, candidate.destination_objects[1]),
            )
        )
    assert excinfo.value.reason_code == "ambiguous_node_identity"


def test_binding_rejects_unrelated_same_filesystem_marker_parent() -> None:
    """A marker parent must be the immediate entry-owning directory."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    marker = replace(
        candidate.ownership_markers[0],
        parent=module.ParentBinding(
            module.Location("private", b"unrelated"),
            module.ExistingNode(module.NodeIdentity("fs-private", "unrelated-parent")),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, ownership_markers=(marker,)))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_wrong_top_level_quarantine_source_parent() -> None:
    """A top-level entry must bind its protected namespace root as source parent."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    quarantine = list(candidate.quarantine_slots)
    working_index = next(
        index for index, slot in enumerate(quarantine) if slot.slot_id == "working-old"
    )
    quarantine[working_index] = replace(
        quarantine[working_index],
        source_parent=module.ParentBinding(
            module.Location("working", b"unrelated"),
            module.ExistingNode(module.NodeIdentity("fs-working", "unrelated-parent")),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, quarantine_slots=tuple(quarantine)))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_wrong_installation_parent_for_working_install() -> None:
    """A prepared working install must target its entry-owning directory."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    slots_list = list(candidate.preparation_slots)
    working_index = next(
        index for index, slot in enumerate(slots_list) if slot.slot_id == "working-new"
    )
    slots_list[working_index] = replace(
        slots_list[working_index],
        installation_parent=module.ParentBinding(
            module.Location("working", b"unrelated"),
            module.ExistingNode(module.NodeIdentity("fs-working", "unrelated-parent")),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, preparation_slots=tuple(slots_list)))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_wrong_nested_promotion_installation_parent() -> None:
    """A promoted object must install under its exact object-shard parent."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    slots_list = list(candidate.preparation_slots)
    object_index = next(
        index for index, slot in enumerate(slots_list) if slot.slot_id == "commit-new"
    )
    slots_list[object_index] = replace(
        slots_list[object_index],
        installation_parent=module.ParentBinding(
            module.Location("admin", b"objects/wrong"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "wrong-object-parent")),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, preparation_slots=tuple(slots_list)))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_wrong_restoration_parent_location() -> None:
    """A restoration parent must own the restored entry location."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    quarantine = list(candidate.quarantine_slots)
    working_index = next(
        index for index, slot in enumerate(quarantine) if slot.slot_id == "working-old"
    )
    quarantine[working_index] = replace(
        quarantine[working_index],
        restoration_parent=module.ParentBinding(
            module.Location("working", b"unrelated"),
            module.ExistingNode(module.NodeIdentity("fs-working", "unrelated-parent")),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(candidate, quarantine_slots=tuple(quarantine)))
    assert excinfo.value.reason_code == "invalid_specification"


def test_top_level_entries_bind_to_protected_namespace_root() -> None:
    """Top-level working entries must observe the protected root boundary parent."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    root_binding = module.ParentBinding(
        module.Location("working", b"root-root"),
        module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
    )
    assert root_binding in module._corrected_initial_parent_bindings(bound.spec)


def test_same_parent_actions_deduplicate_into_one_parent_binding() -> None:
    """Actions sharing one parent directory deduplicate to a single binding."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    second_marker = replace(
        candidate.ownership_markers[0],
        slot_id="ownership-marker-2",
        destination=module.Location("private", b"markers/operation-2"),
        initial=module.AbsentState(module.Location("private", b"markers/operation-2")),
    )
    bound = module.bind_specification(
        replace(candidate, ownership_markers=(candidate.ownership_markers[0], second_marker))
    )
    shared = [
        binding
        for binding in module._corrected_initial_parent_bindings(bound.spec)
        if binding.location == module.Location("private", b"markers")
    ]
    assert len(shared) == 1
    assert shared[0].reference == module.ExistingNode(
        module.NodeIdentity("fs-private", "marker-parent")
    )


def test_binding_rejects_semantic_index_change_with_unchanged_storage() -> None:
    """Index semantics cannot change without a physical storage transition."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    desired_index = replace(
        candidate.desired.index,
        entries=(
            module.IndexEntry(
                b"other.txt",
                0,
                "100644",
                "1" * 40,
                False,
                False,
                False,
            ),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(candidate, desired=replace(candidate.desired, index=desired_index))
        )
    assert excinfo.value.reason_code == "invalid_index_semantics"


def test_binding_rejects_packed_fallback_change_with_unchanged_storage() -> None:
    """Packed fallback semantics cannot change without packed-ref storage."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    desired_refs = replace(candidate.desired.refs, packed_fallback_oid="1" * 40)

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(candidate, desired=replace(candidate.desired, refs=desired_refs))
        )
    assert excinfo.value.reason_code == "invalid_ref_semantics"


def test_new_reflog_absence_rejects_but_prepared_reflog_binds() -> None:
    """A new reflog must have a physical installation transition."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    reflog_location = module.Location("admin", b"logs/refs/heads/feature")
    absent_refs = replace(
        candidate.desired.refs,
        reflogs=candidate.desired.refs.reflogs + (module.AbsentState(reflog_location),),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(candidate, desired=replace(candidate.desired, refs=absent_refs))
        )
    assert excinfo.value.reason_code == "invalid_ref_semantics"

    preparation_location = module.Location("private", b"prepare/reflog-feature")
    preparation = module.PreparationSlot(
        "reflog-feature",
        preparation_location,
        module.ParentBinding(
            module.Location("private", b"prepare"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "prepare-parent")),
        ),
        module.ParentBinding(
            module.Location("admin", b"logs/refs/heads"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "reflogs-heads-parent")),
        ),
        module.AbsentState(preparation_location),
        "file",
        b"feature-log",
        False,
    )
    desired_reflog = module.DesiredNodeState(
        reflog_location,
        module.PreparedNode("reflog-feature"),
        "file",
        b"feature-log",
        False,
        1,
        False,
    )
    prepared_refs = replace(
        candidate.desired.refs,
        reflogs=candidate.desired.refs.reflogs + (desired_reflog,),
    )
    bound = module.bind_specification(
        replace(
            candidate,
            preparation_slots=candidate.preparation_slots + (preparation,),
            desired=replace(candidate.desired, refs=prepared_refs),
        )
    )
    assert desired_reflog in bound.spec.desired.refs.reflogs


@pytest.mark.parametrize("invalid_kind", ["directory", "executable"])
def test_binding_rejects_non_regular_destination_object_storage(
    invalid_kind: str,
) -> None:
    """Durable object storage must be a regular non-executable file."""
    import hashlib

    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    destination = candidate.destination_objects[0]
    required = candidate.required_objects[0]
    if invalid_kind == "directory":
        storage = replace(
            destination.storage,
            kind="directory",
            content=b"",
            executable=False,
            nlink=2,
        )
        content_digest = "sha256:" + hashlib.sha256(b"").hexdigest()
        destination = replace(destination, storage=storage, content_sha256=content_digest)
        required = replace(required, byte_length=0, content_sha256=content_digest)
    else:
        destination = replace(
            destination,
            storage=replace(destination.storage, executable=True),
        )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                candidate,
                destination_objects=(destination,) + candidate.destination_objects[1:],
                required_objects=(required,) + candidate.required_objects[1:],
            )
        )
    assert excinfo.value.reason_code == "invalid_node_state"


@pytest.mark.parametrize("invalid_storage", ["index-executable", "reflog-directory"])
def test_binding_rejects_non_regular_administrative_storage(
    invalid_storage: str,
) -> None:
    """Administrative storage must be a regular non-executable file."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    if invalid_storage == "index-executable":
        baseline_storage = replace(candidate.baseline.index.storage, executable=True)
        desired_storage = module.DesiredNodeState(
            baseline_storage.location,
            module.ExistingNode(baseline_storage.identity),
            "file",
            baseline_storage.content,
            True,
            1,
            False,
        )
        candidate = replace(
            candidate,
            baseline=replace(
                candidate.baseline,
                index=replace(candidate.baseline.index, storage=baseline_storage),
            ),
            desired=replace(
                candidate.desired,
                index=replace(candidate.desired.index, storage=desired_storage),
            ),
        )
    else:
        baseline_reflog = replace(
            candidate.baseline.refs.reflogs[0],
            kind="directory",
            content=b"",
            nlink=2,
        )
        desired_reflog = module.DesiredNodeState(
            baseline_reflog.location,
            module.ExistingNode(baseline_reflog.identity),
            "directory",
            b"",
            False,
            2,
            False,
        )
        candidate = replace(
            candidate,
            baseline=replace(
                candidate.baseline,
                refs=replace(candidate.baseline.refs, reflogs=(baseline_reflog,)),
            ),
            desired=replace(
                candidate.desired,
                refs=replace(candidate.desired.refs, reflogs=(desired_reflog,)),
            ),
        )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_node_state"


@pytest.mark.parametrize(
    "acknowledgment",
    ["acknowledge_working_install", "acknowledge_index"],
)
def test_acknowledgment_cannot_assign_unreached_physical_state(
    acknowledgment: str,
) -> None:
    """An acknowledgment must assert its physical precondition."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_changed_spec()
    image = module._corrected_initial_expected(candidate)

    with pytest.raises(SnapshotRejected) as excinfo:
        module._corrected_advance_expected(
            image,
            candidate,
            "application",
            acknowledgment,
            None,
            None,
        )
    assert excinfo.value.reason_code == "invalid_plan_integrity"


def test_generation_publication_changes_only_generation() -> None:
    """Generation publication cannot replace unrelated replica fields."""
    import yinshi.services.workspace_replica as module

    candidate = _corrected_spec()
    divergent = replace(
        candidate.baseline,
        working_nodes=(replace(candidate.baseline.working_nodes[0], content=b"divergent"),),
    )
    image = replace(module._corrected_initial_expected(candidate), replica_state=divergent)

    published = module._corrected_advance_expected(
        image,
        candidate,
        "broker",
        "publish_generation",
        None,
        None,
    )

    assert published.replica_state.generation == candidate.desired.generation
    assert published.replica_state.replica_generation == divergent.replica_generation
    assert published.replica_state.working_nodes == divergent.working_nodes
    assert published.replica_state.index == divergent.index
    assert published.replica_state.refs == divergent.refs


def test_generation_publication_is_broker_owned_before_application_binding() -> None:
    """Broker publishes physical generation before application commits destination binding."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    publication = next(
        action for action in plan.actions if action.effect_kind == "publish_generation"
    )
    binding = next(
        action for action in plan.actions if action.effect_kind == "commit_destination_binding"
    )

    assert publication.stage == 13
    assert publication.owner == "broker"
    assert publication.completion_postimage.control.publication.owner == "broker"
    assert binding.stage == 14
    assert binding.owner == "application"
    assert publication.completion_postimage.replica_state.generation == 8


def test_changed_replica_replays_to_exact_desired_nodes_and_durable_objects() -> None:
    """Completed mutation resolves prepared identities into exact final replica and object storage."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        retained,
    )
    journal = _corrected_completed_journal(bound, plan, retained, len(plan.actions))
    final = module.materialize_expected_observation(
        plan.actions[-1].completion_postimage,
        journal.preparations,
    )
    preparation_by_slot = {fact.slot_id: fact.identity for fact in journal.preparations}

    assert final.replica_state.generation == 8
    assert final.replica_state.replica_generation == 3
    assert final.replica_state.working_nodes[0].identity == preparation_by_slot["working-new"]
    assert final.replica_state.index.storage.identity == preparation_by_slot["index-new"]
    assert final.replica_state.refs.loose_ref.identity == preparation_by_slot["loose-new"]
    assert final.replica_state.refs.reflogs[0].identity == preparation_by_slot["reflog-new"]
    object_state = next(
        state
        for state in final.storage
        if state.location == module.Location("admin", b"objects/33/" + b"3" * 38)
    )
    assert object_state.identity == preparation_by_slot["commit-new"]
    assert object_state.content == b"commit-three-storage"
    assert module.replay_reconciliation(bound, journal, final).kind == "complete"


def test_semantic_object_vectors_are_independent_from_physical_storage_bytes() -> None:
    """Fixed Git payload vectors keep one semantic identity across two physical encodings."""
    import yinshi.services.workspace_replica as module

    object_id = "d670460b4b4aece5915caf5c68d12f560a9fe3e4"
    payload_length = 13
    payload_sha256 = "sha256:a1fff0ffefb9eace7230c24e50731f0a91c62f9cefdfe77121c2f607125dffae"
    physical_vectors = (
        (
            bytes.fromhex("78014bcac94f5230346628492d2e5148cecf2b49cd2be102004bdf0709"),
            "sha256:7a254758b549802a2e0681b1d15208fc42d4754dc916c24e16790cecdd3cc0bf",
        ),
        (
            bytes.fromhex("78da4bcac94f5230346628492d2e5148cecf2b49cd2be102004bdf0709"),
            "sha256:6459ff11c60f0de91f1a8c080a9cebce28678df1199d95314b9c4aca68e688f5",
        ),
    )

    def vector_spec(physical_bytes: bytes, physical_sha256: str) -> object:
        base = _corrected_spec()
        source = base.preparation_slots[0]
        destination = base.destination_objects[0]
        semantic = replace(
            base.required_objects[0],
            object_id=object_id,
            kind="blob",
            byte_length=payload_length,
            content_sha256=payload_sha256,
        )
        object_parent = module.ParentBinding(
            module.Location("admin", b"objects/d6"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "object-parent-d6")),
        )
        source = replace(
            source,
            installation_parent=object_parent,
            desired_content=physical_bytes,
        )
        destination = replace(
            destination,
            object_id=object_id,
            storage=replace(
                destination.storage,
                location=module.Location(
                    "admin", b"objects/d6/70460b4b4aece5915caf5c68d12f560a9fe3e4"
                ),
                content=physical_bytes,
            ),
            parent=object_parent,
            content_sha256=physical_sha256,
        )
        baseline_entry = replace(base.baseline.index.entries[0], object_id=object_id)
        desired_entry = replace(base.desired.index.entries[0], object_id=object_id)
        return replace(
            base,
            baseline=replace(
                base.baseline,
                index=replace(base.baseline.index, entries=(baseline_entry,)),
            ),
            desired=replace(
                base.desired,
                index=replace(base.desired.index, entries=(desired_entry,)),
            ),
            preparation_slots=(source, *base.preparation_slots[1:]),
            required_objects=(semantic, *base.required_objects[1:]),
            destination_objects=(destination, *base.destination_objects[1:]),
        )

    bound_vectors = tuple(
        module.bind_specification(vector_spec(physical_bytes, physical_sha256))
        for physical_bytes, physical_sha256 in physical_vectors
    )
    semantic_vectors = tuple(
        next(item for item in bound.spec.required_objects if item.object_id == object_id)
        for bound in bound_vectors
    )
    assert semantic_vectors[0] == semantic_vectors[1]
    assert semantic_vectors[0].object_id == object_id
    assert semantic_vectors[0].kind == "blob"
    assert semantic_vectors[0].byte_length == payload_length
    assert semantic_vectors[0].content_sha256 == payload_sha256
    physical_digests = tuple(
        next(
            item.content_sha256
            for item in bound.spec.destination_objects
            if item.object_id == object_id
        )
        for bound in bound_vectors
    )
    assert physical_digests[0] != physical_digests[1]

    empty_payload_vector = module.RequiredObject(
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "blob",
        0,
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "empty-source",
    )
    assert empty_payload_vector.byte_length == 0
    assert empty_payload_vector.content_sha256.endswith(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_binding_enforces_exact_byte_and_expanded_transition_limits() -> None:
    """Binding accepts exact limits and rejects values one below before planning."""
    import base64
    import json
    from dataclasses import fields, is_dataclass

    import yinshi.services.workspace_replica as module

    def projection(value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                "type": type(value).__name__,
                "fields": {
                    field.name: projection(getattr(value, field.name)) for field in fields(value)
                },
            }
        if type(value) is bytes:
            return {"bytes_base64": base64.b64encode(value).decode("ascii")}
        if type(value) is tuple:
            return [projection(item) for item in value]
        if value is None or type(value) in (str, int, bool):
            return value
        raise AssertionError(f"unsupported test value: {type(value)!r}")

    def exact_byte_limit(spec: object) -> int:
        projected = json.dumps(
            projection(spec),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return len(projected) + sum(item.byte_length for item in spec.required_objects)

    byte_candidate = _corrected_spec()
    while True:
        exact_bytes = exact_byte_limit(byte_candidate)
        updated = replace(
            byte_candidate,
            limits=replace(byte_candidate.limits, max_total_bytes=exact_bytes),
        )
        if updated == byte_candidate:
            break
        byte_candidate = updated

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                byte_candidate,
                limits=replace(byte_candidate.limits, max_total_bytes=exact_bytes - 1),
            )
        )
    assert excinfo.value.reason_code == "inventory_limit_exceeded"
    module.bind_specification(byte_candidate)
    module.bind_specification(
        replace(
            byte_candidate,
            limits=replace(byte_candidate.limits, max_total_bytes=exact_bytes + 1),
        )
    )

    for spec in (_corrected_spec(), _corrected_changed_spec()):
        high_bound = module.bind_specification(spec)
        expanded_count = len(
            module.plan_reconciliation(
                high_bound,
                _corrected_initial_observation(high_bound),
                _corrected_retained_inputs(high_bound),
            ).actions
        )
        with pytest.raises(SnapshotRejected) as excinfo:
            module.bind_specification(
                replace(
                    spec,
                    limits=replace(spec.limits, max_transitions=expanded_count - 1),
                )
            )
        assert excinfo.value.reason_code == "inventory_limit_exceeded"
        exact_bound = module.bind_specification(
            replace(
                spec,
                limits=replace(spec.limits, max_transitions=expanded_count),
            )
        )
        assert (
            len(
                module.plan_reconciliation(
                    exact_bound,
                    _corrected_initial_observation(exact_bound),
                    _corrected_retained_inputs(exact_bound),
                ).actions
            )
            == expanded_count
        )
        module.bind_specification(
            replace(
                spec,
                limits=replace(spec.limits, max_transitions=expanded_count + 1),
            )
        )


def test_direct_intermediate_observations_drive_crash_recovery_without_cached_images() -> None:
    """Replay recognizes direct marker and quarantine crash observations."""
    import yinshi.services.workspace_replica as module

    def replace_storage(observation: object, replacement: object) -> object:
        states = {
            (state.location.namespace, state.location.raw_path): state
            for state in observation.storage
        }
        key = (replacement.location.namespace, replacement.location.raw_path)
        states[key] = replacement
        return replace(
            observation,
            storage=tuple(
                sorted(
                    states.values(),
                    key=lambda state: (state.location.namespace, state.location.raw_path),
                )
            ),
        )

    bound = module.bind_specification(_corrected_spec())
    retained = _corrected_retained_inputs(bound)
    plan = module.plan_reconciliation(bound, _corrected_initial_observation(bound), retained)
    marker_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "create_ownership_marker"
    )
    marker_action = plan.actions[marker_index]
    marker_prefix = _corrected_completed_journal(bound, plan, retained, marker_index)
    marker_preimage = module.materialize_expected_observation(
        marker_action.preimage,
        marker_prefix.preparations,
    )
    marker_spec = bound.spec.ownership_markers[0]
    marker_identity = module.NodeIdentity(
        marker_spec.parent.reference.identity.filesystem_id,
        "direct-marker-node",
    )
    direct_marker = module.NodeState(
        marker_spec.destination,
        marker_identity,
        "file",
        b"yinshi-owner-v1\x00fingerprint=" + bound.fingerprint.encode("ascii"),
        False,
        1,
        False,
    )
    marker_observation = replace_storage(marker_preimage, direct_marker)
    marker_intent = module.IntentFact(
        bound.fingerprint,
        marker_action.action_id,
        "direct-marker-intent",
    )
    marker_intended = replace(
        marker_prefix,
        intents=marker_prefix.intents + (marker_intent,),
    )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, marker_intended, marker_observation)
    assert excinfo.value.reason_code == "lost_preparation_identity"
    marker_fact = module.PreparationFact(
        bound.fingerprint,
        marker_spec.slot_id,
        marker_identity,
        "direct-marker-creation",
        _corrected_preparation_position(bound, marker_action.prepared_slot_id),
    )
    marker_effected = replace(marker_intended, preparations=(marker_fact,))
    assert (
        module.replay_reconciliation(bound, marker_effected, marker_observation).kind
        == "retry_synchronization"
    )

    changed_bound = module.bind_specification(_corrected_changed_spec())
    changed_retained = _corrected_retained_inputs(changed_bound)
    changed_plan = module.plan_reconciliation(
        changed_bound,
        _corrected_initial_observation(changed_bound),
        changed_retained,
    )
    quarantine_index = next(
        index
        for index, action in enumerate(changed_plan.actions)
        if action.effect_kind == "quarantine_working_node"
    )
    quarantine_action = changed_plan.actions[quarantine_index]
    quarantine_prefix = _corrected_completed_journal(
        changed_bound,
        changed_plan,
        changed_retained,
        quarantine_index,
    )
    quarantine_preimage = module.materialize_expected_observation(
        quarantine_action.preimage,
        quarantine_prefix.preparations,
    )
    slot = next(
        slot for slot in changed_bound.spec.quarantine_slots if slot.slot_id == "working-old"
    )
    old = changed_bound.spec.baseline.working_nodes[0]
    direct_quarantine = module.NodeState(
        slot.destination,
        old.identity,
        old.kind,
        old.content,
        old.executable,
        old.nlink,
        old.privilege_bits,
    )
    quarantine_observation = replace_storage(quarantine_preimage, direct_quarantine)
    quarantine_observation = replace(
        quarantine_observation,
        replica_state=replace(
            quarantine_observation.replica_state,
            manifest=(),
            working_nodes=(),
        ),
    )
    quarantine_intended = replace(
        quarantine_prefix,
        intents=quarantine_prefix.intents
        + (
            module.IntentFact(
                changed_bound.fingerprint,
                quarantine_action.action_id,
                "direct-quarantine-intent",
            ),
        ),
    )
    assert (
        module.replay_reconciliation(
            changed_bound,
            quarantine_intended,
            quarantine_observation,
        ).kind
        == "retry_synchronization"
    )

    removal_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.effect_kind == "remove_ownership_marker"
    )
    removal_action = plan.actions[removal_index]
    removal_prefix = _corrected_completed_journal(
        bound,
        plan,
        retained,
        removal_index,
    )
    removal_preimage = module.materialize_expected_observation(
        removal_action.preimage,
        removal_prefix.preparations,
    )
    removal_observation = replace_storage(
        removal_preimage,
        module.AbsentState(marker_spec.destination),
    )
    removal_intended = replace(
        removal_prefix,
        intents=removal_prefix.intents
        + (
            module.IntentFact(
                bound.fingerprint,
                removal_action.action_id,
                "direct-removal-intent",
            ),
        ),
    )
    assert (
        module.replay_reconciliation(
            bound,
            removal_intended,
            removal_observation,
        ).kind
        == "retry_synchronization"
    )


def test_semantic_entry_fields_are_required_and_exactly_typed() -> None:
    """Durable semantic entries expose no permissive defaults or boolean integer coercion."""
    import yinshi.services.workspace_replica as module

    with pytest.raises(TypeError):
        module.IndexEntry(b"path", 0, "100644", "1" * 40)
    with pytest.raises(TypeError):
        module.ManifestEntry(b"path", "file", b"bytes")

    base = _corrected_spec()
    malformed_node = replace(base.baseline.working_nodes[0], nlink=True)
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(base, baseline=replace(base.baseline, working_nodes=(malformed_node,)))
        )
    assert excinfo.value.reason_code == "invalid_input_type"


def _f8_schema_paths(spec: object) -> list[bytes]:
    """Enumerate every schema path covered by max_paths and max_depth."""
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
    for slot in spec.preparation_slots:
        paths.extend(
            (
                slot.destination.raw_path,
                slot.materialization_parent.location.raw_path,
                slot.installation_parent.location.raw_path,
                slot.initial.location.raw_path,
            )
        )
    for slot in spec.quarantine_slots:
        paths.extend(
            (
                slot.source.raw_path,
                slot.destination.raw_path,
                slot.source_parent.location.raw_path,
                slot.destination_parent.location.raw_path,
                slot.initial.location.raw_path,
            )
        )
        if slot.restoration_parent is not None:
            paths.append(slot.restoration_parent.location.raw_path)
    for marker in spec.ownership_markers:
        paths.extend(
            (
                marker.destination.raw_path,
                marker.parent.location.raw_path,
                marker.initial.location.raw_path,
            )
        )
    for item in spec.required_objects:
        paths.append(f"objects/{item.object_id[:2]}/{item.object_id[2:]}".encode("ascii"))
    for item in spec.destination_objects:
        paths.extend((item.storage.location.raw_path, item.parent.location.raw_path))
    return paths


def test_binding_bounds_complete_schema_path_count() -> None:
    """The complete schema path count has exact below, equal, and above behavior."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    complete_count = len(_f8_schema_paths(spec))
    assert complete_count == 42
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(spec, limits=replace(spec.limits, max_paths=complete_count - 1))
        )
    assert excinfo.value.reason_code == "inventory_limit_exceeded"
    module.bind_specification(replace(spec, limits=replace(spec.limits, max_paths=complete_count)))
    module.bind_specification(
        replace(spec, limits=replace(spec.limits, max_paths=complete_count + 1))
    )


def test_binding_bounds_deep_preparation_and_marker_paths() -> None:
    """Depth accounting includes private preparation and marker fields."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    deep_destination = module.Location("private", b"prepare/d1/d2/d3/slot-1")
    deep_parent = module.ParentBinding(
        module.Location("private", b"prepare/d1/d2/d3"),
        module.ExistingNode(module.NodeIdentity("fs-admin", "deep-prepare-parent")),
    )
    deep_slot = replace(
        spec.preparation_slots[0],
        destination=deep_destination,
        materialization_parent=deep_parent,
        initial=module.AbsentState(deep_destination),
    )
    marker_destination = module.Location("private", b"markers/m1/m2/m3/op-1")
    marker_parent = module.ParentBinding(
        module.Location("private", b"markers/m1/m2/m3"),
        module.ExistingNode(module.NodeIdentity("fs-private", "deep-marker-parent")),
    )
    marker = replace(
        spec.ownership_markers[0],
        destination=marker_destination,
        parent=marker_parent,
        initial=module.AbsentState(marker_destination),
    )
    deep_spec = replace(
        spec,
        preparation_slots=(deep_slot, spec.preparation_slots[1]),
        ownership_markers=(marker,),
    )
    deepest = max(path.count(b"/") + 1 for path in _f8_schema_paths(deep_spec))
    assert deepest == 5
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(deep_spec, limits=replace(deep_spec.limits, max_depth=deepest - 1))
        )
    assert excinfo.value.reason_code == "inventory_limit_exceeded"
    module.bind_specification(
        replace(deep_spec, limits=replace(deep_spec.limits, max_depth=deepest))
    )
    module.bind_specification(
        replace(deep_spec, limits=replace(deep_spec.limits, max_depth=deepest + 1))
    )


def test_binding_bounds_protected_boundary_ancestor_paths() -> None:
    """Depth accounting includes an ordered protected ancestor chain."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()

    def ancestor(path: bytes, node_id: str) -> object:
        return module.NodeState(
            module.Location("private", path),
            module.NodeIdentity("fs-private", node_id),
            "directory",
            b"",
            False,
            2,
            False,
        )

    chain = (
        ancestor(b"deep", "deep-1"),
        ancestor(b"deep/a", "deep-2"),
        ancestor(b"deep/a/b", "deep-3"),
        ancestor(b"deep/a/b/c", "deep-4"),
    )
    deep_boundary = module.Boundary(
        "private-boundary",
        "private",
        module.Location("private", b"deep/a/b/c/private-root"),
        module.NodeIdentity("fs-private", "deep-private-root"),
        chain,
    )
    deep_spec = replace(
        spec,
        boundaries=tuple(
            deep_boundary if boundary.role == "private" else boundary
            for boundary in spec.boundaries
        ),
    )
    deepest = max(path.count(b"/") + 1 for path in _f8_schema_paths(deep_spec))
    assert deepest == 5
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(deep_spec, limits=replace(deep_spec.limits, max_depth=deepest - 1))
        )
    assert excinfo.value.reason_code == "inventory_limit_exceeded"
    module.bind_specification(
        replace(deep_spec, limits=replace(deep_spec.limits, max_depth=deepest))
    )


def test_binding_bounds_index_paths_before_prefix_validation() -> None:
    """Path admission runs before the quadratic index prefix check."""
    import yinshi.services.workspace_replica as module

    spec = _corrected_spec()
    colliding = (
        _index(b"collide", oid="1" * 40),
        _index(b"collide/child", oid="1" * 40),
    )
    candidate = replace(
        spec,
        baseline=replace(spec.baseline, index=replace(spec.baseline.index, entries=colliding)),
        desired=replace(spec.desired, index=replace(spec.desired.index, entries=colliding)),
    )
    complete_count = len(_f8_schema_paths(candidate))
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(candidate, limits=replace(candidate.limits, max_paths=complete_count - 1))
        )
    assert excinfo.value.reason_code == "inventory_limit_exceeded"
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "index_prefix_collision"


def test_binding_stores_exact_transition_count_and_planning_requires_it() -> None:
    """Planning must match the transition count stored during binding."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    assert bound.transition_count == len(plan.actions) == 28

    changed_bound = module.bind_specification(_corrected_changed_spec())
    changed_plan = module.plan_reconciliation(
        changed_bound,
        _corrected_initial_observation(changed_bound),
        _corrected_retained_inputs(changed_bound),
    )
    assert changed_bound.transition_count == len(changed_plan.actions)

    tampered = replace(bound, transition_count=bound.transition_count + 1)
    with pytest.raises(SnapshotRejected) as excinfo:
        module._derive_protocol_plan(tampered)
    assert excinfo.value.reason_code == "invalid_plan_integrity"
    with pytest.raises(SnapshotRejected) as excinfo:
        module.plan_reconciliation(
            tampered,
            _corrected_initial_observation(bound),
            _corrected_retained_inputs(bound),
        )
    assert excinfo.value.reason_code == "specification_fingerprint_mismatch"


def test_working_install_actions_sort_leaves_by_raw_path_not_depth() -> None:
    """Files and symlinks sort by raw path bytes after parent-first directories."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    root_parent = module.ParentBinding(
        module.Location("working", b"root-root"),
        module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
    )
    directory = module.NodeState(
        module.Location("working", b"a"),
        module.NodeIdentity("fs-working", "directory-a"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    desired_directory = module.DesiredNodeState(
        directory.location,
        module.ExistingNode(directory.identity),
        directory.kind,
        directory.content,
        directory.executable,
        directory.nlink,
        directory.privilege_bits,
    )
    deep_location = module.Location("working", b"a/leaf")
    shallow_location = module.Location("working", b"b")
    deep_slot_location = module.Location("private", b"prepare-working/deep-leaf")
    shallow_slot_location = module.Location("private", b"prepare-working/shallow-leaf")
    materialization_parent = module.ParentBinding(
        module.Location("private", b"prepare-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "prepare-working-parent")),
    )
    deep_slot = module.PreparationSlot(
        "deep-leaf",
        deep_slot_location,
        materialization_parent,
        module.ParentBinding(directory.location, module.ExistingNode(directory.identity)),
        module.AbsentState(deep_slot_location),
        "file",
        b"deep",
        False,
    )
    shallow_slot = module.PreparationSlot(
        "shallow-leaf",
        shallow_slot_location,
        materialization_parent,
        root_parent,
        module.AbsentState(shallow_slot_location),
        "file",
        b"shallow",
        False,
    )
    desired_nodes = (
        desired_directory,
        module.DesiredNodeState(
            deep_location,
            module.PreparedNode("deep-leaf"),
            "file",
            b"deep",
            False,
            1,
            False,
        ),
        module.DesiredNodeState(
            shallow_location,
            module.PreparedNode("shallow-leaf"),
            "file",
            b"shallow",
            False,
            1,
            False,
        ),
    )
    manifest = (
        module.ManifestEntry(b"a", "directory", b"", False, 2, False),
        module.ManifestEntry(b"a/leaf", "file", b"deep", False, 1, False),
        module.ManifestEntry(b"b", "file", b"shallow", False, 1, False),
    )
    candidate = replace(
        base,
        baseline=replace(
            base.baseline,
            index=replace(base.baseline.index, entries=()),
            manifest=(manifest[0],),
            working_nodes=(directory,),
        ),
        desired=replace(
            base.desired,
            index=replace(base.desired.index, entries=()),
            manifest=manifest,
            working_nodes=desired_nodes,
        ),
        preparation_slots=base.preparation_slots + (deep_slot, shallow_slot),
    )

    bound = module.bind_specification(candidate)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    install_ids = [
        action.prepared_slot_id
        for action in plan.actions
        if action.effect_kind == "install_working_node"
    ]
    assert install_ids == ["deep-leaf", "shallow-leaf"]


def test_binding_rejects_missing_object_destination_equal_to_recorded_absence() -> None:
    """A generated object destination cannot occupy a recorded absence."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    missing_oid = base.required_objects[0].object_id
    object_location = module.Location(
        "admin", f"objects/{missing_oid[:2]}/{missing_oid[2:]}".encode("ascii")
    )
    recorded_absence = module.AbsentState(object_location)
    baseline = replace(
        base.baseline, refs=replace(base.baseline.refs, packed_refs=recorded_absence)
    )
    desired = replace(base.desired, refs=replace(base.desired.refs, packed_refs=recorded_absence))

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            replace(
                base,
                baseline=baseline,
                desired=desired,
                destination_objects=base.destination_objects[1:],
            )
        )
    assert excinfo.value.reason_code == "duplicate_location"


def test_binding_rejects_present_object_destination_equal_to_recorded_absence() -> None:
    """A durable object cannot occupy a recorded administrative absence."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    destination = base.destination_objects[0]
    recorded_absence = module.AbsentState(destination.storage.location)
    baseline = replace(
        base.baseline, refs=replace(base.baseline.refs, packed_refs=recorded_absence)
    )
    desired = replace(base.desired, refs=replace(base.desired.refs, packed_refs=recorded_absence))

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(base, baseline=baseline, desired=desired))
    assert excinfo.value.reason_code == "duplicate_location"


def test_binding_rejects_quarantine_destination_equal_to_missing_object_destination() -> None:
    """A quarantine destination cannot occupy a generated object destination."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    missing_object = next(item for item in changed.required_objects if item.object_id == "3" * 40)
    object_location = module.Location(
        "admin",
        f"objects/{missing_object.object_id[:2]}/{missing_object.object_id[2:]}".encode("ascii"),
    )
    loose_quarantine = next(
        slot for slot in changed.quarantine_slots if slot.slot_id == "loose-old"
    )
    hijacked = replace(
        loose_quarantine,
        destination=object_location,
        destination_parent=module.ParentBinding(
            module.Location("admin", b"objects/33"),
            module.ExistingNode(module.NodeIdentity("fs-admin", "object-parent-33")),
        ),
        initial=module.AbsentState(object_location),
    )
    quarantine_slots = tuple(
        hijacked if slot.slot_id == "loose-old" else slot for slot in changed.quarantine_slots
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(changed, quarantine_slots=quarantine_slots))
    assert excinfo.value.reason_code == "duplicate_location"


def _corrected_spec_with_known_parent_state(kind: str, content: bytes) -> object:
    """Build the changed spec with one baseline node at the promotion-parent location."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    known_parent_state = module.NodeState(
        module.Location("admin", b"objects/33"),
        module.NodeIdentity("fs-admin", "object-parent-33"),
        kind,
        content,
        False,
        1,
        False,
    )
    baseline = replace(
        changed.baseline,
        refs=replace(changed.baseline.refs, unrelated_metadata=(known_parent_state,)),
    )
    desired_unrelated = (
        module.DesiredNodeState(
            known_parent_state.location,
            module.ExistingNode(known_parent_state.identity),
            known_parent_state.kind,
            known_parent_state.content,
            known_parent_state.executable,
            known_parent_state.nlink,
            known_parent_state.privilege_bits,
        ),
    )
    desired = replace(
        changed.desired,
        refs=replace(changed.desired.refs, unrelated_metadata=desired_unrelated),
    )
    return replace(changed, baseline=baseline, desired=desired)


def test_binding_rejects_promotion_parent_resolving_to_known_file() -> None:
    """A parent binding must not resolve to a known regular file."""
    import yinshi.services.workspace_replica as module

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(
            _corrected_spec_with_known_parent_state("file", b"not-a-directory")
        )
    assert excinfo.value.reason_code == "invalid_parent_state"


def test_binding_rejects_private_node_in_administrative_metadata() -> None:
    """Administrative metadata cannot contain a private node."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    known_file = module.NodeState(
        module.Location("private", b"markers"),
        module.NodeIdentity("fs-private", "marker-parent"),
        "file",
        b"unrelated",
        False,
        1,
        False,
    )
    baseline = replace(
        base.baseline,
        refs=replace(base.baseline.refs, unrelated_metadata=(known_file,)),
    )
    desired_unrelated = (
        module.DesiredNodeState(
            known_file.location,
            module.ExistingNode(known_file.identity),
            known_file.kind,
            known_file.content,
            known_file.executable,
            known_file.nlink,
            known_file.privilege_bits,
        ),
    )
    desired = replace(
        base.desired, refs=replace(base.desired.refs, unrelated_metadata=desired_unrelated)
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(base, baseline=baseline, desired=desired))
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_preserves_unresolved_parents() -> None:
    """A parent without complete concrete state remains valid."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_changed_spec())
    parent_locations = {
        binding.location for binding in module._corrected_initial_parent_bindings(bound.spec)
    }
    assert module.Location("admin", b"objects/33") in parent_locations


def test_binding_rejects_omitted_present_baseline_reflog() -> None:
    """A present baseline reflog requires an explicit desired state."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    candidate = replace(
        changed,
        desired=replace(
            changed.desired,
            refs=replace(changed.desired.refs, reflogs=()),
        ),
        preparation_slots=tuple(
            slot for slot in changed.preparation_slots if slot.slot_id != "reflog-new"
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_ref_semantics"


def test_binding_rejects_omitted_absent_baseline_reflog() -> None:
    """An absent baseline reflog also requires an explicit desired state."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    reflog_location = changed.baseline.refs.reflogs[0].location
    candidate = replace(
        changed,
        baseline=replace(
            changed.baseline,
            refs=replace(
                changed.baseline.refs,
                reflogs=(module.AbsentState(reflog_location),),
            ),
        ),
        desired=replace(
            changed.desired,
            refs=replace(changed.desired.refs, reflogs=()),
        ),
        preparation_slots=tuple(
            slot for slot in changed.preparation_slots if slot.slot_id != "reflog-new"
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_ref_semantics"


def test_explicit_reflog_absence_binds_and_plans() -> None:
    """An explicit desired absence compiles into the matching quarantine."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    reflog_location = changed.baseline.refs.reflogs[0].location
    candidate = replace(
        changed,
        desired=replace(
            changed.desired,
            refs=replace(
                changed.desired.refs,
                reflogs=(module.AbsentState(reflog_location),),
            ),
        ),
        preparation_slots=tuple(
            slot for slot in changed.preparation_slots if slot.slot_id != "reflog-new"
        ),
    )

    bound = module.bind_specification(candidate)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    effect_kinds = tuple(action.effect_kind for action in plan.actions)
    assert "quarantine_reflog" in effect_kinds
    assert "install_reflog" not in effect_kinds


@pytest.mark.parametrize("factory", (_corrected_spec, _corrected_changed_spec))
def test_existing_and_prepared_reflog_states_bind_and_plan(factory: object) -> None:
    """Existing and prepared desired reflog states remain total."""
    import yinshi.services.workspace_replica as module

    candidate = factory()
    bound = module.bind_specification(candidate)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    assert len(plan.actions) == bound.transition_count


@pytest.mark.parametrize("field", ("packed_refs", "unrelated_metadata"))
@pytest.mark.parametrize("phase", ("baseline", "desired"))
def test_binding_rejects_administrative_storage_outside_admin_namespace(
    phase: str, field: str
) -> None:
    """Administrative storage cannot use a working namespace location."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    shared = module.Location("working", b"keep.txt")
    working_identity = module.NodeIdentity("fs-working", "working-keep")
    if field == "packed_refs":
        state = module.AbsentState(shared)
    elif phase == "baseline":
        state = module.NodeState(shared, working_identity, "file", b"same", False, 1, False)
    else:
        state = module.DesiredNodeState(
            shared,
            module.ExistingNode(working_identity),
            "file",
            b"same",
            False,
            1,
            False,
        )
    value = (state,) if field == "unrelated_metadata" else state
    if phase == "baseline":
        candidate = replace(
            base,
            baseline=replace(base.baseline, refs=replace(base.baseline.refs, **{field: value})),
        )
    else:
        candidate = replace(
            base,
            desired=replace(base.desired, refs=replace(base.desired.refs, **{field: value})),
        )
    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_specification"


def test_binding_rejects_prepared_target_equal_to_existing_object_parent() -> None:
    """A prepared file cannot replace a location still used as a parent."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    baseline_reflog = changed.baseline.refs.reflogs[0]
    slots = tuple(
        (
            replace(
                slot,
                installation_parent=module.ParentBinding(
                    module.Location("admin", b"objects"),
                    module.ExistingNode(module.NodeIdentity("fs-admin", "objects-parent")),
                ),
            )
            if slot.slot_id == "reflog-new"
            else slot
        )
        for slot in changed.preparation_slots
    )
    candidate = replace(
        changed,
        preparation_slots=slots,
        desired=replace(
            changed.desired,
            refs=replace(
                changed.desired.refs,
                reflogs=(
                    module.DesiredNodeState(
                        module.Location("admin", b"objects/33"),
                        module.PreparedNode("reflog-new"),
                        "file",
                        b"new-log",
                        False,
                        1,
                        False,
                    ),
                    module.DesiredNodeState(
                        baseline_reflog.location,
                        module.ExistingNode(baseline_reflog.identity),
                        baseline_reflog.kind,
                        baseline_reflog.content,
                        baseline_reflog.executable,
                        baseline_reflog.nlink,
                        baseline_reflog.privilege_bits,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "duplicate_location"


def test_expected_image_rejects_absence_over_present_location() -> None:
    """A derived image cannot record absence at a present location."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    image = module._corrected_initial_expected(bound.spec)
    contradictory = replace(
        image,
        storage=image.storage + (module.AbsentState(bound.spec.baseline.refs.head.location),),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module._corrected_validate_expected_image(contradictory)
    assert excinfo.value.reason_code == "duplicate_location"


def test_expected_image_rejects_duplicate_absence_location() -> None:
    """A derived image cannot record one absent storage location twice."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    image = module._corrected_initial_expected(bound.spec)
    absence = image.storage[0]
    contradictory = replace(image, storage=image.storage + (absence,))

    with pytest.raises(SnapshotRejected) as excinfo:
        module._corrected_validate_expected_image(contradictory)
    assert excinfo.value.reason_code == "duplicate_location"


def test_binding_rejects_preparation_as_its_own_parent() -> None:
    """A preparation cannot be its own installation parent."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    slots = tuple(
        (
            replace(
                slot,
                installation_parent=module.ParentBinding(
                    slot.installation_parent.location,
                    module.PreparedNode("working-new"),
                ),
            )
            if slot.slot_id == "working-new"
            else slot
        )
        for slot in changed.preparation_slots
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(changed, preparation_slots=slots))
    assert excinfo.value.reason_code == "cyclic_prepared_parent"


def test_binding_rejects_prepared_file_as_parent() -> None:
    """A prepared file cannot serve as an installation parent."""
    import yinshi.services.workspace_replica as module

    changed = _corrected_changed_spec()
    slots = tuple(
        (
            replace(
                slot,
                installation_parent=module.ParentBinding(
                    slot.installation_parent.location,
                    module.PreparedNode("reflog-new"),
                ),
            )
            if slot.slot_id == "index-new"
            else slot
        )
        for slot in changed.preparation_slots
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(replace(changed, preparation_slots=slots))
    assert excinfo.value.reason_code == "invalid_parent_state"


def test_creation_synchronization_cannot_precede_preparation_fact() -> None:
    """Creation synchronization requires the durable post-effect identity."""
    import yinshi.services.workspace_replica as module

    bound = module.bind_specification(_corrected_spec())
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    action_index, action = next(
        (index, item) for index, item in enumerate(plan.actions) if item.creates_prepared_identity
    )
    requirement = action.synchronization_requirements[0]
    prefix = _corrected_completed_journal(
        bound,
        plan,
        _corrected_retained_inputs(bound),
        action_index,
    )
    journal = replace(
        prefix,
        intents=prefix.intents
        + (module.IntentFact(bound.fingerprint, action.action_id, "intent-receipt"),),
        synchronizations=prefix.synchronizations
        + (
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                requirement,
                "sync-receipt",
                _corrected_journal_position(
                    bound,
                    action_index * 1_000 + 50,
                    "early-synchronization",
                ),
                "creation-receipt",
            ),
        ),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(
            bound,
            journal,
            module.materialize_expected_observation(
                action.preimage,
                prefix.preparations,
            ),
            plan,
        )
    assert excinfo.value.reason_code == "impossible_replay_order"

    parent_by_slot = {marker.slot_id: marker.parent for marker in bound.spec.ownership_markers} | {
        slot.slot_id: slot.materialization_parent for slot in bound.spec.preparation_slots
    }
    parent = parent_by_slot[action.prepared_slot_id]
    preparation = module.PreparationFact(
        bound.fingerprint,
        action.prepared_slot_id,
        module.NodeIdentity(parent.reference.identity.filesystem_id, "created-node"),
        "creation-receipt",
        _corrected_preparation_position(bound, action.prepared_slot_id),
    )
    fresh = module.materialize_expected_observation(
        action.permitted_effect_postimages[0],
        (preparation,),
    )
    stale = replace(journal, preparations=prefix.preparations + (preparation,))
    with pytest.raises(SnapshotRejected) as excinfo:
        module.replay_reconciliation(bound, stale, fresh, plan)
    assert excinfo.value.reason_code == "impossible_replay_order"

    synchronized = replace(
        stale,
        synchronizations=prefix.synchronizations
        + tuple(
            module.SynchronizationFact(
                bound.fingerprint,
                action.action_id,
                item,
                f"sync-{index}",
                _corrected_synchronization_position(
                    bound,
                    plan,
                    action,
                    item,
                    prefix.preparations + (preparation,),
                ),
                preparation.creation_receipt_id,
            )
            for index, item in enumerate(
                module.resolve_synchronization_requirements(
                    action,
                    prefix.preparations + (preparation,),
                )
            )
        ),
    )
    assert (
        module.replay_reconciliation(bound, synchronized, fresh, plan).kind == "record_completion"
    )

    current_index = len(prefix.synchronizations)
    current_fact = synchronized.synchronizations[current_index]
    invalid_positions = (
        (
            replace(current_fact.journal_position, journal_id="foreign-journal"),
            "foreign_journal",
        ),
        (
            replace(
                current_fact.journal_position,
                sequence=preparation.journal_position.sequence,
            ),
            "invalid_journal",
        ),
        (
            replace(
                current_fact.journal_position,
                append_receipt_id=preparation.journal_position.append_receipt_id,
            ),
            "invalid_journal",
        ),
        (
            replace(current_fact.journal_position, sequence=0),
            "invalid_specification",
        ),
    )
    for invalid_position, reason_code in invalid_positions:
        invalid_fact = replace(current_fact, journal_position=invalid_position)
        invalid_syncs = list(synchronized.synchronizations)
        invalid_syncs[current_index] = invalid_fact
        invalid_journal = replace(synchronized, synchronizations=tuple(invalid_syncs))
        with pytest.raises(SnapshotRejected) as excinfo:
            module.replay_reconciliation(bound, invalid_journal, fresh, plan)
        assert excinfo.value.reason_code == reason_code


def test_deep_malformed_tuple_refuses_without_python_recursion_error() -> None:
    """Deep foreign tuple structure fails through the protocol refusal boundary."""
    import yinshi.services.workspace_replica as module

    nested: object = "invalid"
    for _ in range(1_200):
        nested = (nested,)
    base = _corrected_spec()
    candidate = replace(base, preparation_slots=nested)

    with pytest.raises(SnapshotRejected):
        module.bind_specification(candidate)


def test_oversized_integer_refuses_before_canonical_json_encoding() -> None:
    """An oversized integer cannot escape through the JSON encoder."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    candidate = replace(
        base,
        identity=replace(base.identity, generation=10**5_000),
    )

    with pytest.raises(SnapshotRejected) as excinfo:
        module.bind_specification(candidate)
    assert excinfo.value.reason_code == "invalid_specification"


def test_nested_prepared_directories_track_intermediate_link_counts() -> None:
    """A prepared parent gains one link only after its child installation."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    materialization_parent = module.ParentBinding(
        module.Location("private", b"prepare-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "prep-working-parent")),
    )
    root_parent = module.ParentBinding(
        module.Location("working", b"root-root"),
        module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
    )
    top_location = module.Location("working", b"tree")
    top_slot = module.PreparationSlot(
        "dir-top",
        module.Location("private", b"prepare-working/dir-top"),
        materialization_parent,
        root_parent,
        module.AbsentState(module.Location("private", b"prepare-working/dir-top")),
        "directory",
        b"",
        False,
    )
    child_slot = module.PreparationSlot(
        "dir-child",
        module.Location("private", b"prepare-working/dir-child"),
        materialization_parent,
        module.ParentBinding(top_location, module.PreparedNode("dir-top")),
        module.AbsentState(module.Location("private", b"prepare-working/dir-child")),
        "directory",
        b"",
        False,
    )
    child_location = module.Location("working", b"tree/sub")
    top = module.DesiredNodeState(
        top_location,
        module.PreparedNode("dir-top"),
        "directory",
        b"",
        False,
        3,
        False,
    )
    child = module.DesiredNodeState(
        child_location,
        module.PreparedNode("dir-child"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    candidate = replace(
        base,
        preparation_slots=base.preparation_slots + (top_slot, child_slot),
        desired=replace(
            base.desired,
            manifest=base.desired.manifest
            + (
                module.ManifestEntry(b"tree", "directory", b"", False, 3, False),
                module.ManifestEntry(b"tree/sub", "directory", b"", False, 2, False),
            ),
            working_nodes=base.desired.working_nodes + (top, child),
        ),
    )

    bound = module.bind_specification(candidate)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    actions = tuple(
        action
        for action in plan.actions
        if action.effect_kind == "install_working_node"
        and action.prepared_slot_id in {"dir-top", "dir-child"}
    )
    assert tuple(action.prepared_slot_id for action in actions) == ("dir-top", "dir-child")
    top_after_parent = next(
        node
        for node in actions[0].completion_postimage.replica_state.working_nodes
        if node.location == top_location
    )
    top_after_child = next(
        node
        for node in actions[1].completion_postimage.replica_state.working_nodes
        if node.location == top_location
    )
    assert top_after_parent.nlink == 2
    assert top_after_child.nlink == 3
    preparations = _corrected_preparation_facts(bound)
    for action in actions:
        fresh = module.materialize_expected_observation(
            action.completion_postimage,
            preparations,
        )
        module._corrected_validate_fresh_observation(bound, fresh)


def test_nested_directory_quarantine_and_restoration_track_link_counts() -> None:
    """Nested directory moves preserve every intermediate link count."""
    import yinshi.services.workspace_replica as module

    base = _corrected_spec()
    old_parent = module.NodeState(
        module.Location("working", b"tree"),
        module.NodeIdentity("fs-working", "old-tree"),
        "directory",
        b"",
        False,
        3,
        False,
    )
    old_child = module.NodeState(
        module.Location("working", b"tree/sub"),
        module.NodeIdentity("fs-working", "old-sub"),
        "directory",
        b"",
        False,
        2,
        False,
    )
    manifest = (
        module.ManifestEntry(b"tree", "directory", b"", False, 3, False),
        module.ManifestEntry(b"tree/sub", "directory", b"", False, 2, False),
    )
    materialization_parent = module.ParentBinding(
        module.Location("private", b"prepare-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "prep-working-parent")),
    )
    root_parent = module.ParentBinding(
        module.Location("working", b"root-root"),
        module.ExistingNode(module.NodeIdentity("fs-working", "root-root")),
    )
    replacement_slot = module.PreparationSlot(
        "new-tree",
        module.Location("private", b"prepare-working/new-tree"),
        materialization_parent,
        root_parent,
        module.AbsentState(module.Location("private", b"prepare-working/new-tree")),
        "directory",
        b"",
        False,
    )
    desired_nodes = (
        module.DesiredNodeState(
            old_parent.location,
            module.PreparedNode("new-tree"),
            "directory",
            b"",
            False,
            3,
            False,
        ),
        module.DesiredNodeState(
            old_child.location,
            module.ExistingNode(old_child.identity),
            "directory",
            b"",
            False,
            2,
            False,
        ),
    )
    quarantine_parent = module.ParentBinding(
        module.Location("excluded", b"quarantine-working"),
        module.ExistingNode(module.NodeIdentity("fs-working", "quarantine-parent")),
    )
    quarantine_slots = (
        module.QuarantineSlot(
            "q-tree",
            old_parent.location,
            old_parent.identity,
            root_parent,
            module.Location("excluded", b"quarantine-working/tree"),
            quarantine_parent,
            None,
            module.AbsentState(module.Location("excluded", b"quarantine-working/tree")),
        ),
        module.QuarantineSlot(
            "q-sub",
            old_child.location,
            old_child.identity,
            module.ParentBinding(
                old_parent.location,
                module.ExistingNode(old_parent.identity),
            ),
            module.Location("excluded", b"quarantine-working/sub"),
            quarantine_parent,
            module.ParentBinding(
                old_parent.location,
                module.PreparedNode("new-tree"),
            ),
            module.AbsentState(module.Location("excluded", b"quarantine-working/sub")),
        ),
    )
    candidate = replace(
        base,
        baseline=replace(base.baseline, manifest=manifest, working_nodes=(old_parent, old_child)),
        desired=replace(base.desired, manifest=manifest, working_nodes=desired_nodes),
        preparation_slots=base.preparation_slots + (replacement_slot,),
        quarantine_slots=quarantine_slots,
    )

    bound = module.bind_specification(candidate)
    plan = module.plan_reconciliation(
        bound,
        _corrected_initial_observation(bound),
        _corrected_retained_inputs(bound),
    )
    preparations = _corrected_preparation_facts(bound)
    for action in plan.actions:
        for image in (*action.permitted_effect_postimages, action.completion_postimage):
            fresh = module.materialize_expected_observation(image, preparations)
            module._corrected_validate_fresh_observation(bound, fresh)
    final = module.materialize_expected_observation(
        plan.actions[-1].completion_postimage,
        preparations,
    )
    expected = module._corrected_resolve_replica(
        bound.spec.desired,
        {fact.slot_id: fact for fact in preparations},
    )
    assert final.replica_state == expected
