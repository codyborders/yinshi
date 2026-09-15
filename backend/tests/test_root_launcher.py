"""Tests for the fixed, disabled-by-default root launcher contract."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import yinshi.root_launcher as root_launcher_module
from yinshi.root_launcher import (
    BROKER_UID,
    EXECUTOR_NAMES,
    ExecutorIdentity,
    LaunchDisabledError,
    LaunchLayout,
    LaunchObjectError,
    LaunchObjectValidator,
    PrepareObjectError,
    ReclaimObjectError,
    RootLauncher,
    RootLauncherProtocolError,
    build_systemd_run_command,
    derive_launch_locations,
    parse_launcher_request,
)

OPERATION_ID = "a" * 32


def _pool_account_ids(name: str) -> tuple[int, int]:
    if name == "yinshi-broker":
        return 100, 200
    if name == "yinshi-app":
        return 101, 201
    slot = int(name[-2:])
    return 300 + slot, 400 + slot


def _request(**extra: object) -> bytes:
    payload: dict[str, object] = {
        "action": "launch",
        "operation_id": OPERATION_ID,
        "protocol_version": "yinshi-root-launcher-v1",
    }
    payload.update(extra)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def test_launcher_derives_all_privileged_locations_and_exact_command() -> None:
    """The operation ID is the launcher's only caller-controlled launch input."""
    locations = derive_launch_locations(OPERATION_ID)

    assert str(LaunchLayout().staging_root) == "/var/lib/yinshi-launcher/staging"
    assert str(locations.replica) == f"/var/lib/yinshi-launcher/replicas/{OPERATION_ID}/repo"
    assert str(locations.home) == f"/var/lib/yinshi-launcher/replicas/{OPERATION_ID}/home"
    assert str(locations.cgroup) == (
        f"/sys/fs/cgroup/yinshi-workloads.slice/yinshi-executor-{OPERATION_ID}.service"
    )
    assert locations.unit == f"yinshi-executor-{OPERATION_ID}.service"
    assert str(locations.executable) == "/opt/yinshi/runtime-v1/bin/yinshi-executor"
    assert (
        str(locations.socket) == f"/run/yinshi-launcher-state/workloads/{OPERATION_ID}/session.sock"
    )
    assert str(locations.socket_pin) == f"/run/yinshi-launcher-state/pins/{OPERATION_ID}"
    assert str(locations.executor_runtime) == "/run"
    assert build_systemd_run_command(OPERATION_ID) == (
        "/usr/bin/systemd-run",
        "--quiet",
        "--collect",
        f"--unit=yinshi-executor-{OPERATION_ID}.service",
        "--slice=yinshi-workloads.slice",
        "--property=User=yinshi-executor-00",
        "--property=Group=yinshi-executor-00",
        "--property=SupplementaryGroups=",
        "--property=CPUQuota=100%",
        "--property=MemoryMax=640M",
        "--property=TasksMax=128",
        "--property=PrivateNetwork=yes",
        "--property=NoNewPrivileges=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=RestrictNamespaces=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes",
        "--property=TemporaryFileSystem=/run:rw",
        "--property=SystemCallArchitectures=native",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        f"--property=BindPaths=/var/lib/yinshi-launcher/replicas/{OPERATION_ID}/repo:/workspace",
        f"--property=BindPaths=/var/lib/yinshi-launcher/replicas/{OPERATION_ID}/home:/home/yinshi",
        (f"--property=BindPaths=/run/yinshi-launcher-state/pins/{OPERATION_ID}:/run/session.sock"),
        "--working-directory=/workspace",
        "--setenv=HOME=/home/yinshi",
        "--setenv=YINSHI_SESSION_SOCKET=/run/session.sock",
        "--",
        "/opt/yinshi/runtime-v1/bin/yinshi-executor",
    )


@pytest.mark.parametrize(
    "frame",
    [
        _request(action="inspect"),
        _request(action=[]),
        _request(operation_id="A" * 32),
        _request(operation_id="../escape"),
        _request(command="id"),
        _request(path="/tmp/x"),
        _request(environment={"TOKEN": "secret"}),
        _request(uid=0),
        _request(mounts=[]),
        _request(systemd_properties={}),
    ],
)
def test_launcher_rejects_every_nonfixed_or_caller_supplied_control(frame: bytes) -> None:
    """Unknown fields and noncanonical operation IDs fail before command execution."""
    with pytest.raises(RootLauncherProtocolError):
        parse_launcher_request(frame, peer_uid=BROKER_UID)


def test_launcher_requires_configured_broker_peer_uid() -> None:
    """Unix peer credentials authorize the broker independently from request syntax."""
    with pytest.raises(RootLauncherProtocolError, match="peer UID"):
        parse_launcher_request(_request(), peer_uid=BROKER_UID + 1)


def test_effectful_launch_is_explicitly_disabled_by_default() -> None:
    """The foundation exposes its fixed command without starting a subprocess."""
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...]) -> None:
        calls.append(command)

    launcher = RootLauncher(
        command_runner=run,
        object_validator=RecordingValidator([]),  # type: ignore[arg-type]
    )
    launcher._allocated_for_existing = lambda *_args, **_kwargs: ExecutorIdentity(
        EXECUTOR_NAMES[0], 300, 400
    )

    assert launcher.execution_enabled is False
    assert launcher.command_for(_request(), peer_uid=BROKER_UID) == build_systemd_run_command(
        OPERATION_ID
    )
    with pytest.raises(LaunchDisabledError, match="disabled"):
        launcher.execute(_request(), peer_uid=BROKER_UID)
    assert calls == []


