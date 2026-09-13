"""Fixed privileged prepare and reclaim actions stay narrow and disabled."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from yinshi import root_launcher as root_launcher_module
from yinshi.root_launcher import (
    BROKER_UID,
    LaunchLayout,
    LaunchObjectError,
    LaunchObjectValidator,
    PrepareDisabledError,
    PrepareObjectError,
    ReclaimDisabledError,
    ReclaimObjectError,
    ReplicaLifecycleValidator,
    RootLauncher,
    RootLauncherProtocolError,
    _cgroup_processes,
    _unit_exists,
    parse_launcher_request,
)

OPERATION_ID = "a" * 32


def frame(action: str, **extra: object) -> bytes:
    return json.dumps(
        {
            "action": action,
            "operation_id": OPERATION_ID,
            "protocol_version": "yinshi-root-launcher-v1",
            **extra,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_protocol_accepts_only_fixed_actions_and_fields() -> None:
    for action in ("launch", "prepare", "reclaim"):
        request = parse_launcher_request(frame(action), peer_uid=BROKER_UID)
        assert request.action == action
        assert request.operation_id == OPERATION_ID

    with pytest.raises(RootLauncherProtocolError):
        parse_launcher_request(frame("prepare", path="/tmp/foreign"), peer_uid=BROKER_UID)
    with pytest.raises(RootLauncherProtocolError):
        parse_launcher_request(frame("delete"), peer_uid=BROKER_UID)


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    ((0, "not-found\n", False), (0, "loaded\n", True)),
)
def test_unit_state_requires_exact_success(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    output: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        root_launcher_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=output),
    )
    assert _unit_exists("fixed.service") is expected


@pytest.mark.parametrize(("returncode", "output"), ((1, ""), (0, ""), (0, "unknown")))
def test_unit_state_uncertainty_fails_closed(
    monkeypatch: pytest.MonkeyPatch, returncode: int, output: str
) -> None:
    monkeypatch.setattr(
        root_launcher_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=output),
    )
    with pytest.raises(LaunchObjectError):
        _unit_exists("fixed.service")


def test_cgroup_events_cover_descendants(tmp_path: Path) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="utf-8")
    assert _cgroup_processes(cgroup) == [1]
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="utf-8")
    assert _cgroup_processes(cgroup) == []


def test_replica_actions_are_disabled_without_both_gates() -> None:
    launcher = RootLauncher()

    with pytest.raises(PrepareDisabledError):
        launcher.prepare(frame("prepare"), peer_uid=BROKER_UID)
    with pytest.raises(ReclaimDisabledError):
        launcher.reclaim(frame("reclaim"), peer_uid=BROKER_UID)


def lifecycle_layout(tmp_path: Path) -> LaunchLayout:
    staging = tmp_path / "staging"
    replicas = tmp_path / "replicas"
    runtime = tmp_path / "runtime"
    socket_pins = tmp_path / "socket-pins"
    quarantine = tmp_path / "quarantine"
    for path, mode in (
        (staging, 0o700),
        (replicas, 0o711),
        (runtime, 0o711),
        (socket_pins, 0o700),
        (quarantine, 0o700),
    ):
        path.mkdir()
        path.chmod(mode)
    return LaunchLayout(
        staging_root=staging,
        replicas_root=replicas,
        runtime_root=runtime,
        socket_pins_root=socket_pins,
        executable=tmp_path / "executor",
        systemd_run=tmp_path / "systemd-run",
        quarantine_root=quarantine,
    )


def stage_operation(layout: LaunchLayout) -> tuple[Path, Path, Path]:
    operation = layout.staging_root / OPERATION_ID
    repo = operation / "repo"
    home = operation / "home"
    repo.mkdir(parents=True, mode=0o700)
    operation.chmod(0o700)
    home.mkdir(mode=0o700)
    return operation, repo, home


def lifecycle_validator(layout: LaunchLayout) -> ReplicaLifecycleValidator:
    current_uid = os.getuid()
    current_gid = os.getgid()
    return ReplicaLifecycleValidator(
        layout=layout,
        root_uid=current_uid,
        root_gid=current_gid,
        broker_uid=current_uid,
        executor_uid=current_uid + 1,
        account_ids=lambda name: (
            (current_uid, current_gid)
            if name == "yinshi-broker"
            else (current_uid + 1, current_gid)
        ),
        validate_all_identities=False,
        unit_exists=lambda _unit: False,
        cgroup_processes=lambda _path: None,
        mount_id=lambda _descriptor: 1,
        transfer_fd_ownership=lambda _fd, _uid, _gid: None,
        transfer_entry_ownership=lambda *_args, **_kwargs: None,
        confirmed_owner=lambda _metadata, _uid: True,
    )


def test_prepare_promotes_fixed_staging_tree_and_is_retryable(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    operation, repo, _home = stage_operation(layout)
    (repo / "tracked").write_text("content", encoding="utf-8")
    (repo / "link").symlink_to("tracked")
    validator = lifecycle_validator(layout)

    with validator.prepared(OPERATION_ID) as locations:
        assert locations.replica == layout.replicas_root / OPERATION_ID / "repo"
        assert locations.home == layout.replicas_root / OPERATION_ID / "home"
    assert not operation.exists()
    with validator.prepared(OPERATION_ID):
        pass

    prepared_repo = layout.replicas_root / OPERATION_ID / "repo"
    assert (prepared_repo / "tracked").read_text(encoding="utf-8") == "content"
    assert (prepared_repo / "link").is_symlink()
    assert stat.S_IMODE((layout.replicas_root / OPERATION_ID).stat().st_mode) == 0o700


def test_prepare_persists_both_slot_markers_before_recursive_transfer(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    operation, repo, home = stage_operation(layout)
    (repo / "tracked").write_text("content", encoding="utf-8")
    validator = lifecycle_validator(layout)
    owner_changes: list[int] = []
    operation_inode = operation.stat().st_ino
    repo_inode = repo.stat().st_ino
    home_inode = home.stat().st_ino

    def transfer(descriptor: int, _uid: int, _gid: int) -> None:
        metadata = os.fstat(descriptor)
        owner_changes.append(metadata.st_ino)
        if stat.S_ISREG(metadata.st_mode):
            raise OSError("recursive transfer failed")

    validator._transfer_fd_ownership = transfer

    with (
        pytest.raises(PrepareObjectError, match="ownership transfer failed"),
        validator.prepared(OPERATION_ID),
    ):
        pass

    assert owner_changes[:3] == [operation_inode, repo_inode, home_inode]
    assert (layout.replicas_root / OPERATION_ID).exists()


@pytest.mark.parametrize("partial_bytes", [0, 3])
def test_prepare_recovers_incomplete_identity_marker_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial_bytes: int,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    original_write = root_launcher_module.os.write
    calls = {"count": 0}

    def short_write(descriptor: int, payload: bytes) -> int:
        calls["count"] += 1
        if calls["count"] == 1 and partial_bytes:
            return original_write(descriptor, payload[:partial_bytes])
        return 0

    monkeypatch.setattr(root_launcher_module.os, "write", short_write)
    with (
        pytest.raises(PrepareObjectError, match="marker write is incomplete"),
        validator.prepared(OPERATION_ID),
    ):
        pass
    operation = layout.replicas_root / OPERATION_ID
    assert (operation / ".executor-identity.pending").exists()
    assert not (operation / ".executor-identity").exists()

    monkeypatch.setattr(root_launcher_module.os, "write", original_write)
    restarted = lifecycle_validator(layout)
    with restarted.prepared(OPERATION_ID):
        pass
    assert (operation / ".executor-identity").read_text(encoding="ascii") == ("yinshi-executor-00")
    assert not (operation / ".executor-identity.pending").exists()


def test_prepare_syncs_destination_before_source_after_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    original_fsync = root_launcher_module.os.fsync
    root_identities = {
        (layout.replicas_root.stat().st_dev, layout.replicas_root.stat().st_ino): "destination",
        (layout.staging_root.stat().st_dev, layout.staging_root.stat().st_ino): "source",
    }
    order: list[str] = []

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        label = root_identities.get((metadata.st_dev, metadata.st_ino))
        if label is not None:
            order.append(label)
        original_fsync(descriptor)

    monkeypatch.setattr(root_launcher_module.os, "fsync", record_fsync)
    with validator.prepared(OPERATION_ID):
        pass

    assert order[:2] == ["destination", "source"]


def test_prepare_result_satisfies_launch_prepared_invariant(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    current_uid = os.getuid()
    current_gid = os.getgid()
    with lifecycle_validator(layout).prepared(OPERATION_ID):
        pass

    tmp_path.chmod(0o755)
    layout.executable.write_bytes(b"executor")
    layout.executable.chmod(0o500)
    layout.systemd_run.write_bytes(b"systemd-run")
    layout.systemd_run.chmod(0o500)
    validator = LaunchObjectValidator(
        layout=layout,
        root_uid=current_uid,
        root_gid=current_gid,
        broker_uid=current_uid,
        executor_uid=current_uid + 1,
        broker_gid=current_gid,
        executor_gid=current_gid,
        account_ids=lambda name: (
            (current_uid, current_gid)
            if name == "yinshi-broker"
            else (current_uid + 1, current_gid)
        ),
        validate_all_identities=False,
        unit_exists=lambda _unit: False,
        mount_id=lambda _descriptor: 1,
        confirmed_owner=lambda _metadata, _uid: True,
    )
    validator._validate_socket = lambda _locations, broker_gid, executor_gid: [
        os.open(layout.runtime_root, os.O_RDONLY | os.O_DIRECTORY)
    ]
    validator._pin_socket = lambda _locations, _runtime, broker_gid, executor_gid: []
    validator._validate_regular_executable = lambda _path: []
    with validator.validate(OPERATION_ID) as locations:
        assert locations.operation_root == layout.replicas_root / OPERATION_ID


def test_prepare_rejects_staging_and_prepared_ambiguity(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    prepared = layout.replicas_root / OPERATION_ID
    (prepared / "repo").mkdir(parents=True, mode=0o700)
    prepared.chmod(0o700)
    (prepared / "home").mkdir(mode=0o700)

    with (
        pytest.raises(PrepareObjectError, match="ambiguous"),
        lifecycle_validator(layout).prepared(OPERATION_ID),
    ):
        pass


def test_prepare_rejects_source_swap_during_promotion(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    original, _repo, _home = stage_operation(layout)
    replacement_name = "b" * 32
    replacement = layout.staging_root / replacement_name
    (replacement / "repo").mkdir(parents=True, mode=0o700)
    replacement.chmod(0o700)
    (replacement / "home").mkdir(mode=0o700)

    def swap_then_promote(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        os.rename(source, "original", src_dir_fd=src_dir_fd, dst_dir_fd=src_dir_fd)
        os.rename(
            replacement_name,
            source,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=src_dir_fd,
        )
        os.rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    validator = lifecycle_validator(layout)
    validator._promote_operation = swap_then_promote

    with pytest.raises(PrepareObjectError, match="replaced"), validator.prepared(OPERATION_ID):
        pass

    assert not original.exists()


def test_prepare_retry_continues_after_promotion_failure(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    calls = {"count": 0}

    def fail_first_ownership(_fd: int, _uid: int, _gid: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected transition failure")

    validator._transfer_fd_ownership = fail_first_ownership
    with (
        pytest.raises(PrepareObjectError, match="ownership transfer"),
        validator.prepared(OPERATION_ID),
    ):
        pass

    assert not (layout.staging_root / OPERATION_ID).exists()
    assert (layout.replicas_root / OPERATION_ID).exists()
    validator._transfer_fd_ownership = lambda _fd, _uid, _gid: None
    with validator.prepared(OPERATION_ID):
        pass


def test_prepare_retry_resynchronizes_both_promotion_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    original_fsync = root_launcher_module.os.fsync
    calls = {"count": 0}

    def fail_first_fsync(descriptor: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected parent sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(root_launcher_module.os, "fsync", fail_first_fsync)
    with (
        pytest.raises(PrepareObjectError, match="ownership transfer"),
        validator.prepared(OPERATION_ID),
    ):
        pass

    assert not (layout.staging_root / OPERATION_ID).exists()
    assert (layout.replicas_root / OPERATION_ID).exists()
    monkeypatch.setattr(root_launcher_module.os, "fsync", original_fsync)
    with validator.prepared(OPERATION_ID):
        pass


def test_reclaim_retry_resynchronizes_both_rename_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    original_fsync = root_launcher_module.os.fsync
    calls = {"count": 0}

    def fail_rename_parent_fsync(descriptor: int) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected parent sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(root_launcher_module.os, "fsync", fail_rename_parent_fsync)
    with (
        pytest.raises(ReclaimObjectError, match="removal failed"),
        validator.reclaimed(OPERATION_ID),
    ):
        pass

    assert not (layout.replicas_root / OPERATION_ID).exists()
    assert (layout.quarantine_root / OPERATION_ID).exists()
    monkeypatch.setattr(root_launcher_module.os, "fsync", original_fsync)
    with validator.reclaimed(OPERATION_ID):
        pass
    tombstone = layout.quarantine_root / OPERATION_ID
    assert {entry.name for entry in tombstone.iterdir()} == {".executor-identity"}


def test_reclaim_ignores_executor_controlled_top_tree_modes(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    prepared = layout.replicas_root / OPERATION_ID
    (prepared / "repo").chmod(0o707)
    (prepared / "home").chmod(0o777)

    with validator.reclaimed(OPERATION_ID):
        pass

    assert not (layout.replicas_root / OPERATION_ID).exists()
    tombstone = layout.quarantine_root / OPERATION_ID
    assert {entry.name for entry in tombstone.iterdir()} == {".executor-identity"}


def test_reclaim_removes_socket_pin_before_last_identity_tree(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    events: list[str] = []
    remove_tree = validator._remove_tree
    validator._validate_socket_pin = lambda _operation_id: True
    validator._remove_socket_pin = lambda _operation_id: events.append("pin")

    def recording_remove_tree(*args: object, **kwargs: object) -> None:
        events.append("tree")
        remove_tree(*args, **kwargs)

    validator._remove_tree = recording_remove_tree

    with validator.reclaimed(OPERATION_ID):
        pass

    assert events[:2] == ["pin", "tree"]


def test_reclaim_removes_valid_socket_pin_after_quiescence(tmp_path: Path) -> None:
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir="/private/tmp"))
    layout = replace(lifecycle_layout(tmp_path), socket_pins_root=short_root)
    short_root.chmod(0o700)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    pin = layout.socket_pins_root / OPERATION_ID
    pinned_socket = socket.socket(socket.AF_UNIX)
    try:
        pinned_socket.bind(str(pin))
        os.chown(pin, -1, os.getgid())
        pin.chmod(0o660)
        with validator.reclaimed(OPERATION_ID):
            pass
        assert not pin.exists()
    finally:
        pinned_socket.close()
        shutil.rmtree(short_root)


@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_reclaim_removes_temporary_socket_pin_after_interrupted_publication(
    tmp_path: Path,
    mode: int,
) -> None:
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir="/private/tmp"))
    layout = replace(lifecycle_layout(tmp_path), socket_pins_root=short_root)
    short_root.chmod(0o700)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    pending_pin = layout.socket_pins_root / f".{OPERATION_ID}.pending"
    pinned_socket = socket.socket(socket.AF_UNIX)
    try:
        pinned_socket.bind(str(pending_pin))
        os.chown(pending_pin, -1, os.getgid())
        pending_pin.chmod(mode)
        with validator.reclaimed(OPERATION_ID):
            pass
        assert not pending_pin.exists()
    finally:
        pinned_socket.close()
        shutil.rmtree(short_root)


def test_reclaim_rejects_malformed_pin_before_replica_deletion(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    pin = layout.socket_pins_root / OPERATION_ID
    pin.write_text("not a socket")
    pin.chmod(0o660)

    with (
        pytest.raises(ReclaimObjectError, match="socket pin"),
        validator.reclaimed(OPERATION_ID),
    ):
        pass

    assert (layout.replicas_root / OPERATION_ID / "repo").is_dir()
    assert pin.exists()


def test_reclaim_handles_tree_deeper_than_python_recursion_limit(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    with validator.prepared(OPERATION_ID):
        pass
    current = os.open(
        layout.replicas_root / OPERATION_ID / "repo",
        os.O_RDONLY | os.O_DIRECTORY,
    )
    try:
        for _index in range(1_100):
            os.mkdir("d", dir_fd=current)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        descriptor = os.open("file", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=current)
        os.close(descriptor)
    finally:
        os.close(current)

    with validator.reclaimed(OPERATION_ID):
        pass

    tombstone = layout.quarantine_root / OPERATION_ID
    assert {entry.name for entry in tombstone.iterdir()} == {".executor-identity"}


def test_prepare_rejects_hard_linked_symlink_before_ownership_transfer(
    tmp_path: Path,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    source = layout.staging_root / OPERATION_ID / "repo" / "linked-symbolic"
    source.symlink_to("payload.txt")
    external = tmp_path / "external-symbolic"
    os.link(source, external, follow_symlinks=False)

    with (
        pytest.raises(PrepareObjectError, match="symbolic link has multiple links"),
        lifecycle_validator(layout).prepared(OPERATION_ID),
    ):
        pass

    assert source.is_symlink()
    assert external.is_symlink()
    assert os.lstat(source).st_uid == os.lstat(external).st_uid


def test_prepare_rejects_unknown_operation_entry(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    operation, _repo, _home = stage_operation(layout)
    (operation / "late-entry").write_text("reject", encoding="utf-8")

    with (
        pytest.raises(PrepareObjectError, match="operation entries"),
        lifecycle_validator(layout).prepared(OPERATION_ID),
    ):
        pass


def test_prepare_rejects_changed_mount_identity(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    validator = lifecycle_validator(layout)
    calls = {"count": 0}

    def changed_mount(_descriptor: int) -> int:
        calls["count"] += 1
        return 2 if calls["count"] == 3 else 1

    validator._mount_id = changed_mount

    with (
        pytest.raises(PrepareObjectError, match="identity"),
        validator.prepared(OPERATION_ID),
    ):
        pass


def test_prepare_closes_partial_top_directory_handles(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    operation = layout.staging_root / OPERATION_ID
    (operation / "repo").mkdir(parents=True, mode=0o700)
    operation.chmod(0o700)
    validator = lifecycle_validator(layout)
    before = len(os.listdir("/dev/fd"))

    for _ in range(20):
        with pytest.raises(PrepareObjectError), validator.prepared(OPERATION_ID):
            pass

    assert len(os.listdir("/dev/fd")) <= before + 1


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "fifo"))
def test_prepare_rejects_unsafe_entries(tmp_path: Path, unsafe_kind: str) -> None:
    layout = lifecycle_layout(tmp_path)
    _operation, repo, _home = stage_operation(layout)
    entry = repo / "entry"
    if unsafe_kind == "hardlink":
        source = repo / "source"
        source.write_text("x", encoding="utf-8")
        os.link(source, entry)
    else:
        os.mkfifo(entry)

    with (
        pytest.raises(PrepareObjectError),
        lifecycle_validator(layout).prepared(OPERATION_ID),
    ):
        pass


@pytest.mark.parametrize("active_state", ("unit", "runtime", "cgroup"))
def test_prepare_rejects_active_state_before_ownership_mutation(
    tmp_path: Path,
    active_state: str,
) -> None:
    layout = lifecycle_layout(tmp_path)
    stage_operation(layout)
    ownership_calls: list[int] = []
    validator = lifecycle_validator(layout)
    validator._transfer_fd_ownership = lambda fd, _uid, _gid: ownership_calls.append(fd)
    if active_state == "unit":
        validator._unit_exists = lambda _unit: True
    elif active_state == "runtime":
        (layout.runtime_root / OPERATION_ID).mkdir()
    else:
        validator._cgroup_processes = lambda _path: [1]

    with pytest.raises(PrepareObjectError, match=active_state), validator.prepared(OPERATION_ID):
        pass

    assert ownership_calls == []
    assert (layout.staging_root / OPERATION_ID).exists()
    assert not (layout.replicas_root / OPERATION_ID).exists()


def test_reclaim_rejects_active_runtime_state(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    target = layout.quarantine_root / OPERATION_ID
    target.mkdir(mode=0o700)
    (layout.runtime_root / OPERATION_ID).mkdir()

    with (
        pytest.raises(ReclaimObjectError, match="runtime is not stopped"),
        lifecycle_validator(layout).reclaimed(OPERATION_ID),
    ):
        pass

    assert target.exists()


def test_reclaim_requires_quiescence_and_stays_in_quarantine(tmp_path: Path) -> None:
    layout = lifecycle_layout(tmp_path)
    target = layout.quarantine_root / OPERATION_ID
    (target / "repo" / "nested").mkdir(parents=True)
    (target / "home").mkdir(mode=0o700)
    (target / "repo").chmod(0o700)
    target.chmod(0o700)
    (target / "repo" / "nested" / "file").write_text("x", encoding="utf-8")
    marker = target / ".executor-identity"
    marker.write_text("yinshi-executor-00", encoding="ascii")
    marker.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    validator = lifecycle_validator(layout)

    with validator.reclaimed(OPERATION_ID):
        pass

    assert {entry.name for entry in target.iterdir()} == {".executor-identity"}
    assert outside.read_text(encoding="utf-8") == "preserve"
