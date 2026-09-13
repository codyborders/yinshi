"""Fixed root-owned containment launcher with execution disabled by default."""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "yinshi-app"
BROKER_NAME = "yinshi-broker"
BROKER_UID = 999
EXECUTOR_COUNT = 32
EXECUTOR_NAMES = tuple(f"yinshi-executor-{slot:02d}" for slot in range(EXECUTOR_COUNT))
ROOT_UID = 0
ROOT_LAUNCHER_PROTOCOL_VERSION = "yinshi-root-launcher-v1"
ROOT_LAUNCHER_FRAME_BYTES_MAX = 1_024
LAUNCH_EXECUTION_ENABLED_DEFAULT = False
REPLICA_PREPARE_ENABLED_DEFAULT = False
REPLICA_RECLAIM_ENABLED_DEFAULT = False
QUARANTINE_ROOT = Path("/var/lib/yinshi-launcher/quarantine")
_CGROUP_SLICE_ROOT = Path("/sys/fs/cgroup/yinshi-workloads.slice")
_ALLOWED_ACTIONS = frozenset({"launch", "prepare", "reclaim"})
_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_EXECUTOR_IDENTITY_MARKER = ".executor-identity"
_EXECUTOR_IDENTITY_PENDING = ".executor-identity.pending"
_REQUEST_KEYS = frozenset({"action", "operation_id", "protocol_version"})


class RootLauncherProtocolError(ValueError):
    """Reject any request outside the single fixed launcher action."""


class LaunchDisabledError(RuntimeError):
    """Prevent process launch unless an operator changes the explicit gate."""


class LaunchObjectError(RuntimeError):
    """Reject an unsafe or replaced object before privileged launch."""


class PrepareDisabledError(RuntimeError):
    """Prevent replica prepare unless an operator changes the explicit gate."""


class ReclaimDisabledError(RuntimeError):
    """Prevent quarantine reclaim unless an operator changes the explicit gate."""


class PrepareObjectError(RuntimeError):
    """Reject an unsafe replica tree or interrupted ownership transfer."""


class ReclaimObjectError(RuntimeError):
    """Reject an unsafe or non-quiescent quarantine removal."""


@dataclass(frozen=True, slots=True)
class RootLauncherRequest:
    """The complete accepted input surface for one fixed launcher action."""

    action: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    """One fixed executor pool account selected for an operation."""

    name: str
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class LaunchLayout:
    """Trusted staging, prepared, installation, and runtime roots."""

    staging_root: Path = Path("/var/lib/yinshi-launcher/staging")
    replicas_root: Path = Path("/var/lib/yinshi-launcher/replicas")
    runtime_root: Path = Path("/run/yinshi-launcher-state/workloads")
    socket_pins_root: Path = Path("/run/yinshi-launcher-state/pins")
    executable: Path = Path("/opt/yinshi/runtime-v1/bin/yinshi-executor")
    systemd_run: Path = Path("/usr/bin/systemd-run")
    quarantine_root: Path = QUARANTINE_ROOT


DEFAULT_LAUNCH_LAYOUT = LaunchLayout()