class RecordingValidator:
    """Record validation lifetime around a modeled command runner."""

    executor_name = EXECUTOR_NAMES[0]

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    @contextmanager
    def validate(self, operation_id: str):
        assert operation_id == OPERATION_ID
        self.events.append("validate-enter")
        if self.fail:
            raise LaunchObjectError("injected unsafe object")
        try:
            yield derive_launch_locations(operation_id)
        finally:
            self.events.append("validate-exit")


def test_enabled_launcher_holds_validation_through_command_acceptance() -> None:
    """Object descriptors remain retained while systemd accepts the fixed command."""
    events: list[str] = []

    def run(_command: tuple[str, ...]) -> None:
        events.append("command")

    launcher = RootLauncher(
        execution_enabled=True,
        command_runner=run,
        object_validator=RecordingValidator(events),  # type: ignore[arg-type]
    )
    launcher._allocated_for_existing = lambda *_args, **_kwargs: ExecutorIdentity(
        EXECUTOR_NAMES[0], 300, 400
    )

    launcher.execute(_request(), peer_uid=BROKER_UID)

    assert events == ["validate-enter", "command", "validate-exit"]


def test_same_operation_prepare_and_launch_are_serialized() -> None:
    """One RootLauncher owns all same-operation lifecycle transitions."""
    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    launch_entered = threading.Event()

    class BlockingLifecycle:
        executor_name = EXECUTOR_NAMES[0]

        @contextmanager
        def prepared(self, operation_id: str):
            assert operation_id == OPERATION_ID
            prepare_entered.set()
            assert release_prepare.wait(timeout=1.0)
            yield derive_launch_locations(operation_id)

        @contextmanager
        def reclaimed(self, operation_id: str):
            yield derive_launch_locations(operation_id)

    class LaunchValidator:
        executor_name = EXECUTOR_NAMES[0]

        @contextmanager
        def validate(self, operation_id: str):
            launch_entered.set()
            yield derive_launch_locations(operation_id)

    launcher = RootLauncher(
        execution_enabled=True,
        prepare_enabled=True,
        command_runner=lambda _command: None,
        object_validator=LaunchValidator(),  # type: ignore[arg-type]
        replica_lifecycle=BlockingLifecycle(),  # type: ignore[arg-type]
    )
    selected = ExecutorIdentity(EXECUTOR_NAMES[0], 300, 400)
    launcher._allocated_for_prepare = lambda _operation_id: selected
    launcher._allocated_for_existing = lambda *_args, **_kwargs: selected
    prepare_thread = threading.Thread(
        target=lambda: launcher.prepare(_request(action="prepare"), peer_uid=BROKER_UID)
    )
    launch_thread = threading.Thread(
        target=lambda: launcher.execute(_request(), peer_uid=BROKER_UID)
    )
    prepare_thread.start()
    assert prepare_entered.wait(timeout=1.0)
    launch_thread.start()
    assert not launch_entered.wait(timeout=0.05)
    release_prepare.set()
    prepare_thread.join(timeout=1.0)
    launch_thread.join(timeout=1.0)

    assert not prepare_thread.is_alive()
    assert not launch_thread.is_alive()
    assert launch_entered.is_set()


def test_object_validation_failure_prevents_systemd_contact() -> None:
    """An unsafe derived object blocks the privileged command runner."""
    calls: list[tuple[str, ...]] = []
    launcher = RootLauncher(
        execution_enabled=True,
        command_runner=calls.append,
        object_validator=RecordingValidator([], fail=True),  # type: ignore[arg-type]
    )
    launcher._allocated_for_existing = lambda *_args, **_kwargs: ExecutorIdentity(
        EXECUTOR_NAMES[0], 300, 400
    )

    with pytest.raises(LaunchObjectError, match="unsafe object"):
        launcher.execute(_request(), peer_uid=BROKER_UID)

    assert calls == []


def test_injected_validator_cannot_override_persisted_executor_identity() -> None:
    events: list[str] = []
    launcher = RootLauncher(
        execution_enabled=True,
        command_runner=lambda _command: events.append("command"),
        object_validator=RecordingValidator(events),  # type: ignore[arg-type]
    )
    launcher._allocated_for_existing = lambda *_args, **_kwargs: ExecutorIdentity(
        EXECUTOR_NAMES[1], 301, 401
    )

    with pytest.raises(LaunchObjectError, match="validator identity"):
        launcher.execute(_request(), peer_uid=BROKER_UID)

    assert events == []


def test_validator_rejects_executor_membership_in_broker_group(tmp_path: Path) -> None:
    """Executor NSS groups cannot include a protected broker group."""
    validator = LaunchObjectValidator(
        layout=LaunchLayout(
            replicas_root=tmp_path / "replicas",
            runtime_root=tmp_path / "runtime",
            socket_pins_root=tmp_path / "pins",
            executable=tmp_path / "executor",
            systemd_run=tmp_path / "systemd-run",
        ),
        root_uid=0,
        root_gid=0,
        broker_uid=100,
        broker_gid=200,
        executor_uid=300,
        executor_gid=400,
        account_ids=lambda name: (
            (100, 200)
            if name == "yinshi-broker"
            else (
                (101, 201) if name == "yinshi-app" else (300 + int(name[-2:]), 400 + int(name[-2:]))
            )
        ),
        account_groups=lambda name, primary: (
            frozenset({primary, 200}) if name == "yinshi-executor-00" else frozenset({primary})
        ),
        group_ids=lambda name: _pool_account_ids(name)[1],
        unit_exists=lambda _unit: False,
    )

    with (
        pytest.raises(LaunchObjectError, match="supplementary groups"),
        validator.validate(OPERATION_ID),
    ):
        pass


