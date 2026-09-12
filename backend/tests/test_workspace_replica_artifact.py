"""Validate bounded deterministic workspace-replica artifact encoding."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import yinshi.services.workspace_replica_artifact as artifact_module
from yinshi.services.workspace_replica_artifact import (
    ArtifactLimits,
    ManifestEntry,
    WorktreeArtifactDecodeError,
    WorktreeArtifactEncodeError,
    WorktreeArtifactInput,
    WorktreeEntryKind,
    decode_worktree_artifact,
    encode_worktree_artifact,
)

OID = b"a" * 20
OTHER_OID = b"b" * 20


def artifact_input() -> WorktreeArtifactInput:
    return WorktreeArtifactInput(
        object_format="sha1",
        head_state="symbolic",
        head_target=b"refs/heads/main",
        head_oid=OID,
        index_bytes=b"DIRC\x00exact-index\xff",
        entries=(
            ManifestEntry(b"dir", WorktreeEntryKind.DIRECTORY),
            ManifestEntry(
                b"dir/file\xff",
                WorktreeEntryKind.FILE,
                b"staged and unstaged\x00bytes",
                executable=True,
            ),
            ManifestEntry(
                b"link",
                WorktreeEntryKind.SYMLINK,
                b"../opaque-target\xff",
                allowed_ignored=True,
            ),
        ),
        root_oids=(OTHER_OID, OID),
        policy_digest=b"p" * 32,
    )


def sections(frame: bytes) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    offset = 20
    for _ in range(4):
        section_id = int.from_bytes(frame[offset : offset + 4], "big")
        length = int.from_bytes(frame[offset + 4 : offset + 12], "big")
        start = offset + 44
        result.append((section_id, frame[start : start + length]))
        offset = start + length
    assert offset == len(frame)
    return result


def reframe(values: list[tuple[int, bytes]]) -> bytes:
    return artifact_module._frame_envelope(values)


def control_offsets(value: WorktreeArtifactInput) -> tuple[int, int, int]:
    count = 8 + len(value.head_target) + len(value.head_oid)
    return count, count + 4, count + 8


def test_round_trip_preserves_exact_index_manifest_and_head() -> None:
    source = artifact_input()
    encoded = encode_worktree_artifact(source)
    decoded = decode_worktree_artifact(encoded)

    assert encode_worktree_artifact(source) == encoded
    assert decoded.index_bytes == source.index_bytes
    assert decoded.entries == tuple(sorted(source.entries, key=lambda entry: entry.raw_path))
    assert decoded.head_target == source.head_target
    assert decoded.head_oid == source.head_oid
    assert decoded.root_oids == (OID, OTHER_OID)
    assert decoded.policy_digest == source.policy_digest
    assert decoded.total_bytes == len(encoded)
    assert len(decoded.source_state_sha256) == 32


def test_sha256_and_unborn_repositories_round_trip() -> None:
    sha256_oid = b"c" * 32
    sha256_value = replace(
        artifact_input(),
        object_format="sha256",
        head_oid=sha256_oid,
        root_oids=(sha256_oid,),
    )
    decoded = decode_worktree_artifact(encode_worktree_artifact(sha256_value))
    assert decoded.object_format == "sha256"
    assert decoded.root_oids == (sha256_oid,)

    unborn = WorktreeArtifactInput(object_format="sha1", head_state="unborn")
    decoded_unborn = decode_worktree_artifact(encode_worktree_artifact(unborn))
    assert decoded_unborn.has_index is False
    assert decoded_unborn.index_bytes is None
    assert decoded_unborn.head_oid == b""


@pytest.mark.parametrize(
    "path",
    [
        b"",
        b"/absolute",
        b".",
        b"..",
        b"a/./b",
        b"a/../b",
        b".git/x",
        b"a/.git/x",
        b"a//b",
        b"nul\x00path",
    ],
)
def test_encoder_rejects_unsafe_paths(path: bytes) -> None:
    value = replace(
        artifact_input(),
        entries=(ManifestEntry(path, WorktreeEntryKind.FILE, b"x"),),
    )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(value)


def test_encoder_rejects_missing_or_non_directory_parents() -> None:
    missing = replace(
        artifact_input(),
        entries=(ManifestEntry(b"a/b", WorktreeEntryKind.FILE, b"x"),),
    )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(missing)

    blocked = replace(
        artifact_input(),
        entries=(
            ManifestEntry(b"a", WorktreeEntryKind.FILE, b"x"),
            ManifestEntry(b"a/b", WorktreeEntryKind.FILE, b"y"),
        ),
    )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(blocked)


@pytest.mark.parametrize(
    "entry",
    [
        ManifestEntry(b"hard", WorktreeEntryKind.FILE, b"x", nlink=2),
        ManifestEntry(b"priv", WorktreeEntryKind.FILE, b"x", privilege_bits=True),
        ManifestEntry(b"dir", WorktreeEntryKind.DIRECTORY, b"x"),
        ManifestEntry(b"dir", WorktreeEntryKind.DIRECTORY, executable=True),
        ManifestEntry(b"link", WorktreeEntryKind.SYMLINK, b"x", executable=True),
    ],
)
def test_encoder_rejects_unrepresentable_node_state(entry: ManifestEntry) -> None:
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), entries=(entry,)))


def test_encoder_rejects_duplicate_paths_and_invalid_heads() -> None:
    duplicate = replace(
        artifact_input(),
        entries=(
            ManifestEntry(b"dup", WorktreeEntryKind.FILE, b"x"),
            ManifestEntry(b"dup", WorktreeEntryKind.FILE, b"y"),
        ),
    )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(duplicate)

    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), head_oid=b"c" * 20))
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), head_target=b"", head_state="symbolic"))
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), head_target=b"x", head_state="detached"))
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(
            WorktreeArtifactInput(
                object_format="sha1",
                head_state="unborn",
                head_target=b"refs/heads/main",
            )
        )


@pytest.mark.parametrize(
    "target",
    [
        b"main",
        b"refs/tags/v1",
        b"refs/heads/",
        b"refs/heads/.hidden",
        b"refs/heads/a..b",
        b"refs/heads/a@{b",
        b"refs/heads/a//b",
        b"refs/heads/a.lock",
        b"refs/heads/a\x00b",
        b"refs/heads/a\nb",
    ],
)
def test_encoder_rejects_noncanonical_symbolic_head_targets(target: bytes) -> None:
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), head_target=target))


def test_encoder_rejects_zero_duplicate_roots_and_unbound_ignored_entries() -> None:
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(
            replace(artifact_input(), head_oid=b"\x00" * 20, root_oids=(b"\x00" * 20,))
        )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), root_oids=(OID, OID)))
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), policy_digest=None))


def test_encoder_rejects_unmaterializable_symlink_and_preserves_ignored_directory() -> None:
    empty_symlink = replace(
        artifact_input(),
        entries=(ManifestEntry(b"link", WorktreeEntryKind.SYMLINK, b""),),
    )
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(empty_symlink)
    ignored_directory = replace(
        artifact_input(),
        entries=(
            ManifestEntry(
                b"ignored",
                WorktreeEntryKind.DIRECTORY,
                allowed_ignored=True,
            ),
        ),
    )
    decoded = decode_worktree_artifact(encode_worktree_artifact(ignored_directory))
    assert decoded.entries[0].allowed_ignored is True


def small_limits(**changes: int) -> ArtifactLimits:
    values = {
        "max_total_bytes": 4096,
        "max_control_bytes": 512,
        "max_index_bytes": 512,
        "max_manifest_bytes": 2048,
        "max_roots_bytes": 128,
        "max_exact_index_bytes": 512,
        "max_manifest_entries": 20,
        "max_path_bytes": 64,
        "max_depth": 8,
        "max_content_bytes": 128,
        "max_aggregate_content_bytes": 1024,
        "max_roots": 8,
    }
    values.update(changes)
    return ArtifactLimits(**values)


@pytest.mark.parametrize(
    "limits",
    [
        small_limits(max_exact_index_bytes=1),
        small_limits(max_manifest_entries=1),
        small_limits(max_path_bytes=3),
        small_limits(max_depth=1),
        small_limits(max_content_bytes=1),
        small_limits(max_aggregate_content_bytes=1, max_content_bytes=1),
        small_limits(max_roots=1),
        small_limits(max_total_bytes=1),
        small_limits(max_control_bytes=1),
        small_limits(max_index_bytes=1, max_exact_index_bytes=1),
        small_limits(max_manifest_bytes=1, max_aggregate_content_bytes=1, max_content_bytes=1),
        small_limits(max_roots_bytes=1),
    ],
)
def test_encoder_enforces_each_limit(limits: ArtifactLimits) -> None:
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(artifact_input(), limits=limits)


def test_encoder_normalizes_unhashable_selector_types() -> None:
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), object_format=[]))  # type: ignore[arg-type]
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(replace(artifact_input(), head_state=[]))  # type: ignore[arg-type]


def test_encoder_checks_aggregate_sizes_before_building_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("payload builder ran before size validation")

    monkeypatch.setattr(artifact_module, "_build_manifest_payload", unexpected)
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(
            artifact_input(),
            limits=small_limits(
                max_manifest_bytes=64,
                max_aggregate_content_bytes=64,
                max_content_bytes=64,
            ),
        )


def test_encoder_checks_total_size_before_framing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("framing ran before total-size validation")

    monkeypatch.setattr(artifact_module, "_frame_envelope", unexpected)
    with pytest.raises(WorktreeArtifactEncodeError):
        encode_worktree_artifact(artifact_input(), limits=small_limits(max_total_bytes=100))


def test_limit_object_rejects_boolean_negative_and_inconsistent_values() -> None:
    with pytest.raises(ValueError):
        small_limits(max_roots=True)
    with pytest.raises(ValueError):
        small_limits(max_roots=-1)
    with pytest.raises(ValueError):
        small_limits(max_index_bytes=1, max_exact_index_bytes=2)
    with pytest.raises(ValueError):
        small_limits(max_content_bytes=9, max_aggregate_content_bytes=8)


def test_decoder_rejects_envelope_corruption() -> None:
    encoded = encode_worktree_artifact(artifact_input())
    corruptions = [
        encoded[:-1],
        encoded + b"trailing",
        b"BADMAGIC" + encoded[8:],
        encoded[:8] + (2).to_bytes(4, "big") + encoded[12:],
        encoded[:12] + (0).to_bytes(4, "big") + encoded[16:],
        encoded[:16] + (3).to_bytes(4, "big") + encoded[20:],
    ]
    for corrupted in corruptions:
        with pytest.raises(WorktreeArtifactDecodeError):
            decode_worktree_artifact(corrupted)


def test_decoder_rejects_digest_unknown_section_and_reordering() -> None:
    encoded = encode_worktree_artifact(artifact_input())
    damaged = bytearray(encoded)
    damaged[-1] ^= 1
    with pytest.raises(WorktreeArtifactDecodeError):
        decode_worktree_artifact(bytes(damaged))

    values = sections(encoded)
    with pytest.raises(WorktreeArtifactDecodeError):
        decode_worktree_artifact(reframe([(9, values[0][1]), *values[1:]]))
    with pytest.raises(WorktreeArtifactDecodeError):
        decode_worktree_artifact(reframe([values[1], values[0], *values[2:]]))


def test_decoder_rejects_control_count_and_aggregate_mismatches() -> None:
    source = artifact_input()
    values = sections(encode_worktree_artifact(source))
    entry_offset, roots_offset, aggregate_offset = control_offsets(source)
    for offset, replacement in (
        (entry_offset, (99).to_bytes(4, "big")),
        (roots_offset, (99).to_bytes(4, "big")),
        (aggregate_offset, (99).to_bytes(8, "big")),
    ):
        control = bytearray(values[0][1])
        control[offset : offset + len(replacement)] = replacement
        corrupted = reframe([(1, bytes(control)), *values[1:]])
        with pytest.raises(WorktreeArtifactDecodeError):
            decode_worktree_artifact(corrupted)


def test_decoder_rejects_manifest_kind_flags_parent_and_order_corruption() -> None:
    values = sections(encode_worktree_artifact(artifact_input()))
    manifest = values[2][1]
    corruptions: list[bytes] = []
    unknown_kind = bytearray(manifest)
    unknown_kind[0] = 9
    corruptions.append(bytes(unknown_kind))
    unknown_flags = bytearray(manifest)
    unknown_flags[1] = 0x80
    corruptions.append(bytes(unknown_flags))
    parent_mismatch = bytearray(manifest)
    second_entry = 14 + len(b"dir")
    parent_mismatch[second_entry + 2 : second_entry + 6] = (1).to_bytes(4, "big")
    corruptions.append(bytes(parent_mismatch))
    for payload in corruptions:
        with pytest.raises(WorktreeArtifactDecodeError):
            decode_worktree_artifact(reframe([*values[:2], (3, payload), values[3]]))


def test_decoder_enforces_consumer_limits_before_payload_conversion() -> None:
    encoded = encode_worktree_artifact(artifact_input())
    for limits in (
        small_limits(max_total_bytes=1),
        small_limits(max_control_bytes=1),
        small_limits(max_index_bytes=1, max_exact_index_bytes=1),
        small_limits(max_manifest_bytes=1, max_aggregate_content_bytes=1, max_content_bytes=1),
        small_limits(max_roots_bytes=1),
    ):
        with pytest.raises(WorktreeArtifactDecodeError):
            decode_worktree_artifact(encoded, limits=limits)


def test_exact_limits_pass_and_one_byte_less_fails() -> None:
    source = artifact_input()
    encoded = encode_worktree_artifact(source)
    payloads = [payload for _, payload in sections(encoded)]
    aggregate = sum(
        len(entry.content)
        for entry in source.entries
        if entry.kind is not WorktreeEntryKind.DIRECTORY
    )
    exact = ArtifactLimits(
        max_total_bytes=len(encoded),
        max_control_bytes=len(payloads[0]),
        max_index_bytes=len(payloads[1]),
        max_manifest_bytes=len(payloads[2]),
        max_roots_bytes=len(payloads[3]),
        max_exact_index_bytes=len(payloads[1]),
        max_manifest_entries=len(source.entries),
        max_path_bytes=max(
            len(source.head_target),
            *(len(entry.raw_path) for entry in source.entries),
        ),
        max_depth=2,
        max_content_bytes=max(len(entry.content) for entry in source.entries),
        max_aggregate_content_bytes=aggregate,
        max_roots=len(source.root_oids),
    )
    assert encode_worktree_artifact(source, limits=exact) == encoded
    assert decode_worktree_artifact(encoded, limits=exact).total_bytes == len(encoded)
    with pytest.raises(WorktreeArtifactDecodeError):
        decode_worktree_artifact(encoded, limits=replace(exact, max_total_bytes=len(encoded) - 1))


def test_errors_do_not_include_raw_path_or_content() -> None:
    sentinel = b"customer-secret-sentinel"
    value = replace(
        artifact_input(),
        entries=(ManifestEntry(b"../" + sentinel, WorktreeEntryKind.FILE, sentinel),),
    )
    with pytest.raises(WorktreeArtifactEncodeError) as excinfo:
        encode_worktree_artifact(value)
    assert "customer-secret-sentinel" not in str(excinfo.value)


def test_source_state_digest_binds_every_section() -> None:
    source = artifact_input()
    original = decode_worktree_artifact(encode_worktree_artifact(source))
    changed_entry = replace(source.entries[1], content=b"changed")
    changed = replace(source, entries=(source.entries[0], changed_entry, source.entries[2]))
    different = decode_worktree_artifact(encode_worktree_artifact(changed))
    assert original.source_state_sha256 != different.source_state_sha256

    digests = [
        hashlib.sha256(payload).digest()
        for _, payload in sections(encode_worktree_artifact(source))
    ]
    expected = hashlib.sha256(b"yinshi-replica-source-v1\x00" + b"".join(digests)).digest()
    assert artifact_module.compute_source_state_sha256(*digests) == expected
    assert expected == original.source_state_sha256