@dataclass(frozen=True, slots=True)
class LaunchLocations:
    """Root-owned locations derived only from one canonical operation ID."""

    operation_root: Path
    replica: Path
    home: Path
    runtime: Path
    cgroup: Path
    unit: str
    executable: Path
    systemd_run: Path
    socket: Path
    socket_pin: Path
    executor_runtime: Path


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RootLauncherProtocolError("duplicate launcher request field")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_launcher_request(
    frame: bytes,
    *,
    peer_uid: int,
    broker_uid: int = BROKER_UID,
) -> RootLauncherRequest:
    """Bind a strict fixed-action request to the kernel-provided broker UID."""
    if type(broker_uid) is not int or broker_uid < 0:
        raise RootLauncherProtocolError("configured broker UID is invalid")
    if type(peer_uid) is not int or peer_uid != broker_uid:
        raise RootLauncherProtocolError("launcher peer UID is not authorized")
    if not isinstance(frame, bytes) or not frame or len(frame) > ROOT_LAUNCHER_FRAME_BYTES_MAX:
        raise RootLauncherProtocolError("launcher request frame size is invalid")
    try:
        value = json.loads(frame.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RootLauncherProtocolError("launcher request JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise RootLauncherProtocolError("launcher request contains missing or unknown fields")
    if _canonical_json(value) != frame:
        raise RootLauncherProtocolError("launcher request JSON is not canonical")
    if value.get("protocol_version") != ROOT_LAUNCHER_PROTOCOL_VERSION:
        raise RootLauncherProtocolError("launcher protocol version is invalid")
    action = value.get("action")
    if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
        raise RootLauncherProtocolError("launcher action is not supported")
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise RootLauncherProtocolError("launcher operation ID is invalid")
    return RootLauncherRequest(action=action, operation_id=operation_id)


def derive_launch_locations(
    operation_id: str,
    *,
    layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
) -> LaunchLocations:
    """Derive every privileged path and name without caller-provided path data."""
    if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise RootLauncherProtocolError("launcher operation ID is invalid")
    if not all(
        path.is_absolute()
        for path in (
            layout.staging_root,
            layout.replicas_root,
            layout.runtime_root,
            layout.socket_pins_root,
            layout.quarantine_root,
        )
    ):
        raise RootLauncherProtocolError("launcher roots must be absolute")
    if not layout.executable.is_absolute() or not layout.systemd_run.is_absolute():
        raise RootLauncherProtocolError("launcher executables must be absolute")
    unit = f"yinshi-executor-{operation_id}.service"
    operation_root = layout.replicas_root / operation_id
    runtime = layout.runtime_root / operation_id
    return LaunchLocations(
        operation_root=operation_root,
        replica=operation_root / "repo",
        home=operation_root / "home",
        runtime=runtime,
        cgroup=_CGROUP_SLICE_ROOT / unit,
        unit=unit,
        executable=layout.executable,
        systemd_run=layout.systemd_run,
        socket=runtime / "session.sock",
        socket_pin=layout.socket_pins_root / operation_id,
        executor_runtime=Path("/run"),
    )


@dataclass(frozen=True, slots=True)
class ReclaimLocations:
    """Root-owned reclaim targets derived from one operation ID."""

    prepared_target: Path
    quarantine_target: Path
    runtime: Path
    cgroup: Path
    unit: str
    socket_pin: Path


def derive_reclaim_locations(
    operation_id: str,
    *,
    layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
) -> ReclaimLocations:
    """Derive every reclaim path without caller-provided path data."""
    if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise RootLauncherProtocolError("launcher operation ID is invalid")
    if not all(
        path.is_absolute()
        for path in (
            layout.quarantine_root,
            layout.runtime_root,
            layout.socket_pins_root,
        )
    ):
        raise RootLauncherProtocolError("launcher roots must be absolute")
    unit = f"yinshi-executor-{operation_id}.service"
    return ReclaimLocations(
        prepared_target=layout.replicas_root / operation_id,
        quarantine_target=layout.quarantine_root / operation_id,
        runtime=layout.runtime_root / operation_id,
        cgroup=_CGROUP_SLICE_ROOT / unit,
        unit=unit,
        socket_pin=layout.socket_pins_root / operation_id,
    )


def build_systemd_run_command(
    operation_id: str,
    executor_name: str = EXECUTOR_NAMES[0],
    *,
    layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
) -> tuple[str, ...]:
    """Build the complete immutable systemd transient-unit argument vector."""
    if executor_name not in EXECUTOR_NAMES:
        raise RootLauncherProtocolError("executor identity name is invalid")
    locations = derive_launch_locations(operation_id, layout=layout)
    return (
        str(locations.systemd_run),
        "--quiet",
        "--collect",
        f"--unit={locations.unit}",
        "--slice=yinshi-workloads.slice",
        f"--property=User={executor_name}",
        f"--property=Group={executor_name}",
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
        f"--property=BindPaths={locations.replica}:/workspace",
        f"--property=BindPaths={locations.home}:/home/yinshi",
        f"--property=BindPaths={locations.socket_pin}:{locations.executor_runtime}/session.sock",
        "--working-directory=/workspace",
        "--setenv=HOME=/home/yinshi",
        f"--setenv=YINSHI_SESSION_SOCKET={locations.executor_runtime}/session.sock",
        "--",
        str(locations.executable),
    )


def _run_command(command: tuple[str, ...]) -> None:
    subprocess.run(command, check=True, close_fds=True)


def _account_ids(name: str) -> tuple[int, int]:
    account = pwd.getpwnam(name)
    return account.pw_uid, account.pw_gid


def _account_groups(name: str, primary_gid: int) -> frozenset[int]:
    return frozenset(os.getgrouplist(name, primary_gid))


def _group_id(name: str) -> int:
    return grp.getgrnam(name).gr_gid


def _unit_exists(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", "--property=LoadState", "--value", unit],
            check=False,
            close_fds=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchObjectError("launcher unit state is unavailable") from exc
    if result.returncode != 0:
        raise LaunchObjectError("launcher unit state is unavailable")
    state = result.stdout.strip()
    if state == "not-found":
        return False
    if state not in {
        "bad-setting",
        "error",
        "generated",
        "loaded",
        "masked",
        "merged",
        "stub",
        "transient",
    }:
        raise LaunchObjectError("launcher unit state is invalid")
    return True


def _mode_bits(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _physical_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _mount_id(descriptor: int) -> int:
    try:
        raw = Path(f"/proc/self/fdinfo/{descriptor}").read_bytes()
    except OSError as exc:
        raise RuntimeError("mount identity is unavailable") from exc
    values = [line.split()[1] for line in raw.splitlines() if line.startswith(b"mnt_id:")]
    if len(values) != 1:
        raise RuntimeError("mount identity is invalid")
    try:
        return int(values[0])
    except ValueError as exc:
        raise RuntimeError("mount identity is invalid") from exc


def _walk_to_fixed_root(
    path: Path,
    *,
    final_uid: int,
    final_mode: int,
    ancestor_uids: frozenset[int],
    error: Callable[[str], RuntimeError],
    mount_id: Callable[[int], int],
) -> tuple[list[int], int, int]:
    if not path.is_absolute() or path == Path("/"):
        raise error("fixed root path is invalid")
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        parts = path.parts[1:]
        for index, component in enumerate(parts):
            descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptors[-1],
            )
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if index == len(parts) - 1:
                if metadata.st_uid != final_uid or _mode_bits(metadata) != final_mode:
                    raise error("fixed root ownership or mode is invalid")
            else:
                mode = _mode_bits(metadata)
                sticky_root_directory = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
                if metadata.st_uid not in ancestor_uids or (
                    mode & 0o022 and not sticky_root_directory
                ):
                    raise error("fixed root ancestor is writable or foreign")
        return (
            descriptors,
            os.fstat(descriptors[-1]).st_dev,
            mount_id(descriptors[-1]),
        )
    except (OSError, RuntimeError) as exc:
        _close_descriptors(descriptors)
        raise error("fixed root validation failed") from exc
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _open_contained_child(
    parent_fd: int,
    name: str,
    *,
    expected_dev: int,
    final_uids: frozenset[int],
    final_mode: int | None,
    error: Callable[[str], RuntimeError],
    expected_mount_id: int | None = None,
    mount_id: Callable[[int], int] = _mount_id,
) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise error("contained directory open failed") from exc
    try:
        metadata = os.fstat(descriptor)
        valid = (
            metadata.st_dev == expected_dev
            and metadata.st_uid in final_uids
            and (final_mode is None or _mode_bits(metadata) == final_mode)
            and (expected_mount_id is None or mount_id(descriptor) == expected_mount_id)
        )
    except (OSError, RuntimeError) as exc:
        os.close(descriptor)
        raise error("contained directory identity is unavailable") from exc
    if not valid:
        os.close(descriptor)
        raise error("contained directory identity is invalid")
    return descriptor


def _open_prepared_operation(
    replicas_root: Path,
    operation_id: str,
    *,
    root_uid: int,
    root_gid: int,
    executor_name: str,
    executor_uid: int,
    executor_gid: int,
    error: Callable[[str], RuntimeError],
    mount_id: Callable[[int], int],
    confirmed_owner: Callable[[os.stat_result, int], bool],
    replicas_root_mode: int = 0o711,
    executor_tree_mode: int | None = 0o700,
) -> list[int]:
    """Open one operation that satisfies the shared prepared invariant."""
    retained: list[int] = []
    try:
        roots, device, root_mount_id = _walk_to_fixed_root(
            replicas_root,
            final_uid=root_uid,
            final_mode=replicas_root_mode,
            ancestor_uids=frozenset({ROOT_UID, root_uid}),
            error=error,
            mount_id=mount_id,
        )
        retained.extend(roots)
        if os.fstat(roots[-1]).st_gid != root_gid:
            raise error("prepared replicas root group is invalid")
        operation = _open_contained_child(
            roots[-1],
            operation_id,
            expected_dev=device,
            final_uids=frozenset({root_uid, executor_uid}),
            final_mode=0o700,
            error=error,
            expected_mount_id=root_mount_id,
            mount_id=mount_id,
        )
        retained.append(operation)
        operation_metadata = os.fstat(operation)
        if (
            not confirmed_owner(operation_metadata, root_uid)
            or operation_metadata.st_gid != root_gid
        ):
            raise error("prepared operation ownership is invalid")
        if set(os.listdir(operation)) != {"repo", "home", _EXECUTOR_IDENTITY_MARKER}:
            raise error("prepared operation entries are invalid")
        marker = os.open(
            _EXECUTOR_IDENTITY_MARKER,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=operation,
        )
        try:
            marker_stat = os.fstat(marker)
            marker_payload = os.read(marker, 65)
        finally:
            os.close(marker)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or marker_stat.st_uid != root_uid
            or marker_stat.st_gid != root_gid
            or marker_stat.st_nlink != 1
            or _mode_bits(marker_stat) != 0o600
            or marker_payload != executor_name.encode("ascii")
        ):
            raise error("prepared executor identity marker is invalid")
        for name in ("repo", "home"):
            tree = _open_contained_child(
                operation,
                name,
                expected_dev=device,
                final_uids=frozenset({root_uid, executor_uid}),
                final_mode=executor_tree_mode,
                error=error,
                expected_mount_id=root_mount_id,
                mount_id=mount_id,
            )
            retained.append(tree)
            tree_metadata = os.fstat(tree)
            if (
                not confirmed_owner(tree_metadata, executor_uid)
                or tree_metadata.st_gid != executor_gid
            ):
                raise error("prepared tree ownership is invalid")
        return retained
    except BaseException:
        _close_descriptors(retained)
        raise


def _validated_executor_pool(
    *,
    account_ids: Callable[[str], tuple[int, int]],
    account_groups: Callable[[str, int], frozenset[int]],
    group_ids: Callable[[str], int],
    broker_uid: int,
    broker_gid: int | None,
    root_uid: int,
    root_gid: int,
    validate_all_identities: bool = True,
) -> tuple[int, tuple[ExecutorIdentity, ...]]:
    """Resolve every protected account and reject aliases or group crossover."""
    names = (BROKER_NAME, APP_NAME, *EXECUTOR_NAMES)
    try:
        accounts = {name: account_ids(name) for name in names}
    except (KeyError, OSError) as exc:
        raise LaunchObjectError("launcher account is missing") from exc
    broker = accounts[BROKER_NAME]
    if broker[0] != broker_uid or (broker_gid is not None and broker[1] != broker_gid):
        raise LaunchObjectError("broker account identity is invalid")
    protected_uids = [root_uid, *(identity[0] for identity in accounts.values())]
    protected_gids = [root_gid, *(identity[1] for identity in accounts.values())]
    if any(type(value) is not int or value < 0 for value in (*protected_uids, *protected_gids)):
        raise LaunchObjectError("launcher account identity is invalid")
    if validate_all_identities:
        if any(value == 0 for value in protected_uids[1:] + protected_gids[1:]):
            raise LaunchObjectError("launcher protected identities must be positive")
        if len(set(protected_uids)) != len(protected_uids):
            raise LaunchObjectError("launcher protected UIDs must differ")
        if len(set(protected_gids)) != len(protected_gids):
            raise LaunchObjectError("launcher protected groups must differ")
        protected_group_set = frozenset(protected_gids)
        try:
            for name, (_uid, primary_gid) in accounts.items():
                if group_ids(name) != primary_gid:
                    raise LaunchObjectError("launcher named group identity is invalid")
                groups = account_groups(name, primary_gid)
                if groups & (protected_group_set - {primary_gid}):
                    raise LaunchObjectError("launcher supplementary groups cross trust boundaries")
        except (KeyError, OSError) as exc:
            raise LaunchObjectError("launcher group identities are unavailable") from exc
    identities = tuple(
        ExecutorIdentity(name=name, uid=accounts[name][0], gid=accounts[name][1])
        for name in EXECUTOR_NAMES
    )
    return broker[1], identities


def _validated_account_ids(
    *,
    account_ids: Callable[[str], tuple[int, int]],
    broker_uid: int,
    executor_name: str,
    executor_uid: int,
    broker_gid: int | None,
    executor_gid: int | None,
    root_uid: int,
    root_gid: int,
    account_groups: Callable[[str, int], frozenset[int]],
    group_ids: Callable[[str], int],
    validate_all_identities: bool,
) -> tuple[int, int]:
    broker_primary_gid, identities = _validated_executor_pool(
        account_ids=account_ids,
        account_groups=account_groups,
        group_ids=group_ids,
        broker_uid=broker_uid,
        broker_gid=broker_gid,
        root_uid=root_uid,
        root_gid=root_gid,
        validate_all_identities=validate_all_identities,
    )
    selected = next((item for item in identities if item.name == executor_name), None)
    if (
        selected is None
        or selected.uid != executor_uid
        or (executor_gid is not None and selected.gid != executor_gid)
    ):
        raise LaunchObjectError("executor account identity is invalid")
    if selected.uid in {root_uid, broker_uid}:
        raise LaunchObjectError("launcher identities must differ")
    return broker_primary_gid, selected.gid


def _cgroup_processes(cgroup: Path) -> list[int] | None:
    if not cgroup.is_absolute():
        raise ReclaimObjectError("reclaim cgroup path is invalid")
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for component in cgroup.parts[1:]:
            try:
                descriptors.append(
                    os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=descriptors[-1],
                    )
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ReclaimObjectError("reclaim cgroup is unreadable") from exc
        try:
            processes = os.open(
                "cgroup.events",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptors[-1],
            )
        except FileNotFoundError as exc:
            raise ReclaimObjectError("reclaim cgroup events are missing") from exc
        try:
            chunks: list[bytes] = []
            while chunk := os.read(processes, 4096):
                chunks.append(chunk)
        except OSError as exc:
            raise ReclaimObjectError("reclaim cgroup is unreadable") from exc
        finally:
            os.close(processes)
        fields: dict[bytes, bytes] = {}
        for line in b"".join(chunks).splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] in fields:
                raise ReclaimObjectError("reclaim cgroup events are invalid")
            fields[parts[0]] = parts[1]
        populated = fields.get(b"populated")
        if populated == b"0":
            return []
        if populated == b"1":
            return [1]
        raise ReclaimObjectError("reclaim cgroup events are invalid")
    finally:
        _close_descriptors(descriptors)


class LaunchObjectValidator:
    """Validate actual derived objects while retaining directory descriptors."""

    def __init__(
        self,
        *,
        layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
        root_uid: int = ROOT_UID,
        broker_uid: int = BROKER_UID,
        executor_name: str = EXECUTOR_NAMES[0],
        executor_uid: int | None = None,
        broker_gid: int | None = None,
        executor_gid: int | None = None,
        account_ids: Callable[[str], tuple[int, int]] = _account_ids,
        account_groups: Callable[[str, int], frozenset[int]] = _account_groups,
        group_ids: Callable[[str], int] = _group_id,
        validate_all_identities: bool = True,
        unit_exists: Callable[[str], bool] = _unit_exists,
        root_gid: int = 0,
        mount_id: Callable[[int], int] = _mount_id,
        confirmed_owner: Callable[[os.stat_result, int], bool] = lambda value, uid: (
            value.st_uid == uid
        ),
    ) -> None:
        self._layout = layout
        self._root_uid = root_uid
        self._broker_uid = broker_uid
        self._executor_name = executor_name
        self._executor_uid = executor_uid
        self._broker_gid = broker_gid
        self._executor_gid = executor_gid
        self._account_ids = account_ids
        self._account_groups = account_groups
        self._group_ids = group_ids
        self._validate_all_identities = validate_all_identities
        self._unit_exists = unit_exists
        self._root_gid = root_gid
        self._mount_id = mount_id
        self._confirmed_owner = confirmed_owner

    @property
    def executor_name(self) -> str:
        return self._executor_name

    def _validate_accounts(self) -> tuple[int, int]:
        expected_uid = (
            self._account_ids(self._executor_name)[0]
            if self._executor_uid is None
            else self._executor_uid
        )
        result = _validated_account_ids(
            account_ids=self._account_ids,
            broker_uid=self._broker_uid,
            executor_name=self._executor_name,
            executor_uid=expected_uid,
            broker_gid=self._broker_gid,
            executor_gid=self._executor_gid,
            root_uid=self._root_uid,
            root_gid=self._root_gid,
            account_groups=self._account_groups,
            group_ids=self._group_ids,
            validate_all_identities=self._validate_all_identities,
        )
        self._executor_uid = expected_uid
        self._executor_gid = result[1]
        return result

    def _open_directory(
        self,
        path: Path,
        *,
        final_uid: int,
        final_gid: int,
        final_mode: int,
        parent_uids: frozenset[int],
        require_other_search: bool = False,
    ) -> list[int]:
        if not path.is_absolute() or path == Path("/"):
            raise LaunchObjectError("launcher directory path is invalid")
        descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
        try:
            parts = path.parts[1:]
            for index, component in enumerate(parts):
                descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                is_final = index == len(parts) - 1
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LaunchObjectError("launcher directory object is invalid")
                if require_other_search and not _mode_bits(metadata) & 0o001:
                    raise LaunchObjectError("launcher executable ancestor is not searchable")
                if is_final:
                    if (
                        metadata.st_uid != final_uid
                        or metadata.st_gid != final_gid
                        or _mode_bits(metadata) != final_mode
                    ):
                        raise LaunchObjectError("launcher directory ownership or mode is invalid")
                else:
                    mode = _mode_bits(metadata)
                    sticky_root_directory = metadata.st_uid == ROOT_UID and bool(
                        mode & stat.S_ISVTX
                    )
                    if metadata.st_uid not in parent_uids | {ROOT_UID} or (
                        mode & 0o022 and not sticky_root_directory
                    ):
                        raise LaunchObjectError(
                            "launcher directory ancestor is writable or foreign"
                        )
            return descriptors
        except (OSError, LaunchObjectError) as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(exc, LaunchObjectError):
                raise
            raise LaunchObjectError("launcher directory validation failed") from exc

    def _validate_regular_executable(self, path: Path) -> list[int]:
        parent_descriptors = self._open_directory(
            path.parent,
            final_uid=self._root_uid,
            final_gid=self._root_gid,
            final_mode=0o755,
            parent_uids=frozenset({self._root_uid}),
            require_other_search=True,
        )
        descriptors = list(parent_descriptors)
        try:
            executable = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptors[-1],
            )
            descriptors.append(executable)
            metadata = os.fstat(executable)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._root_uid
                or metadata.st_gid != self._root_gid
                or _mode_bits(metadata) & 0o6022
                or not _mode_bits(metadata) & 0o001
                or metadata.st_nlink != 1
            ):
                raise LaunchObjectError("launcher executable identity or mode is invalid")
            return descriptors
        except (OSError, LaunchObjectError) as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(exc, LaunchObjectError):
                raise
            raise LaunchObjectError("launcher executable validation failed") from exc

    def _validate_socket(
        self,
        locations: LaunchLocations,
        *,
        broker_gid: int,
        executor_gid: int,
    ) -> list[int]:
        descriptors = self._open_directory(
            locations.runtime,
            final_uid=self._broker_uid,
            final_gid=broker_gid,
            final_mode=0o700,
            parent_uids=frozenset({self._root_uid, self._broker_uid}),
        )
        try:
            before = os.stat(
                locations.socket.name,
                dir_fd=descriptors[-1],
                follow_symlinks=False,
            )
            source_state = before.st_gid == broker_gid and _mode_bits(before) == 0o600
            partial_state = before.st_gid == executor_gid and _mode_bits(before) in {0o600, 0o660}
            if (
                not stat.S_ISSOCK(before.st_mode)
                or before.st_uid != self._broker_uid
                or not (source_state or partial_state)
            ):
                raise LaunchObjectError("launcher session socket identity or mode is invalid")
            return descriptors
        except (OSError, LaunchObjectError) as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(exc, LaunchObjectError):
                raise
            raise LaunchObjectError("launcher session socket validation failed") from exc

    def _pin_socket(
        self,
        locations: LaunchLocations,
        runtime_directory: int,
        *,
        broker_gid: int,
        executor_gid: int,
    ) -> list[int]:
        """Transition the accepted socket through a root-controlled hard link."""
        descriptors = self._open_directory(
            self._layout.socket_pins_root,
            final_uid=self._root_uid,
            final_gid=self._root_gid,
            final_mode=0o700,
            parent_uids=frozenset({self._root_uid}),
        )
        pin_root = descriptors[-1]
        temporary_name = f".{locations.socket_pin.name}.pending"
        final_name = locations.socket_pin.name
        temporary_exists = False
        try:
            source = os.stat(
                locations.socket.name,
                dir_fd=runtime_directory,
                follow_symlinks=False,
            )
            try:
                pinned = os.stat(final_name, dir_fd=pin_root, follow_symlinks=False)
            except FileNotFoundError:
                pinned = None
            if pinned is not None:
                if (
                    not stat.S_ISSOCK(source.st_mode)
                    or not stat.S_ISSOCK(pinned.st_mode)
                    or source.st_uid != self._broker_uid
                    or pinned.st_uid != self._broker_uid
                    or source.st_gid != executor_gid
                    or pinned.st_gid != executor_gid
                    or _mode_bits(source) != 0o660
                    or _mode_bits(pinned) != 0o660
                    or source.st_nlink != 2
                    or pinned.st_nlink != 2
                    or (source.st_dev, source.st_ino) != (pinned.st_dev, pinned.st_ino)
                ):
                    raise LaunchObjectError("launcher session socket pin is invalid")
                return descriptors
            try:
                temporary = os.stat(
                    temporary_name,
                    dir_fd=pin_root,
                    follow_symlinks=False,
                )
                temporary_exists = True
            except FileNotFoundError:
                if source.st_nlink != 1:
                    raise LaunchObjectError("launcher session socket link count is invalid")
                os.link(
                    locations.socket.name,
                    temporary_name,
                    src_dir_fd=runtime_directory,
                    dst_dir_fd=pin_root,
                    follow_symlinks=False,
                )
                temporary_exists = True
                os.fsync(pin_root)
                temporary = os.stat(
                    temporary_name,
                    dir_fd=pin_root,
                    follow_symlinks=False,
                )
            source = os.stat(
                locations.socket.name,
                dir_fd=runtime_directory,
                follow_symlinks=False,
            )
            initial_state = temporary.st_gid == broker_gid and _mode_bits(temporary) == 0o600
            partial_state = temporary.st_gid == executor_gid and _mode_bits(temporary) in {
                0o600,
                0o660,
            }
            if (
                not stat.S_ISSOCK(source.st_mode)
                or not stat.S_ISSOCK(temporary.st_mode)
                or source.st_uid != self._broker_uid
                or temporary.st_uid != self._broker_uid
                or source.st_nlink != 2
                or temporary.st_nlink != 2
                or (source.st_dev, source.st_ino) != (temporary.st_dev, temporary.st_ino)
                or not (initial_state or partial_state)
            ):
                raise LaunchObjectError("launcher temporary socket pin is invalid")
            if initial_state:
                os.chown(
                    temporary_name,
                    self._broker_uid,
                    executor_gid,
                    dir_fd=pin_root,
                    follow_symlinks=False,
                )
            os.chmod(
                temporary_name,
                0o660,
                dir_fd=pin_root,
                follow_symlinks=False,
            )
            source = os.stat(
                locations.socket.name,
                dir_fd=runtime_directory,
                follow_symlinks=False,
            )
            transitioned = os.stat(
                temporary_name,
                dir_fd=pin_root,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISSOCK(source.st_mode)
                or not stat.S_ISSOCK(transitioned.st_mode)
                or source.st_uid != self._broker_uid
                or transitioned.st_uid != self._broker_uid
                or source.st_gid != executor_gid
                or transitioned.st_gid != executor_gid
                or _mode_bits(source) != 0o660
                or _mode_bits(transitioned) != 0o660
                or source.st_nlink != 2
                or transitioned.st_nlink != 2
                or (source.st_dev, source.st_ino) != (transitioned.st_dev, transitioned.st_ino)
            ):
                raise LaunchObjectError("launcher session socket transition failed")
            os.rename(temporary_name, final_name, src_dir_fd=pin_root, dst_dir_fd=pin_root)
            temporary_exists = False
            os.fsync(pin_root)
            return descriptors
        except (OSError, LaunchObjectError) as exc:
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=pin_root)
                    os.fsync(pin_root)
                except OSError as cleanup_exc:
                    _close_descriptors(descriptors)
                    raise LaunchObjectError(
                        "launcher temporary socket pin cleanup failed"
                    ) from cleanup_exc
            _close_descriptors(descriptors)
            if isinstance(exc, LaunchObjectError):
                raise
            raise LaunchObjectError("launcher session socket pin failed") from exc

    @contextmanager
    def validate(self, operation_id: str) -> Iterator[LaunchLocations]:
        """Retain validated object descriptors until systemd accepts the command."""
        broker_gid, executor_gid = self._validate_accounts()
        if self._executor_uid is None:
            raise LaunchObjectError("executor account identity is invalid")
        locations = derive_launch_locations(operation_id, layout=self._layout)
        if self._unit_exists(locations.unit):
            raise LaunchObjectError("launcher unit already exists")
        descriptors: list[int] = []
        try:
            descriptors.extend(
                self._open_directory(
                    self._layout.runtime_root,
                    final_uid=self._broker_uid,
                    final_gid=broker_gid,
                    final_mode=0o711,
                    parent_uids=frozenset({self._root_uid, self._broker_uid}),
                )
            )
            descriptors.extend(
                _open_prepared_operation(
                    self._layout.replicas_root,
                    operation_id,
                    root_uid=self._root_uid,
                    root_gid=self._root_gid,
                    executor_name=self._executor_name,
                    executor_uid=self._executor_uid,
                    executor_gid=executor_gid,
                    error=LaunchObjectError,
                    mount_id=self._mount_id,
                    confirmed_owner=self._confirmed_owner,
                )
            )
            descriptors.extend(self._validate_regular_executable(locations.systemd_run))
            descriptors.extend(self._validate_regular_executable(locations.executable))
            socket_descriptors = self._validate_socket(
                locations,
                broker_gid=broker_gid,
                executor_gid=executor_gid,
            )
            descriptors.extend(socket_descriptors)
            descriptors.extend(
                self._pin_socket(
                    locations,
                    socket_descriptors[-1],
                    broker_gid=broker_gid,
                    executor_gid=executor_gid,
                )
            )
            yield locations
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