def test_validator_rejects_named_group_alias(tmp_path: Path) -> None:
    """Each systemd group name must resolve to its matching user primary GID."""
    validator = LaunchObjectValidator(
        layout=LaunchLayout(
            replicas_root=tmp_path / "missing-replicas",
            runtime_root=tmp_path / "missing-runtime",
            executable=tmp_path / "missing-executor",
            systemd_run=tmp_path / "missing-systemd-run",
        ),
        root_uid=0,
        root_gid=0,
        broker_uid=100,
        broker_gid=200,
        executor_uid=300,
        executor_gid=400,
        account_ids=_pool_account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: 200 if name == "yinshi-executor-00" else _pool_account_ids(name)[1],
        unit_exists=lambda _unit: False,
    )

    with (
        pytest.raises(LaunchObjectError, match="named group"),
        validator.validate(OPERATION_ID),
    ):
        pass


def test_validator_rejects_account_alias_and_symlinked_runtime_root(tmp_path: Path) -> None:
    """Distinct accounts and no-follow runtime roots are mandatory."""
    current_uid = os.getuid()
    layout = LaunchLayout(
        replicas_root=tmp_path / "replicas",
        runtime_root=tmp_path / "runtime-link",
        executable=tmp_path / "bin" / "executor",
        systemd_run=tmp_path / "bin" / "systemd-run",
    )
    layout.replicas_root.mkdir(mode=0o711)
    real_runtime = tmp_path / "runtime"
    real_runtime.mkdir(mode=0o711)
    layout.runtime_root.symlink_to(real_runtime, target_is_directory=True)

    aliased = LaunchObjectValidator(
        layout=layout,
        root_uid=current_uid,
        broker_uid=current_uid,
        executor_uid=current_uid,
        broker_gid=os.getgid(),
        executor_gid=os.getgid(),
        account_ids=lambda _name: (current_uid, os.getgid()),
        unit_exists=lambda _unit: False,
    )
    with pytest.raises(LaunchObjectError, match="must differ"), aliased.validate(OPERATION_ID):
        pass

    symlinked = LaunchObjectValidator(
        layout=layout,
        root_uid=current_uid,
        broker_uid=current_uid,
        executor_uid=current_uid + 1,
        broker_gid=os.getgid(),
        executor_gid=os.getgid() + 1,
        account_ids=lambda name: (
            (current_uid, os.getgid())
            if name == "yinshi-broker"
            else (current_uid + 1, os.getgid() + 1)
        ),
        validate_all_identities=False,
        unit_exists=lambda _unit: False,
    )
    with pytest.raises(LaunchObjectError, match="directory"), symlinked.validate(OPERATION_ID):
        pass


def test_validator_rejects_existing_unit_before_filesystem_work(tmp_path: Path) -> None:
    """A stale or active unit name blocks reuse of an operation identity."""
    validator = LaunchObjectValidator(
        layout=LaunchLayout(
            replicas_root=tmp_path / "missing-replicas",
            runtime_root=tmp_path / "missing-runtime",
            executable=tmp_path / "missing-executor",
            systemd_run=tmp_path / "missing-systemd-run",
        ),
        root_uid=0,
        broker_uid=999,
        executor_uid=997,
        broker_gid=999,
        executor_gid=997,
        account_ids=lambda name: (
            (999, 999)
            if name == "yinshi-broker"
            else (
                (998, 998) if name == "yinshi-app" else (997 - int(name[-2:]), 997 - int(name[-2:]))
            )
        ),
        group_ids=lambda name: (
            999
            if name == "yinshi-broker"
            else (998 if name == "yinshi-app" else 997 - int(name[-2:]))
        ),
        unit_exists=lambda _unit: True,
    )
    with (
        pytest.raises(LaunchObjectError, match="unit already exists"),
        validator.validate(OPERATION_ID),
    ):
        pass


def short_temp_root() -> Path:
    """Select a short, physical, searchable temp root for pinned Unix sockets."""
    candidate = Path("/private/tmp")
    if not (candidate.is_dir() and not candidate.is_symlink()):
        candidate = Path(tempfile.gettempdir()).resolve()
    assert candidate.is_dir() and not candidate.is_symlink()
    return candidate


def test_validator_pins_socket_before_broker_path_replacement(tmp_path: Path) -> None:
    """Systemd source remains the accepted inode after broker path replacement."""
    current_uid = os.getuid()
    current_gid = os.getgid()
    tmp_path.chmod(0o755)
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir=short_temp_root()))
    short_root.chmod(0o755)
    os.chown(short_root, -1, current_gid)
    layout = LaunchLayout(
        replicas_root=tmp_path / "replicas",
        runtime_root=short_root / "runtime",
        socket_pins_root=short_root / "socket-pins",
        executable=short_root / "executor",
        systemd_run=short_root / "systemd-run",
    )
    layout.replicas_root.mkdir(mode=0o711)
    layout.runtime_root.mkdir(mode=0o711)
    layout.socket_pins_root.mkdir(mode=0o700)
    os.chown(layout.runtime_root, -1, current_gid)
    layout.runtime_root.chmod(0o711)
    os.chown(layout.socket_pins_root, -1, current_gid)
    operation = layout.replicas_root / OPERATION_ID
    (operation / "repo").mkdir(parents=True, mode=0o700)
    operation.chmod(0o700)
    (operation / "home").mkdir(mode=0o700)
    marker = operation / ".executor-identity"
    marker.write_text("yinshi-executor-00", encoding="ascii")
    marker.chmod(0o600)
    runtime = layout.runtime_root / OPERATION_ID
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    for executable in (layout.executable, layout.systemd_run):
        executable.write_bytes(b"binary")
        executable.chmod(0o501)
    original = socket.socket(socket.AF_UNIX)
    original.bind(str(runtime / "session.sock"))
    os.chown(runtime / "session.sock", -1, current_gid)
    os.chmod(runtime / "session.sock", 0o600)
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
        mount_id=lambda descriptor: os.fstat(descriptor).st_dev,
        confirmed_owner=lambda metadata, expected_uid: (
            metadata.st_uid == current_uid and expected_uid in {current_uid, current_uid + 1}
        ),
    )
    try:
        with validator.validate(OPERATION_ID) as locations:
            pinned_before = os.stat(locations.socket_pin)
            original.close()
            locations.socket.unlink()
            replacement = socket.socket(socket.AF_UNIX)
            try:
                replacement.bind(str(locations.socket))
                os.chown(locations.socket, -1, current_gid)
                os.chmod(locations.socket, 0o660)
                pinned_after = os.stat(locations.socket_pin)
                replaced = os.stat(locations.socket)
                assert (pinned_before.st_dev, pinned_before.st_ino) == (
                    pinned_after.st_dev,
                    pinned_after.st_ino,
                )
                assert (pinned_after.st_dev, pinned_after.st_ino) != (
                    replaced.st_dev,
                    replaced.st_ino,
                )
                assert str(locations.socket_pin) in " ".join(
                    build_systemd_run_command(OPERATION_ID, layout=layout)
                )
            finally:
                replacement.close()
    finally:
        original.close()
        shutil.rmtree(short_root)