class ReplicaLifecycleValidator:
    """Validate fixed lifecycle state and perform gated ownership effects."""

    def __init__(
        self,
        *,
        layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
        root_uid: int = ROOT_UID,
        broker_uid: int = BROKER_UID,
        executor_name: str = EXECUTOR_NAMES[0],
        executor_uid: int | None = None,
        broker_gid: int | None = None,
        executor_gid: int | None = None,
        account_ids: Callable[[str], tuple[int, int]] = _account_ids,
        account_groups: Callable[[str, int], frozenset[int]] = _account_groups,
        group_ids: Callable[[str], int] = _group_id,
        validate_all_identities: bool = True,
        unit_exists: Callable[[str], bool] = _unit_exists,
        cgroup_processes: Callable[[Path], list[int] | None] = _cgroup_processes,
        root_gid: int = 0,
        mount_id: Callable[[int], int] = _mount_id,
        transfer_fd_ownership: Callable[[int, int, int], None] = os.fchown,
        transfer_entry_ownership: Callable[..., None] = os.chown,
        confirmed_owner: Callable[[os.stat_result, int], bool] = lambda value, uid: (
            value.st_uid == uid
        ),
        promote_operation: Callable[..., None] = os.rename,
    ) -> None:
        self._layout = layout
        self._root_uid = root_uid
        self._broker_uid = broker_uid
        self._executor_name = executor_name
        self._executor_uid = executor_uid
        self._broker_gid = broker_gid
        self._executor_gid = executor_gid
        self._account_ids = account_ids
        self._account_groups = account_groups
        self._group_ids = group_ids
        self._validate_all_identities = validate_all_identities
        self._unit_exists = unit_exists
        self._cgroup_processes = cgroup_processes
        self._root_gid = root_gid
        self._mount_id = mount_id
        self._promote_operation = promote_operation
        self._transfer_fd_ownership = transfer_fd_ownership
        self._transfer_entry_ownership = transfer_entry_ownership
        self._confirmed_owner = confirmed_owner

    @contextmanager
    def prepared(self, operation_id: str) -> Iterator[LaunchLocations]:
        try:
            expected_uid = (
                self._account_ids(self._executor_name)[0]
                if self._executor_uid is None
                else self._executor_uid
            )
            broker_gid, executor_gid = _validated_account_ids(
                account_ids=self._account_ids,
                broker_uid=self._broker_uid,
                executor_name=self._executor_name,
                executor_uid=expected_uid,
                broker_gid=self._broker_gid,
                executor_gid=self._executor_gid,
                root_uid=self._root_uid,
                root_gid=self._root_gid,
                account_groups=self._account_groups,
                group_ids=self._group_ids,
                validate_all_identities=self._validate_all_identities,
            )
            self._executor_uid = expected_uid
            self._executor_gid = executor_gid
        except (KeyError, OSError, LaunchObjectError) as exc:
            raise PrepareObjectError("replica prepare identity is invalid") from exc
        locations = derive_launch_locations(operation_id, layout=self._layout)
        try:
            self._validate_reclaim_quiescence(
                derive_reclaim_locations(operation_id, layout=self._layout)
            )
            self._require_socket_pin_absent(operation_id)
        except ReclaimObjectError as exc:
            raise PrepareObjectError(str(exc).replace("reclaim", "replica prepare")) from exc
        retained: list[int] = []
        if self._executor_uid is None:
            raise PrepareObjectError("replica prepare identity is invalid")
        allowed = frozenset({self._root_uid, self._broker_uid, self._executor_uid})
        try:
            staging_roots, staging_device, staging_mount_id = _walk_to_fixed_root(
                self._layout.staging_root,
                final_uid=self._broker_uid,
                final_mode=0o700,
                ancestor_uids=frozenset({ROOT_UID, self._root_uid, self._broker_uid}),
                error=PrepareObjectError,
                mount_id=self._mount_id,
            )
            retained.extend(staging_roots)
            if os.fstat(staging_roots[-1]).st_gid != broker_gid:
                raise PrepareObjectError("replica staging root group is invalid")
            prepared_roots, device, root_mount_id = _walk_to_fixed_root(
                self._layout.replicas_root,
                final_uid=self._root_uid,
                final_mode=0o711,
                ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
                error=PrepareObjectError,
                mount_id=self._mount_id,
            )
            retained.extend(prepared_roots)
            if os.fstat(prepared_roots[-1]).st_gid != self._root_gid:
                raise PrepareObjectError("prepared replicas root group is invalid")
            if staging_device != device or staging_mount_id != root_mount_id:
                raise PrepareObjectError("replica promotion filesystem identity changed")
            source_exists = self._entry_exists(staging_roots[-1], operation_id)
            destination_exists = self._entry_exists(prepared_roots[-1], operation_id)
            if source_exists and destination_exists:
                raise PrepareObjectError("replica promotion state is ambiguous")
            if not source_exists and not destination_exists:
                raise PrepareObjectError("replica operation is missing")
            parent = prepared_roots[-1]
            if source_exists:
                operation = _open_contained_child(
                    staging_roots[-1],
                    operation_id,
                    expected_dev=device,
                    final_uids=frozenset({self._broker_uid, self._root_uid}),
                    final_mode=0o700,
                    error=PrepareObjectError,
                    expected_mount_id=root_mount_id,
                    mount_id=self._mount_id,
                )
                retained.append(operation)
                if set(os.listdir(operation)) != {"repo", "home"}:
                    raise PrepareObjectError("replica operation entries are invalid")
                self._preflight_operation(
                    operation,
                    device,
                    root_mount_id,
                    allowed,
                )
                self._promote_operation(
                    operation_id,
                    operation_id,
                    src_dir_fd=staging_roots[-1],
                    dst_dir_fd=prepared_roots[-1],
                )
                os.fsync(prepared_roots[-1])
                os.fsync(staging_roots[-1])
            else:
                operation = _open_contained_child(
                    prepared_roots[-1],
                    operation_id,
                    expected_dev=device,
                    final_uids=frozenset({self._broker_uid, self._root_uid}),
                    final_mode=0o700,
                    error=PrepareObjectError,
                    expected_mount_id=root_mount_id,
                    mount_id=self._mount_id,
                )
                retained.append(operation)
                self._preflight_operation(
                    operation,
                    device,
                    root_mount_id,
                    allowed,
                )
            os.fsync(staging_roots[-1])
            os.fsync(prepared_roots[-1])
            self._freeze_directory(operation)
            self._verify_named_directory(parent, operation_id, operation, self._root_uid)
            operation_entries = set(os.listdir(operation))
            if not {"repo", "home"} <= operation_entries or not operation_entries <= {
                "repo",
                "home",
                _EXECUTOR_IDENTITY_MARKER,
                _EXECUTOR_IDENTITY_PENDING,
            }:
                raise PrepareObjectError("replica operation entries are invalid")
            trees: list[int] = []
            for name in ("repo", "home"):
                tree = _open_contained_child(
                    operation,
                    name,
                    expected_dev=device,
                    final_uids=allowed,
                    final_mode=0o700,
                    error=PrepareObjectError,
                    expected_mount_id=root_mount_id,
                    mount_id=self._mount_id,
                )
                retained.append(tree)
                trees.append(tree)
            # The root-owned marker is durable allocation authority before transfer.
            try:
                self._persist_executor_identity(operation)
            except OSError as exc:
                raise PrepareObjectError("executor identity marker publication failed") from exc
            for name, tree in zip(("repo", "home"), trees, strict=True):
                self._transfer_fd_ownership(tree, self._executor_uid, executor_gid)
                os.fchmod(tree, 0o700)
                os.fsync(tree)
                self._verify_top_entry(operation, name, tree)
            os.fsync(operation)
            for name, tree in zip(("repo", "home"), trees, strict=True):
                self._freeze_tree(
                    tree,
                    device,
                    root_mount_id,
                    allowed,
                    freeze_self=False,
                )
                self._transfer_tree(tree, device, root_mount_id, allowed, executor_gid)
                os.fchmod(tree, 0o700)
                os.fsync(tree)
                self._verify_top_entry(operation, name, tree)
            os.fsync(operation)
            verified = _open_prepared_operation(
                self._layout.replicas_root,
                operation_id,
                root_uid=self._root_uid,
                root_gid=self._root_gid,
                executor_name=self._executor_name,
                executor_uid=self._executor_uid,
                executor_gid=executor_gid,
                error=PrepareObjectError,
                mount_id=self._mount_id,
                confirmed_owner=self._confirmed_owner,
            )
            _close_descriptors(verified)
            yield locations
        except (FileNotFoundError, NotADirectoryError, RecursionError) as exc:
            raise PrepareObjectError("replica tree validation failed") from exc
        except OSError as exc:
            raise PrepareObjectError("replica ownership transfer failed") from exc
        finally:
            _close_descriptors(retained)

    def _validate_reclaim_operation(
        self,
        root: Path,
        operation_id: str,
        *,
        root_mode: int,
        allow_partial: bool,
    ) -> list[int]:
        if self._executor_uid is None or self._executor_gid is None:
            raise ReclaimObjectError("reclaim identity is invalid")
        descriptors, device, root_mount_id = _walk_to_fixed_root(
            root,
            final_uid=self._root_uid,
            final_mode=root_mode,
            ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
            error=ReclaimObjectError,
            mount_id=self._mount_id,
        )
        try:
            operation = _open_contained_child(
                descriptors[-1],
                operation_id,
                expected_dev=device,
                final_uids=frozenset({self._root_uid}),
                final_mode=0o700,
                error=ReclaimObjectError,
                expected_mount_id=root_mount_id,
                mount_id=self._mount_id,
            )
            descriptors.append(operation)
            names = set(os.listdir(operation))
            if not names <= {"repo", "home", _EXECUTOR_IDENTITY_MARKER}:
                raise ReclaimObjectError("reclaim operation contains an unknown entry")
            if _EXECUTOR_IDENTITY_MARKER not in names:
                raise ReclaimObjectError("executor identity marker is missing")
            marker = os.open(
                _EXECUTOR_IDENTITY_MARKER,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=operation,
            )
            try:
                marker_stat = os.fstat(marker)
                marker_payload = os.read(marker, 65)
            finally:
                os.close(marker)
            if (
                not stat.S_ISREG(marker_stat.st_mode)
                or marker_stat.st_uid != self._root_uid
                or marker_stat.st_gid != self._root_gid
                or marker_stat.st_nlink != 1
                or _mode_bits(marker_stat) != 0o600
                or marker_payload != self._executor_name.encode("ascii")
            ):
                raise ReclaimObjectError("executor identity marker is invalid")
            for name in ("repo", "home"):
                if name not in names:
                    if allow_partial:
                        continue
                    raise ReclaimObjectError("executor allocation marker is incomplete")
                tree = _open_contained_child(
                    operation,
                    name,
                    expected_dev=device,
                    final_uids=frozenset({self._root_uid, self._executor_uid}),
                    final_mode=None,
                    error=ReclaimObjectError,
                    expected_mount_id=root_mount_id,
                    mount_id=self._mount_id,
                )
                descriptors.append(tree)
                metadata = os.fstat(tree)
                if (
                    not self._confirmed_owner(metadata, self._executor_uid)
                    or metadata.st_gid != self._executor_gid
                ):
                    raise ReclaimObjectError("executor allocation marker is incomplete")
            return descriptors
        except BaseException:
            _close_descriptors(descriptors)
            raise

    @contextmanager
    def reclaimed(self, operation_id: str) -> Iterator[ReclaimLocations]:
        locations = derive_reclaim_locations(operation_id, layout=self._layout)
        self._validate_reclaim_quiescence(locations)
        self._validate_socket_pin(operation_id)
        retained: list[int] = []
        try:
            prepared_roots, device, root_mount_id = _walk_to_fixed_root(
                self._layout.replicas_root,
                final_uid=self._root_uid,
                final_mode=0o711,
                ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
                error=ReclaimObjectError,
                mount_id=self._mount_id,
            )
            retained.extend(prepared_roots)
            quarantine_roots, quarantine_device, quarantine_mount_id = _walk_to_fixed_root(
                self._layout.quarantine_root,
                final_uid=self._root_uid,
                final_mode=0o700,
                ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
                error=ReclaimObjectError,
                mount_id=self._mount_id,
            )
            retained.extend(quarantine_roots)
            if quarantine_device != device or quarantine_mount_id != root_mount_id:
                raise ReclaimObjectError("reclaim quarantine crossed a filesystem boundary")
            source_exists = self._entry_exists(prepared_roots[-1], operation_id)
            target_exists = self._entry_exists(quarantine_roots[-1], operation_id)
            if source_exists and target_exists:
                raise ReclaimObjectError("reclaim state is ambiguous")
            if not source_exists and not target_exists:
                os.fsync(quarantine_roots[-1])
                self._remove_socket_pin(operation_id)
                yield locations
                return
            if self._executor_uid is None or self._executor_gid is None:
                raise ReclaimObjectError("reclaim identity is invalid")
            validated = self._validate_reclaim_operation(
                self._layout.replicas_root if source_exists else self._layout.quarantine_root,
                operation_id,
                root_mode=0o711 if source_exists else 0o700,
                allow_partial=not source_exists,
            )
            _close_descriptors(validated)
            self._remove_socket_pin(operation_id)
            parent = quarantine_roots[-1]
            if source_exists:
                target = os.open(
                    operation_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=prepared_roots[-1],
                )
                retained.append(target)
                os.rename(
                    operation_id,
                    operation_id,
                    src_dir_fd=prepared_roots[-1],
                    dst_dir_fd=quarantine_roots[-1],
                )
                os.fsync(prepared_roots[-1])
                os.fsync(quarantine_roots[-1])
            else:
                target = os.open(
                    operation_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=quarantine_roots[-1],
                )
                retained.append(target)
            os.fsync(prepared_roots[-1])
            os.fsync(quarantine_roots[-1])
            if self._checked_mount_id(target, ReclaimObjectError) != root_mount_id:
                raise ReclaimObjectError("reclaim crossed a mount boundary")
            metadata = os.fstat(target)
            if (
                metadata.st_uid != self._root_uid
                or _mode_bits(metadata) != 0o700
                or metadata.st_dev != device
            ):
                raise ReclaimObjectError("reclaim target identity is invalid")
            self._remove_tree(target, device, root_mount_id)
            os.fsync(parent)
            yield locations
        except (FileNotFoundError, NotADirectoryError, RecursionError) as exc:
            raise ReclaimObjectError("reclaim tree validation failed") from exc
        except OSError as exc:
            raise ReclaimObjectError("reclaim target removal failed") from exc
        finally:
            _close_descriptors(retained)

    def _require_socket_pin_absent(self, operation_id: str) -> None:
        descriptors, _, _ = _walk_to_fixed_root(
            self._layout.socket_pins_root,
            final_uid=self._root_uid,
            final_mode=0o700,
            ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
            error=ReclaimObjectError,
            mount_id=self._mount_id,
        )
        try:
            if any(
                self._entry_exists(descriptors[-1], name)
                for name in (operation_id, f".{operation_id}.pending")
            ):
                raise ReclaimObjectError("reclaim socket pin remains")
        finally:
            _close_descriptors(descriptors)

    def _validate_socket_pin(self, operation_id: str) -> bool:
        if self._executor_uid is None:
            raise ReclaimObjectError("reclaim identity is invalid")
        broker_gid, executor_gid = _validated_account_ids(
            account_ids=self._account_ids,
            broker_uid=self._broker_uid,
            executor_name=self._executor_name,
            executor_uid=self._executor_uid,
            broker_gid=self._broker_gid,
            executor_gid=self._executor_gid,
            root_uid=self._root_uid,
            root_gid=self._root_gid,
            account_groups=self._account_groups,
            group_ids=self._group_ids,
            validate_all_identities=self._validate_all_identities,
        )
        descriptors, device, _ = _walk_to_fixed_root(
            self._layout.socket_pins_root,
            final_uid=self._root_uid,
            final_mode=0o700,
            ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
            error=ReclaimObjectError,
            mount_id=self._mount_id,
        )
        try:
            pins: list[tuple[str, os.stat_result]] = []
            for name in (operation_id, f".{operation_id}.pending"):
                try:
                    pins.append(
                        (
                            name,
                            os.stat(name, dir_fd=descriptors[-1], follow_symlinks=False),
                        )
                    )
                except FileNotFoundError:
                    continue
            if not pins:
                return False
            if len(pins) != 1:
                raise ReclaimObjectError("reclaim socket pin state is ambiguous")
            name, metadata = pins[0]
            final_state = (
                name == operation_id
                and metadata.st_gid == executor_gid
                and _mode_bits(metadata) == 0o660
            )
            pending_state = name != operation_id and (
                (metadata.st_gid == broker_gid and _mode_bits(metadata) == 0o600)
                or (metadata.st_gid == executor_gid and _mode_bits(metadata) in {0o600, 0o660})
            )
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_dev != device
                or metadata.st_uid != self._broker_uid
                or not (final_state or pending_state)
            ):
                raise ReclaimObjectError("reclaim socket pin is invalid")
            return True
        finally:
            _close_descriptors(descriptors)

    def _remove_socket_pin(self, operation_id: str) -> None:
        if self._executor_uid is None:
            raise ReclaimObjectError("reclaim identity is invalid")
        self._validate_socket_pin(operation_id)
        descriptors, _device, _ = _walk_to_fixed_root(
            self._layout.socket_pins_root,
            final_uid=self._root_uid,
            final_mode=0o700,
            ancestor_uids=frozenset({ROOT_UID, self._root_uid}),
            error=ReclaimObjectError,
            mount_id=self._mount_id,
        )
        parent = descriptors[-1]
        try:
            existing = [
                name
                for name in (operation_id, f".{operation_id}.pending")
                if self._entry_exists(parent, name)
            ]
            if not existing:
                os.fsync(parent)
                return
            if len(existing) != 1:
                raise ReclaimObjectError("reclaim socket pin state is ambiguous")
            os.unlink(existing[0], dir_fd=parent)
            os.fsync(parent)
        finally:
            _close_descriptors(descriptors)

    def _checked_mount_id(
        self,
        descriptor: int,
        error: type[PrepareObjectError | ReclaimObjectError],
    ) -> int:
        try:
            return self._mount_id(descriptor)
        except (OSError, RuntimeError) as exc:
            raise error("replica mount identity is unavailable") from exc

    @staticmethod
    def _entry_exists(parent: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _validate_reclaim_quiescence(self, locations: ReclaimLocations) -> None:
        try:
            expected_uid = (
                self._account_ids(self._executor_name)[0]
                if self._executor_uid is None
                else self._executor_uid
            )
            _broker_gid, executor_gid = _validated_account_ids(
                account_ids=self._account_ids,
                broker_uid=self._broker_uid,
                executor_name=self._executor_name,
                executor_uid=expected_uid,
                broker_gid=self._broker_gid,
                executor_gid=self._executor_gid,
                root_uid=self._root_uid,
                root_gid=self._root_gid,
                account_groups=self._account_groups,
                group_ids=self._group_ids,
                validate_all_identities=self._validate_all_identities,
            )
            self._executor_uid = expected_uid
            self._executor_gid = executor_gid
        except (KeyError, OSError, LaunchObjectError) as exc:
            raise ReclaimObjectError("reclaim identity is invalid") from exc
        try:
            unit_exists = self._unit_exists(locations.unit)
        except LaunchObjectError as exc:
            raise ReclaimObjectError("reclaim unit state is unavailable") from exc
        if unit_exists:
            raise ReclaimObjectError("reclaim unit still exists")
        if self._cgroup_processes(locations.cgroup):
            raise ReclaimObjectError("reclaim cgroup is still populated")
        try:
            roots, _, _ = _walk_to_fixed_root(
                self._layout.runtime_root,
                final_uid=self._broker_uid,
                final_mode=0o711,
                ancestor_uids=frozenset({ROOT_UID, self._root_uid, self._broker_uid}),
                error=ReclaimObjectError,
                mount_id=self._mount_id,
            )
        except FileNotFoundError:
            return
        try:
            try:
                os.stat(locations.runtime.name, dir_fd=roots[-1], follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ReclaimObjectError("reclaim runtime is not stopped")
        finally:
            _close_descriptors(roots)

    def _preflight_operation(
        self,
        operation: int,
        expected_device: int,
        expected_mount_id: int,
        allowed_uids: frozenset[int],
    ) -> None:
        names = set(os.listdir(operation))
        if not {"repo", "home"} <= names or not names <= {
            "repo",
            "home",
            _EXECUTOR_IDENTITY_MARKER,
            _EXECUTOR_IDENTITY_PENDING,
        }:
            raise PrepareObjectError("replica operation entries are invalid")
        for name in ("repo", "home"):
            tree = _open_contained_child(
                operation,
                name,
                expected_dev=expected_device,
                final_uids=allowed_uids,
                final_mode=0o700,
                error=PrepareObjectError,
                expected_mount_id=expected_mount_id,
                mount_id=self._mount_id,
            )
            try:
                self._preflight_tree(
                    tree,
                    expected_device,
                    expected_mount_id,
                    allowed_uids,
                )
            finally:
                os.close(tree)

    def _preflight_tree(
        self,
        directory: int,
        expected_device: int,
        expected_mount_id: int,
        allowed_uids: frozenset[int],
    ) -> None:
        for name in sorted(os.listdir(directory)):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if metadata.st_dev != expected_device or metadata.st_uid not in allowed_uids:
                raise PrepareObjectError("replica entry identity is invalid")
            if stat.S_ISDIR(metadata.st_mode):
                child = _open_contained_child(
                    directory,
                    name,
                    expected_dev=expected_device,
                    final_uids=allowed_uids,
                    final_mode=None,
                    error=PrepareObjectError,
                    expected_mount_id=expected_mount_id,
                    mount_id=self._mount_id,
                )
                try:
                    self._preflight_tree(
                        child,
                        expected_device,
                        expected_mount_id,
                        allowed_uids,
                    )
                finally:
                    os.close(child)
            elif stat.S_ISLNK(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise PrepareObjectError("replica symbolic link has multiple links")
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise PrepareObjectError("replica file has multiple links")
            else:
                raise PrepareObjectError("replica entry type is unsupported")

    def _freeze_directory(self, directory: int) -> None:
        self._transfer_fd_ownership(directory, self._root_uid, self._root_gid)
        os.fchmod(directory, 0o700)
        os.fsync(directory)

    def _freeze_tree(
        self,
        directory: int,
        expected_device: int,
        expected_mount_id: int,
        allowed_uids: frozenset[int],
        *,
        freeze_self: bool = True,
    ) -> None:
        if freeze_self:
            self._freeze_directory(directory)
        for name in sorted(os.listdir(directory)):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if metadata.st_dev != expected_device or metadata.st_uid not in allowed_uids:
                raise PrepareObjectError("replica entry identity is invalid")
            if stat.S_ISDIR(metadata.st_mode):
                child = _open_contained_child(
                    directory,
                    name,
                    expected_dev=expected_device,
                    final_uids=allowed_uids,
                    final_mode=None,
                    error=PrepareObjectError,
                    expected_mount_id=expected_mount_id,
                    mount_id=self._mount_id,
                )
                try:
                    self._freeze_tree(child, expected_device, expected_mount_id, allowed_uids)
                finally:
                    os.close(child)
        os.fsync(directory)

    def _verify_named_directory(
        self, parent: int, name: str, directory: int, expected_uid: int
    ) -> None:
        pinned = os.fstat(directory)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_ino != pinned.st_ino
            or not self._confirmed_owner(current, expected_uid)
        ):
            raise PrepareObjectError("replica directory was replaced")

    def _transfer_tree(
        self,
        directory: int,
        expected_device: int,
        expected_mount_id: int,
        allowed_uids: frozenset[int],
        executor_gid: int,
    ) -> None:
        executor_uid = self._executor_uid
        if executor_uid is None:
            raise PrepareObjectError("replica prepare identity is invalid")
        for name in sorted(os.listdir(directory)):
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if before.st_dev != expected_device or before.st_uid not in allowed_uids:
                raise PrepareObjectError("replica entry identity is invalid")
            if stat.S_ISLNK(before.st_mode):
                if before.st_nlink != 1:
                    raise PrepareObjectError("replica symbolic link has multiple links")
                self._transfer_entry_ownership(
                    name,
                    executor_uid,
                    executor_gid,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise PrepareObjectError("replica file has multiple links")
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    current = os.fstat(descriptor)
                    if self._checked_mount_id(descriptor, PrepareObjectError) != expected_mount_id:
                        raise PrepareObjectError("replica file crosses a mount boundary")
                    if current.st_ino != before.st_ino:
                        raise PrepareObjectError("replica file was replaced")
                    self._transfer_fd_ownership(descriptor, executor_uid, executor_gid)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            elif stat.S_ISDIR(before.st_mode):
                descriptor = _open_contained_child(
                    directory,
                    name,
                    expected_dev=expected_device,
                    final_uids=allowed_uids,
                    final_mode=None,
                    error=PrepareObjectError,
                    expected_mount_id=expected_mount_id,
                    mount_id=self._mount_id,
                )
                try:
                    if os.fstat(descriptor).st_ino != before.st_ino:
                        raise PrepareObjectError("replica directory was replaced")
                    self._transfer_tree(
                        descriptor,
                        expected_device,
                        expected_mount_id,
                        allowed_uids,
                        executor_gid,
                    )
                    self._transfer_fd_ownership(descriptor, executor_uid, executor_gid)
                    os.fchmod(descriptor, 0o700)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            else:
                raise PrepareObjectError("replica tree contains a special file")
            after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if after.st_ino != before.st_ino or not self._confirmed_owner(after, executor_uid):
                raise PrepareObjectError("replica entry was replaced during prepare")

    def _persist_executor_identity(self, operation: int) -> None:
        """Publish the selected identity atomically below the root-owned operation."""
        payload = self._executor_name.encode("ascii")
        entries = set(os.listdir(operation))
        if {_EXECUTOR_IDENTITY_MARKER, _EXECUTOR_IDENTITY_PENDING} <= entries:
            raise PrepareObjectError("executor identity marker state is ambiguous")
        if _EXECUTOR_IDENTITY_MARKER in entries:
            marker = os.open(
                _EXECUTOR_IDENTITY_MARKER,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=operation,
            )
            try:
                metadata = os.fstat(marker)
                current = os.read(marker, len(payload) + 1)
            finally:
                os.close(marker)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._root_uid
                or metadata.st_gid != self._root_gid
                or metadata.st_nlink != 1
                or _mode_bits(metadata) != 0o600
                or metadata.st_size != len(payload)
                or current != payload
            ):
                raise PrepareObjectError("executor identity marker is invalid")
            return

        flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        marker = os.open(_EXECUTOR_IDENTITY_PENDING, flags, 0o600, dir_fd=operation)
        try:
            metadata = os.fstat(marker)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._root_uid
                or metadata.st_gid != self._root_gid
                or metadata.st_nlink != 1
                or _mode_bits(metadata) != 0o600
            ):
                raise PrepareObjectError("executor identity marker is invalid")
            os.ftruncate(marker, 0)
            offset = 0
            while offset < len(payload):
                written = os.write(marker, payload[offset:])
                if written <= 0:
                    raise PrepareObjectError("executor identity marker write is incomplete")
                offset += written
            os.fsync(marker)
        finally:
            os.close(marker)
        if _EXECUTOR_IDENTITY_MARKER in os.listdir(operation):
            raise PrepareObjectError("executor identity marker state is ambiguous")
        os.rename(
            _EXECUTOR_IDENTITY_PENDING,
            _EXECUTOR_IDENTITY_MARKER,
            src_dir_fd=operation,
            dst_dir_fd=operation,
        )
        os.fsync(operation)

    def _verify_top_entry(self, operation: int, name: str, tree: int) -> None:
        executor_uid = self._executor_uid
        if executor_uid is None:
            raise PrepareObjectError("replica prepare identity is invalid")
        pinned = os.fstat(tree)
        current = os.stat(name, dir_fd=operation, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_ino != pinned.st_ino
            or not self._confirmed_owner(current, executor_uid)
        ):
            raise PrepareObjectError("replica top tree was replaced during prepare")

    def _remove_tree(self, directory: int, expected_device: int, expected_mount_id: int) -> None:
        current = os.dup(directory)
        frames: list[tuple[str | None, list[str]]] = [
            (
                None,
                [
                    name
                    for name in sorted(os.listdir(current), reverse=True)
                    if name != _EXECUTOR_IDENTITY_MARKER
                ],
            )
        ]
        allowed_uids = frozenset({self._root_uid, self._executor_uid or self._root_uid})
        try:
            while frames:
                current_name, remaining = frames[-1]
                if remaining:
                    name = remaining.pop()
                    metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
                    if metadata.st_dev != expected_device:
                        raise ReclaimObjectError("reclaim crossed a filesystem boundary")
                    if stat.S_ISDIR(metadata.st_mode):
                        child = _open_contained_child(
                            current,
                            name,
                            expected_dev=expected_device,
                            final_uids=allowed_uids,
                            final_mode=None,
                            error=ReclaimObjectError,
                            expected_mount_id=expected_mount_id,
                            mount_id=self._mount_id,
                        )
                        child_names = sorted(os.listdir(child), reverse=True)
                        frames.append((name, child_names))
                        os.close(current)
                        current = child
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        entry = os.open(
                            name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=current,
                        )
                        try:
                            if (
                                self._checked_mount_id(entry, ReclaimObjectError)
                                != expected_mount_id
                            ):
                                raise ReclaimObjectError("reclaim crossed a mount boundary")
                            if _physical_identity(os.fstat(entry)) != _physical_identity(metadata):
                                raise ReclaimObjectError("reclaim entry was replaced")
                        finally:
                            os.close(entry)
                    current_entry = os.stat(name, dir_fd=current, follow_symlinks=False)
                    if _physical_identity(current_entry) != _physical_identity(metadata):
                        raise ReclaimObjectError("reclaim entry was replaced")
                    os.unlink(name, dir_fd=current)
                    continue

                os.fsync(current)
                if current_name is None:
                    break
                parent = _open_contained_child(
                    current,
                    "..",
                    expected_dev=expected_device,
                    final_uids=allowed_uids,
                    final_mode=None,
                    error=ReclaimObjectError,
                    expected_mount_id=expected_mount_id,
                    mount_id=self._mount_id,
                )
                child_identity = _physical_identity(os.fstat(current))
                named_child = os.stat(
                    current_name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                if _physical_identity(named_child) != child_identity:
                    os.close(parent)
                    raise ReclaimObjectError("reclaim directory was replaced")
                os.close(current)
                current = parent
                os.rmdir(current_name, dir_fd=current)
                os.fsync(current)
                frames.pop()
        finally:
            os.close(current)

        marker = os.stat(
            _EXECUTOR_IDENTITY_MARKER,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(marker.st_mode) or marker.st_uid != self._root_uid:
            raise ReclaimObjectError("executor identity marker is invalid")
        os.fsync(directory)


class RootLauncher:
    """Own executor allocation and expose one opt-in effect boundary."""

    def __init__(
        self,
        *,
        broker_uid: int = BROKER_UID,
        broker_gid: int | None = None,
        root_uid: int = ROOT_UID,
        root_gid: int = 0,
        execution_enabled: bool = LAUNCH_EXECUTION_ENABLED_DEFAULT,
        prepare_enabled: bool = REPLICA_PREPARE_ENABLED_DEFAULT,
        reclaim_enabled: bool = REPLICA_RECLAIM_ENABLED_DEFAULT,
        command_runner: Callable[[tuple[str, ...]], object] = _run_command,
        layout: LaunchLayout = DEFAULT_LAUNCH_LAYOUT,
        account_ids: Callable[[str], tuple[int, int]] = _account_ids,
        account_groups: Callable[[str, int], frozenset[int]] = _account_groups,
        group_ids: Callable[[str], int] = _group_id,
        object_validator: LaunchObjectValidator | None = None,
        replica_lifecycle: ReplicaLifecycleValidator | None = None,
    ) -> None:
        if type(broker_uid) is not int or broker_uid < 0:
            raise ValueError("broker UID must be a non-negative integer")
        if type(execution_enabled) is not bool:
            raise TypeError("execution_enabled must be a boolean")
        if type(prepare_enabled) is not bool:
            raise TypeError("prepare_enabled must be a boolean")
        if type(reclaim_enabled) is not bool:
            raise TypeError("reclaim_enabled must be a boolean")
        self._broker_uid = broker_uid
        self._broker_gid = broker_gid
        self._root_uid = root_uid
        self._root_gid = root_gid
        self._execution_enabled = execution_enabled
        self._prepare_enabled = prepare_enabled
        self._reclaim_enabled = reclaim_enabled
        self._command_runner = command_runner
        self._layout = layout
        self._account_ids = account_ids
        self._account_groups = account_groups
        self._group_ids = group_ids
        self._effect_lock = threading.RLock()
        self._object_validator = object_validator
        self._replica_lifecycle = replica_lifecycle

    @property
    def execution_enabled(self) -> bool:
        return self._execution_enabled

    @property
    def prepare_enabled(self) -> bool:
        return self._prepare_enabled

    @property
    def reclaim_enabled(self) -> bool:
        return self._reclaim_enabled

    def _pool(
        self,
        error: type[LaunchObjectError | PrepareObjectError | ReclaimObjectError],
    ) -> tuple[int, tuple[ExecutorIdentity, ...]]:
        try:
            return _validated_executor_pool(
                account_ids=self._account_ids,
                account_groups=self._account_groups,
                group_ids=self._group_ids,
                broker_uid=self._broker_uid,
                broker_gid=self._broker_gid,
                root_uid=self._root_uid,
                root_gid=self._root_gid,
            )
        except LaunchObjectError as exc:
            raise error("launcher executor pool identity is invalid") from exc

    def _persisted_operation_identity(
        self,
        operation_root: Path,
        pool: tuple[ExecutorIdentity, ...],
        *,
        error: type[LaunchObjectError | PrepareObjectError | ReclaimObjectError],
    ) -> ExecutorIdentity | None:
        marker_path = operation_root / _EXECUTOR_IDENTITY_MARKER
        pending_path = operation_root / _EXECUTOR_IDENTITY_PENDING

        def entry_exists(path: Path) -> bool:
            try:
                path.lstat()
                return True
            except FileNotFoundError:
                return False

        marker_exists = entry_exists(marker_path)
        pending_exists = entry_exists(pending_path)
        if marker_exists and pending_exists:
            raise error("executor identity marker state is ambiguous")
        selected_path = marker_path if marker_exists else pending_path
        try:
            descriptor = os.open(
                selected_path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise error("executor identity marker is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            payload = os.read(descriptor, 65)
        except OSError as exc:
            raise error("executor identity marker is unavailable") from exc
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._root_uid
            or metadata.st_gid != self._root_gid
            or metadata.st_nlink != 1
            or _mode_bits(metadata) != 0o600
            or metadata.st_size != len(payload)
        ):
            raise error("executor identity marker is invalid")
        try:
            name = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            if selected_path == pending_path:
                return None
            raise error("executor identity marker is invalid") from exc
        matches = [identity for identity in pool if identity.name == name]
        if len(matches) != 1:
            if selected_path == pending_path:
                return None
            raise error("executor identity marker is invalid")
        return matches[0]

    def _operation_identity(
        self,
        operation_root: Path,
        pool: tuple[ExecutorIdentity, ...],
        *,
        broker_gid: int,
        error: type[LaunchObjectError | PrepareObjectError | ReclaimObjectError],
        require_complete: bool,
        allow_partial: bool = False,
    ) -> ExecutorIdentity | None:
        try:
            operation = operation_root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(operation.st_mode):
            raise error("executor allocation operation is invalid")
        persisted = self._persisted_operation_identity(operation_root, pool, error=error)
        if persisted is not None and (
            operation.st_uid != self._root_uid or _mode_bits(operation) != 0o700
        ):
            raise error("executor identity marker authority is invalid")
        selected: set[ExecutorIdentity] = set()
        unassigned = {(self._root_uid, self._root_gid), (self._broker_uid, broker_gid)}
        identities = {(identity.uid, identity.gid): identity for identity in pool}
        for name in ("repo", "home"):
            try:
                metadata = (operation_root / name).stat(follow_symlinks=False)
            except FileNotFoundError:
                if allow_partial and persisted is not None:
                    continue
                raise error("executor allocation marker is unavailable") from None
            except OSError as exc:
                raise error("executor allocation marker is unavailable") from exc
            if not stat.S_ISDIR(metadata.st_mode):
                raise error("executor allocation marker is invalid")
            owner = (metadata.st_uid, metadata.st_gid)
            identity = identities.get(owner)
            if identity is not None:
                selected.add(identity)
            elif owner not in unassigned:
                raise error("executor allocation marker owner is invalid")
        if len(selected) > 1:
            raise error("executor allocation markers conflict")
        if persisted is not None and selected and persisted not in selected:
            raise error("executor allocation markers conflict")
        if persisted is not None:
            identity = persisted
        elif selected:
            identity = next(iter(selected))
        else:
            if require_complete:
                raise error("executor allocation marker is missing")
            return None
        if require_complete:
            for name in ("repo", "home"):
                metadata = (operation_root / name).stat(follow_symlinks=False)
                if (metadata.st_uid, metadata.st_gid) != (identity.uid, identity.gid):
                    raise error("executor allocation marker is incomplete")
        return identity

    def _active_allocations(
        self,
        pool: tuple[ExecutorIdentity, ...],
        *,
        broker_gid: int,
        error: type[LaunchObjectError | PrepareObjectError | ReclaimObjectError],
    ) -> dict[str, ExecutorIdentity | None]:
        try:
            entries = tuple(self._layout.replicas_root.iterdir())
        except OSError as exc:
            raise error("executor pool scan failed") from exc
        allocations: dict[str, ExecutorIdentity | None] = {}
        owners: dict[ExecutorIdentity, str] = {}
        for entry in entries:
            if _OPERATION_ID_PATTERN.fullmatch(entry.name) is None:
                raise error("executor pool contains an unknown operation")
            identity = self._operation_identity(
                entry,
                pool,
                broker_gid=broker_gid,
                error=error,
                require_complete=False,
            )
            if identity is not None:
                previous = owners.get(identity)
                if previous is not None and previous != entry.name:
                    raise error("executor allocation slot conflicts")
                owners[identity] = entry.name
            allocations[entry.name] = identity
        return allocations

    def _allocated_for_prepare(self, operation_id: str) -> ExecutorIdentity:
        broker_gid, pool = self._pool(PrepareObjectError)
        allocations = self._active_allocations(
            pool,
            broker_gid=broker_gid,
            error=PrepareObjectError,
        )
        prepared = allocations.get(operation_id)
        if prepared is not None:
            return prepared
        occupied = {identity for identity in allocations.values() if identity is not None}
        try:
            return next(identity for identity in pool if identity not in occupied)
        except StopIteration as exc:
            raise PrepareObjectError("executor pool is exhausted") from exc

    def _allocated_for_existing(
        self,
        operation_id: str,
        error: type[LaunchObjectError | ReclaimObjectError],
        *,
        allow_absent: bool = False,
    ) -> ExecutorIdentity:
        broker_gid, pool = self._pool(error)
        self._active_allocations(pool, broker_gid=broker_gid, error=error)
        roots = [self._layout.replicas_root / operation_id]
        if error is ReclaimObjectError:
            roots.append(self._layout.quarantine_root / operation_id)
        selected: list[ExecutorIdentity] = []
        for index, root in enumerate(roots):
            identity = self._operation_identity(
                root,
                pool,
                broker_gid=broker_gid,
                error=error,
                require_complete=index == 0,
                allow_partial=index > 0,
            )
            if identity is not None:
                selected.append(identity)
        if len(selected) > 1:
            raise error("executor allocation state is ambiguous")
        if selected:
            return selected[0]
        if allow_absent:
            pins: list[tuple[str, os.stat_result]] = []
            for name in (operation_id, f".{operation_id}.pending"):
                try:
                    pins.append(
                        (
                            name,
                            (self._layout.socket_pins_root / name).stat(follow_symlinks=False),
                        )
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise error("executor allocation socket pin is unavailable") from exc
            if not pins:
                return pool[0]
            if len(pins) != 1:
                raise error("executor allocation socket pin state is ambiguous")
            name, pin = pins[0]
            matches = [identity for identity in pool if identity.gid == pin.st_gid]
            final_pin = name == operation_id and len(matches) == 1 and _mode_bits(pin) == 0o660
            transitioned_pending = (
                name != operation_id and len(matches) == 1 and _mode_bits(pin) in {0o600, 0o660}
            )
            initial_pending = (
                name != operation_id and pin.st_gid == broker_gid and _mode_bits(pin) == 0o600
            )
            if (
                not stat.S_ISSOCK(pin.st_mode)
                or pin.st_uid != self._broker_uid
                or not (final_pin or transitioned_pending or initial_pending)
            ):
                raise error("executor allocation socket pin is invalid")
            return matches[0] if matches else pool[0]
        raise error("executor allocation is missing")

    def _launch_validator(self, identity: ExecutorIdentity) -> LaunchObjectValidator:
        if self._object_validator is not None:
            if getattr(self._object_validator, "executor_name", None) != identity.name:
                raise LaunchObjectError("injected launch validator identity is invalid")
            return self._object_validator
        return LaunchObjectValidator(
            layout=self._layout,
            root_uid=self._root_uid,
            root_gid=self._root_gid,
            broker_uid=self._broker_uid,
            broker_gid=self._broker_gid,
            executor_name=identity.name,
            executor_uid=identity.uid,
            executor_gid=identity.gid,
            account_ids=self._account_ids,
            account_groups=self._account_groups,
            group_ids=self._group_ids,
        )

    def _lifecycle_validator(
        self,
        identity: ExecutorIdentity,
        error: type[PrepareObjectError | ReclaimObjectError],
    ) -> ReplicaLifecycleValidator:
        if self._replica_lifecycle is not None:
            if getattr(self._replica_lifecycle, "executor_name", None) != identity.name:
                raise error("injected lifecycle validator identity is invalid")
            return self._replica_lifecycle
        return ReplicaLifecycleValidator(
            layout=self._layout,
            root_uid=self._root_uid,
            root_gid=self._root_gid,
            broker_uid=self._broker_uid,
            broker_gid=self._broker_gid,
            executor_name=identity.name,
            executor_uid=identity.uid,
            executor_gid=identity.gid,
            account_ids=self._account_ids,
            account_groups=self._account_groups,
            group_ids=self._group_ids,
        )

    def command_for(self, frame: bytes, *, peer_uid: int) -> tuple[str, ...]:
        request = parse_launcher_request(
            frame,
            peer_uid=peer_uid,
            broker_uid=self._broker_uid,
        )
        if request.action != "launch":
            raise RootLauncherProtocolError("launcher action does not build a launch command")
        with self._effect_lock:
            identity = self._allocated_for_existing(request.operation_id, LaunchObjectError)
            return build_systemd_run_command(
                request.operation_id,
                identity.name,
                layout=self._layout,
            )

    def execute(self, frame: bytes, *, peer_uid: int) -> tuple[str, ...]:
        """Launch only after deriving the prepared operation's executor identity."""
        request = parse_launcher_request(
            frame,
            peer_uid=peer_uid,
            broker_uid=self._broker_uid,
        )
        if request.action != "launch":
            raise RootLauncherProtocolError("launcher action does not support a launch effect")
        if not self._execution_enabled:
            raise LaunchDisabledError("root launcher execution is disabled")
        with self._effect_lock:
            identity = self._allocated_for_existing(request.operation_id, LaunchObjectError)
            command = build_systemd_run_command(
                request.operation_id,
                identity.name,
                layout=self._layout,
            )
            with self._launch_validator(identity).validate(request.operation_id):
                self._command_runner(command)
            return command

    def prepare(self, frame: bytes, *, peer_uid: int) -> None:
        """Allocate and transfer one broker-staged replica tree."""
        request = parse_launcher_request(frame, peer_uid=peer_uid, broker_uid=self._broker_uid)
        if request.action != "prepare":
            raise RootLauncherProtocolError("launcher action does not match prepare")
        if not self._prepare_enabled:
            raise PrepareDisabledError("root launcher replica prepare is disabled")
        with self._effect_lock:
            identity = self._allocated_for_prepare(request.operation_id)
            with self._lifecycle_validator(identity, PrepareObjectError).prepared(
                request.operation_id
            ):
                pass

    def reclaim(self, frame: bytes, *, peer_uid: int) -> None:
        """Validate the selected identity before quarantining and removing its tree."""
        request = parse_launcher_request(frame, peer_uid=peer_uid, broker_uid=self._broker_uid)
        if request.action != "reclaim":
            raise RootLauncherProtocolError("launcher action does not match reclaim")
        if not self._reclaim_enabled:
            raise ReclaimDisabledError("root launcher replica reclaim is disabled")
        with self._effect_lock:
            identity = self._allocated_for_existing(
                request.operation_id,
                ReclaimObjectError,
                allow_absent=True,
            )
            with self._lifecycle_validator(identity, ReclaimObjectError).reclaimed(
                request.operation_id
            ):
                pass