def test_socket_replacement_during_transition_keeps_broker_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root changes metadata only through its protected temporary hard link."""
    current_uid = os.getuid()
    current_gid = os.getgid()
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir=short_temp_root()))
    runtime = short_root / OPERATION_ID
    pins = short_root / "pins"
    runtime.mkdir(mode=0o700)
    pins.mkdir(mode=0o700)
    os.chown(runtime, -1, current_gid)
    os.chown(pins, -1, current_gid)
    source = runtime / "session.sock"
    original_socket = socket.socket(socket.AF_UNIX)
    original_socket.bind(str(source))
    os.chown(source, -1, current_gid)
    source.chmod(0o600)
    replacement_socket: socket.socket | None = None
    original_chown = root_launcher_module.os.chown

    def replace_before_chown(
        path: str,
        uid: int,
        gid: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal replacement_socket
        source.unlink()
        replacement_socket = socket.socket(socket.AF_UNIX)
        replacement_socket.bind(str(source))
        source.chmod(0o600)
        original_chown(
            path,
            uid,
            gid,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    validator = LaunchObjectValidator(
        layout=LaunchLayout(runtime_root=short_root, socket_pins_root=pins),
        root_uid=current_uid,
        root_gid=current_gid,
        broker_uid=current_uid,
        executor_uid=current_uid,
        broker_gid=current_gid,
        executor_gid=current_gid,
        validate_all_identities=False,
    )
    locations = derive_launch_locations(OPERATION_ID, layout=validator._layout)
    runtime_descriptor = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(root_launcher_module.os, "chown", replace_before_chown)
    try:
        with pytest.raises(LaunchObjectError, match="transition failed"):
            validator._pin_socket(
                locations,
                runtime_descriptor,
                broker_gid=current_gid,
                executor_gid=current_gid,
            )
        replacement = source.stat(follow_symlinks=False)
        assert stat.S_IMODE(replacement.st_mode) == 0o600
        assert replacement.st_gid == current_gid
        assert not locations.socket_pin.exists()
        assert not (pins / f".{OPERATION_ID}.pending").exists()
    finally:
        os.close(runtime_descriptor)
        original_socket.close()
        if replacement_socket is not None:
            replacement_socket.close()
        shutil.rmtree(short_root)


def test_validator_maps_missing_fixed_root_to_stable_error(tmp_path: Path) -> None:
    """Missing fixed roots must not escape as raw filesystem errors."""
    current_uid = os.getuid()
    current_gid = os.getgid()
    validator = LaunchObjectValidator(
        layout=LaunchLayout(
            replicas_root=tmp_path / "missing-replicas",
            runtime_root=tmp_path / "missing-runtime",
            executable=tmp_path / "missing-executor",
            systemd_run=tmp_path / "missing-systemd-run",
        ),
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
    )
    with pytest.raises(LaunchObjectError), validator.validate(OPERATION_ID):
        pass


def test_executable_ancestor_requires_other_search_permission(tmp_path: Path) -> None:
    """Executor-visible files cannot sit below a root-only directory."""
    current_uid = os.getuid()
    current_gid = os.getgid()
    tmp_path.chmod(0o755)
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    executable = protected / "executor"
    executable.write_bytes(b"binary")
    executable.chmod(0o501)
    validator = LaunchObjectValidator(
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
    )

    with pytest.raises(LaunchObjectError, match="not searchable"):
        validator._validate_regular_executable(executable)


def test_executor_requires_other_execute_permission() -> None:
    """Root-owned executables must be executable by the executor account."""
    current_uid = os.getuid()
    current_gid = os.getgid()
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir=short_temp_root()))
    short_root.chmod(0o755)
    os.chown(short_root, -1, current_gid)
    executable = short_root / "executor"
    executable.write_bytes(b"binary")
    executable.chmod(0o500)
    validator = LaunchObjectValidator(
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
    )

    try:
        with pytest.raises(LaunchObjectError, match="executable identity"):
            validator._validate_regular_executable(executable)

        executable.chmod(0o501)
        descriptors = validator._validate_regular_executable(executable)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    finally:
        shutil.rmtree(short_root)


def test_executor_pool_rejects_conflicting_protected_uid(tmp_path: Path) -> None:
    def conflicting_accounts(name: str) -> tuple[int, int]:
        if name == "yinshi-executor-31":
            return _pool_account_ids("yinshi-executor-00")
        return _pool_account_ids(name)

    validator = LaunchObjectValidator(
        layout=LaunchLayout(
            runtime_root=tmp_path / "runtime",
            replicas_root=tmp_path / "replicas",
            socket_pins_root=tmp_path / "pins",
            executable=tmp_path / "executor",
            systemd_run=tmp_path / "systemd-run",
        ),
        root_uid=0,
        root_gid=0,
        broker_uid=100,
        broker_gid=200,
        executor_name="yinshi-executor-00",
        executor_uid=300,
        executor_gid=400,
        account_ids=conflicting_accounts,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: conflicting_accounts(name)[1],
        unit_exists=lambda _unit: True,
    )

    with (
        pytest.raises(LaunchObjectError, match="protected UIDs"),
        validator.validate(OPERATION_ID),
    ):
        pass


def test_prepare_allocation_selects_lowest_free_executor(tmp_path: Path) -> None:
    replicas = tmp_path / "replicas"
    staging = tmp_path / "staging"
    staging.mkdir()
    occupied = replicas / ("b" * 32)
    (occupied / "repo").mkdir(parents=True)
    (occupied / "home").mkdir()
    current_uid = os.getuid()
    current_gid = os.getgid()

    def account_ids(name: str) -> tuple[int, int]:
        if name == "yinshi-broker":
            return current_uid + 100, current_gid + 100
        if name == "yinshi-app":
            return current_uid + 101, current_gid + 101
        slot = int(name[-2:])
        if slot == 0:
            return current_uid, current_gid
        return current_uid + 200 + slot, current_gid + 200 + slot

    launcher = RootLauncher(
        layout=LaunchLayout(replicas_root=replicas, staging_root=staging),
        root_uid=current_uid + 50,
        root_gid=current_gid + 50,
        broker_uid=current_uid + 100,
        broker_gid=current_gid + 100,
        account_ids=account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: account_ids(name)[1],
    )

    assert launcher._allocated_for_prepare(OPERATION_ID).name == "yinshi-executor-01"

    broker_staged = staging / OPERATION_ID
    (broker_staged / "repo").mkdir(parents=True)
    (broker_staged / "home").mkdir()
    assert launcher._allocated_for_prepare(OPERATION_ID).name == "yinshi-executor-01"

    (occupied / "repo").rmdir()
    (occupied / "home").rmdir()
    occupied.rmdir()
    assert launcher._allocated_for_prepare(OPERATION_ID).name == "yinshi-executor-00"


def test_production_launcher_derives_identity_for_each_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replicas = tmp_path / "replicas"
    operation = replicas / OPERATION_ID
    (operation / "repo").mkdir(parents=True)
    (operation / "home").mkdir()
    current_uid = os.getuid()
    current_gid = os.getgid()
    constructed: list[tuple[str, str]] = []

    def account_ids(name: str) -> tuple[int, int]:
        if name == "yinshi-broker":
            return current_uid + 100, current_gid + 100
        if name == "yinshi-app":
            return current_uid + 101, current_gid + 101
        slot = int(name[-2:])
        if slot == 0:
            return current_uid, current_gid
        return current_uid + 200 + slot, current_gid + 200 + slot

    class ObjectValidator:
        def __init__(self, **values: object) -> None:
            self.executor_name = str(values["executor_name"])
            constructed.append(("launch", self.executor_name))

        @contextmanager
        def validate(self, operation_id: str):
            yield derive_launch_locations(operation_id)

    class LifecycleValidator:
        def __init__(self, **values: object) -> None:
            self.executor_name = str(values["executor_name"])
            constructed.append(("lifecycle", self.executor_name))

        @contextmanager
        def reclaimed(self, operation_id: str):
            yield derive_launch_locations(operation_id)

    monkeypatch.setattr(root_launcher_module, "LaunchObjectValidator", ObjectValidator)
    monkeypatch.setattr(root_launcher_module, "ReplicaLifecycleValidator", LifecycleValidator)
    launcher = RootLauncher(
        layout=LaunchLayout(replicas_root=replicas, quarantine_root=tmp_path / "quarantine"),
        root_uid=current_uid + 50,
        root_gid=current_gid + 50,
        broker_uid=current_uid + 100,
        broker_gid=current_gid + 100,
        execution_enabled=True,
        reclaim_enabled=True,
        account_ids=account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: account_ids(name)[1],
        command_runner=lambda _command: None,
    )

    command = launcher.execute(_request(), peer_uid=current_uid + 100)
    launcher.reclaim(_request(action="reclaim"), peer_uid=current_uid + 100)

    assert "--property=User=yinshi-executor-00" in command
    assert constructed == [
        ("launch", "yinshi-executor-00"),
        ("lifecycle", "yinshi-executor-00"),
    ]


def test_injected_lifecycle_identity_mismatch_uses_action_specific_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replicas = tmp_path / "replicas"
    staging = tmp_path / "staging"
    quarantine = tmp_path / "quarantine"
    replicas.mkdir()
    staging.mkdir()
    quarantine.mkdir()

    class MismatchedLifecycle:
        executor_name = EXECUTOR_NAMES[1]

    launcher = RootLauncher(
        layout=LaunchLayout(
            replicas_root=replicas,
            staging_root=staging,
            quarantine_root=quarantine,
        ),
        root_uid=50,
        root_gid=60,
        broker_uid=100,
        broker_gid=200,
        prepare_enabled=True,
        reclaim_enabled=True,
        account_ids=_pool_account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: _pool_account_ids(name)[1],
        replica_lifecycle=MismatchedLifecycle(),  # type: ignore[arg-type]
    )
    selected = ExecutorIdentity(EXECUTOR_NAMES[0], 300, 400)
    monkeypatch.setattr(launcher, "_allocated_for_prepare", lambda _operation_id: selected)
    monkeypatch.setattr(
        launcher,
        "_allocated_for_existing",
        lambda *_args, **_kwargs: selected,
    )

    with pytest.raises(PrepareObjectError, match="identity is invalid"):
        launcher.prepare(_request(action="prepare"), peer_uid=100)
    with pytest.raises(ReclaimObjectError, match="identity is invalid"):
        launcher.reclaim(_request(action="reclaim"), peer_uid=100)


def test_duplicate_active_allocations_block_every_root_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replicas = tmp_path / "replicas"
    staging = tmp_path / "staging"
    quarantine = tmp_path / "quarantine"
    replicas.mkdir()
    staging.mkdir()
    quarantine.mkdir()
    other_operation = "b" * 32
    for operation_id in (OPERATION_ID, other_operation):
        (replicas / operation_id).mkdir()
    selected = ExecutorIdentity(EXECUTOR_NAMES[0], 300, 400)
    runner_calls: list[tuple[str, ...]] = []
    callback_events: list[str] = []

    class LifecycleValidator:
        executor_name = EXECUTOR_NAMES[0]

        @contextmanager
        def prepared(self, _operation_id: str):
            callback_events.append("prepare")
            yield

        @contextmanager
        def reclaimed(self, _operation_id: str):
            callback_events.append("reclaim")
            yield

    launcher = RootLauncher(
        layout=LaunchLayout(
            replicas_root=replicas,
            staging_root=staging,
            quarantine_root=quarantine,
        ),
        root_uid=50,
        root_gid=60,
        broker_uid=100,
        broker_gid=200,
        execution_enabled=True,
        prepare_enabled=True,
        reclaim_enabled=True,
        account_ids=_pool_account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: _pool_account_ids(name)[1],
        command_runner=runner_calls.append,
        object_validator=RecordingValidator(callback_events),  # type: ignore[arg-type]
        replica_lifecycle=LifecycleValidator(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        launcher,
        "_operation_identity",
        lambda *_args, **_kwargs: selected,
    )

    with pytest.raises(PrepareObjectError, match="slot conflicts"):
        launcher.prepare(_request(action="prepare"), peer_uid=100)
    with pytest.raises(LaunchObjectError, match="slot conflicts"):
        launcher.execute(_request(), peer_uid=100)
    with pytest.raises(ReclaimObjectError, match="slot conflicts"):
        launcher.reclaim(_request(action="reclaim"), peer_uid=100)
    assert runner_calls == []
    assert callback_events == []


def test_quarantined_allocation_does_not_reserve_pool_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replicas = tmp_path / "replicas"
    staging = tmp_path / "staging"
    quarantine = tmp_path / "quarantine"
    replicas.mkdir()
    staging.mkdir()
    quarantined = quarantine / ("b" * 32)
    quarantined.mkdir(parents=True)
    launcher = RootLauncher(
        layout=LaunchLayout(
            replicas_root=replicas,
            staging_root=staging,
            quarantine_root=quarantine,
        ),
        root_uid=50,
        root_gid=60,
        broker_uid=100,
        broker_gid=200,
        account_ids=_pool_account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: _pool_account_ids(name)[1],
    )
    monkeypatch.setattr(
        launcher,
        "_operation_identity",
        lambda root, *_args, **_kwargs: (
            ExecutorIdentity(EXECUTOR_NAMES[0], 300, 400) if root.parent == quarantine else None
        ),
    )

    assert launcher._allocated_for_prepare(OPERATION_ID).name == EXECUTOR_NAMES[0]


def test_tree_less_socket_pin_recovers_nonzero_executor_identity(tmp_path: Path) -> None:
    short_root = Path(tempfile.mkdtemp(prefix="yp-", dir=short_temp_root()))
    replicas = short_root / "replicas"
    quarantine = short_root / "quarantine"
    pins = short_root / "pins"
    replicas.mkdir()
    quarantine.mkdir()
    pins.mkdir()
    current_uid = os.getuid()
    current_gid = os.getgid()
    pin_socket = socket.socket(socket.AF_UNIX)
    pin_socket.bind(str(pins / OPERATION_ID))
    os.chown(pins / OPERATION_ID, -1, current_gid)
    (pins / OPERATION_ID).chmod(0o660)

    def account_ids(name: str) -> tuple[int, int]:
        if name == "yinshi-broker":
            return current_uid, current_gid + 500
        if name == "yinshi-app":
            return current_uid + 1, current_gid + 501
        slot = int(name[-2:])
        gid = current_gid if slot == 7 else current_gid + 600 + slot
        return current_uid + 100 + slot, gid

    launcher = RootLauncher(
        layout=LaunchLayout(
            replicas_root=replicas,
            quarantine_root=quarantine,
            socket_pins_root=pins,
        ),
        root_uid=current_uid + 50,
        root_gid=current_gid + 550,
        broker_uid=current_uid,
        broker_gid=current_gid + 500,
        account_ids=account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: account_ids(name)[1],
    )
    try:
        selected = launcher._allocated_for_existing(
            OPERATION_ID,
            error=ReclaimObjectError,
            allow_absent=True,
        )
    finally:
        pin_socket.close()
        shutil.rmtree(short_root)

    assert selected.name == "yinshi-executor-07"


def test_quarantine_tombstone_recovers_nonzero_executor_identity(tmp_path: Path) -> None:
    replicas = tmp_path / "replicas"
    quarantine = tmp_path / "quarantine"
    operation = quarantine / OPERATION_ID
    replicas.mkdir()
    operation.mkdir(parents=True, mode=0o700)
    marker = operation / ".executor-identity"
    marker.write_text("yinshi-executor-07", encoding="ascii")
    marker.chmod(0o600)
    current_uid = os.getuid()
    current_gid = os.getgid()

    def account_ids(name: str) -> tuple[int, int]:
        if name == "yinshi-broker":
            return current_uid + 100, current_gid + 100
        if name == "yinshi-app":
            return current_uid + 101, current_gid + 101
        slot = int(name[-2:])
        return current_uid + 200 + slot, current_gid + 200 + slot

    launcher = RootLauncher(
        layout=LaunchLayout(replicas_root=replicas, quarantine_root=quarantine),
        root_uid=current_uid,
        root_gid=current_gid,
        broker_uid=current_uid + 100,
        broker_gid=current_gid + 100,
        account_ids=account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: account_ids(name)[1],
    )

    selected = launcher._allocated_for_existing(
        OPERATION_ID,
        error=ReclaimObjectError,
        allow_absent=True,
    )

    assert selected.name == "yinshi-executor-07"


@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_tree_less_pending_pin_recovers_nonzero_executor_identity(
    tmp_path: Path,
    mode: int,
) -> None:
    replicas = tmp_path / "replicas"
    quarantine = tmp_path / "quarantine"
    pins = Path(tempfile.mkdtemp(prefix="yp-", dir=Path.home()))
    replicas.mkdir()
    quarantine.mkdir()
    pins.chmod(0o700)
    current_uid = os.getuid()
    current_gid = os.getgid()

    def account_ids(name: str) -> tuple[int, int]:
        if name == "yinshi-broker":
            return current_uid, current_gid + 100
        if name == "yinshi-app":
            return current_uid + 101, current_gid + 101
        slot = int(name[-2:])
        gid = current_gid if slot == 7 else current_gid + 200 + slot
        return current_uid + 200 + slot, gid

    pending = pins / f".{OPERATION_ID}.pending"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(pending))
        pending.chmod(mode)
        launcher = RootLauncher(
            layout=LaunchLayout(
                replicas_root=replicas,
                quarantine_root=quarantine,
                socket_pins_root=pins,
            ),
            root_uid=current_uid + 100,
            root_gid=current_gid + 50,
            broker_uid=current_uid,
            broker_gid=current_gid + 100,
            account_ids=account_ids,
            account_groups=lambda _name, primary: frozenset({primary}),
            group_ids=lambda name: account_ids(name)[1],
        )

        selected = launcher._allocated_for_existing(
            OPERATION_ID,
            error=ReclaimObjectError,
            allow_absent=True,
        )

        assert selected.name == "yinshi-executor-07"
    finally:
        listener.close()
        shutil.rmtree(pins)


def test_pool_exhaustion_fails_before_staging_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = LaunchLayout(
        staging_root=tmp_path / "staging",
        replicas_root=tmp_path / "replicas",
    )
    staged = layout.staging_root / OPERATION_ID
    staged.mkdir(parents=True)
    layout.replicas_root.mkdir()
    for slot in range(32):
        (layout.replicas_root / f"{slot:032x}").mkdir()
    pool = tuple(
        ExecutorIdentity(name, 300 + slot, 400 + slot) for slot, name in enumerate(EXECUTOR_NAMES)
    )
    launcher = RootLauncher(
        layout=layout,
        root_uid=50,
        root_gid=60,
        broker_uid=100,
        broker_gid=200,
        prepare_enabled=True,
        account_ids=_pool_account_ids,
        account_groups=lambda _name, primary: frozenset({primary}),
        group_ids=lambda name: _pool_account_ids(name)[1],
    )

    def allocated(
        root: Path,
        _pool: tuple[ExecutorIdentity, ...],
        **_kwargs: object,
    ) -> ExecutorIdentity | None:
        if root.name == OPERATION_ID:
            return None
        return pool[int(root.name, 16)]

    monkeypatch.setattr(launcher, "_operation_identity", allocated)
    request = _request(action="prepare")

    with pytest.raises(PrepareObjectError, match="pool is exhausted"):
        launcher.prepare(request, peer_uid=100)

    assert staged.exists()


def test_deployment_templates_are_locked_and_not_enabled() -> None:
    """Deployment artifacts lock identities, writable paths, sockets, and activation policy."""
    root = Path(__file__).resolve().parents[2]
    broker = (root / "deploy/systemd/yinshi-broker.service").read_text()
    broker_socket = (root / "deploy/systemd/yinshi-broker.socket").read_text()
    broker_override = (
        root / "deploy/systemd/yinshi-broker.service.d/zzzz-yinshi-hardening.conf"
    ).read_text()
    launcher = (root / "deploy/systemd/yinshi-launcher.service").read_text()
    launcher_socket = (root / "deploy/systemd/yinshi-launcher.socket").read_text()
    launcher_override = (
        root / "deploy/systemd/yinshi-launcher.service.d/zzzz-yinshi-hardening.conf"
    ).read_text()
    tmpfiles = (root / "deploy/tmpfiles.d/yinshi.conf").read_text()
    sysusers = (root / "deploy/sysusers.d/yinshi.conf").read_text()
    workloads = (root / "deploy/systemd/yinshi-workloads.slice").read_text()
    documentation = (root / "docs/broker-root-launcher.md").read_text()
    vm_profile = json.loads((root / "deploy/orbstack/yinshi-runtime.profile.json").read_text())

    assert vm_profile == {
        "architecture": "arm64",
        "cpu_count": 1,
        "disk_gib": 8,
        "distribution": "ubuntu:24.04",
        "execution_enabled": False,
        "isolated": True,
        "memory_mib": 1024,
        "network_isolated": True,
        "profile_version": 1,
    }
    assert "User=yinshi-broker" in broker
    assert "NoNewPrivileges=yes" in broker
    assert (
        "ReadWritePaths=/var/lib/yinshi /run/yinshi "
        "/var/lib/yinshi-launcher/staging /run/yinshi-launcher-state/workloads" in broker
    )
    assert "StateDirectory=" not in broker
    assert "RuntimeDirectory=" not in broker
    legacy_journal_fence = "ReadOnlyPaths=-/var/lib/yinshi/control/broker-journal.sqlite3"
    assert legacy_journal_fence in broker
    assert legacy_journal_fence in broker_override
    assert "broker-journal-v2.sqlite3" in documentation
    assert "SocketGroup=yinshi-app" in broker_socket
    assert "SocketMode=0660" in broker_socket
    for hardening in (
        "NoNewPrivileges=yes",
        "PrivateNetwork=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
    ):
        assert hardening in broker_override
        assert hardening in launcher_override
    assert (
        "ReadWritePaths=/var/lib/yinshi /run/yinshi "
        "/var/lib/yinshi-launcher/staging /run/yinshi-launcher-state/workloads" in broker_override
    )
    assert (
        "ReadWritePaths=/var/lib/yinshi-launcher /run/yinshi-launcher-state"
    ) in launcher_override
    assert (
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER"
        in launcher_override
    )
    assert "User=" not in launcher
    assert "RuntimeDirectory=" not in launcher
    assert "CapabilityBoundingSet=" not in launcher
    assert "SocketMode=0660" in launcher_socket
    assert "SocketUser=root" in launcher_socket
    assert "SocketGroup=yinshi-broker" in launcher_socket
    assert "Environment=YINSHI_LAUNCH_EXECUTION_ENABLED=false" in launcher
    assert "Environment=YINSHI_REPLICA_PREPARE_ENABLED=false" in launcher
    assert "Environment=YINSHI_REPLICA_RECLAIM_ENABLED=false" in launcher
    assert "CPUQuota=100%" in workloads
    assert "MemoryMax=640M" in workloads
    assert "TasksMax=128" in workloads
    assert "d /var/lib/yinshi/control 0700 yinshi-broker yinshi-broker" in tmpfiles
    assert "d /var/lib/yinshi-launcher/staging 0700 yinshi-broker yinshi-broker" in tmpfiles
    assert "d /var/lib/yinshi-launcher/replicas 0711 root root" in tmpfiles
    assert "d /var/lib/yinshi-launcher/quarantine 0700 root root" in tmpfiles
    assert "d /run/yinshi 0710 yinshi-broker yinshi-app" in tmpfiles
    assert "d /run/yinshi-launcher-state/workloads 0711 yinshi-broker yinshi-broker" in tmpfiles
    assert "d /run/yinshi-launcher 0710 root yinshi-broker" in tmpfiles
    executor_accounts = [
        line.split()[1] for line in sysusers.splitlines() if line.startswith("u yinshi-executor-")
    ]
    assert executor_accounts == list(EXECUTOR_NAMES)
    assert "u yinshi-executor " not in sysusers
    assert "false" in documentation
    assert "does not connect to production execution" in documentation
    assert "/var/lib/yinshi-launcher/staging/<operation-id>" in documentation
    assert "Root then owns the operation directory with mode `0700`" in documentation
