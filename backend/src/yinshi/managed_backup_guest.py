"""Create and restore encrypted managed guest archives."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from yinshi.services.managed_backup_crypto import open_managed_backup_job
from yinshi.services.runner_noise import load_or_create_runner_noise_keypair
from yinshi.services.sprite_task_lease import SpriteTaskLease

_ARCHIVE_MAGIC = b"YINSHI-MANAGED-BACKUP-V1\n"
_CHUNK_BYTES = 1024 * 1024
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MANIFEST_NAME = "manifest.json"
_MEMBER_COUNT_MAX = 200_000
_MEMBER_BYTES_MAX = 64 * 1024 * 1024 * 1024
_EXPANDED_BYTES_MAX = 200 * 1024 * 1024 * 1024
_STATE_ROOT = Path("/var/lib/yinshi")
_RESTORE_TRANSACTION_NAME = ".yinshi-restore-active"
_JOB_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


@dataclass(frozen=True, slots=True)
class ManagedArchiveContext:
    """Authenticated identity and lifecycle binding for one guest archive."""

    archive_id: str
    created_at: str
    owner_digest: str
    runtime_generation: int


@dataclass(frozen=True, slots=True)
class ManagedArchiveRecord:
    """Ciphertext metadata returned after durable archive creation."""

    size_bytes: int
    sha256: str


def _require_context(context: ManagedArchiveContext) -> None:
    """Validate bounded archive context before writing authenticated metadata."""
    if not isinstance(context, ManagedArchiveContext):
        raise TypeError("context must be ManagedArchiveContext")
    if not context.archive_id or len(context.archive_id) > 128:
        raise ValueError("archive_id must be bounded non-empty text")
    if not context.created_at or len(context.created_at) > 128:
        raise ValueError("created_at must be bounded non-empty text")
    if len(context.owner_digest) != 64 or any(
        character not in "0123456789abcdef" for character in context.owner_digest
    ):
        raise ValueError("owner_digest must be 64 lowercase hexadecimal characters")
    if type(context.runtime_generation) is not int or context.runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")


def _require_key(key: bytes) -> bytes:
    """Copy one exact AES-256 archive key."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("archive_key must contain exactly 32 bytes")
    return bytes(key)


def _regular_files(root: Path, archive_root: str) -> list[tuple[Path, str]]:
    """List regular files without following links or accepting special entries."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError(f"{archive_root}_root must be an absolute path")
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise ValueError(f"{archive_root}_root must be a real directory")
    files: list[tuple[Path, str]] = []
    for directory_name, directory_names, file_names in os.walk(root, followlinks=False):
        directory = Path(directory_name)
        for name in tuple(directory_names):
            candidate = directory / name
            candidate_metadata = candidate.lstat()
            if not stat.S_ISDIR(candidate_metadata.st_mode) or candidate.is_symlink():
                raise ValueError("managed backup roots must not contain links or special entries")
        for name in file_names:
            candidate = directory / name
            candidate_metadata = candidate.lstat()
            if not stat.S_ISREG(candidate_metadata.st_mode) or candidate.is_symlink():
                raise ValueError("managed backup roots must contain regular files only")
            relative = candidate.relative_to(root)
            member_name = str(PurePosixPath(archive_root, *relative.parts))
            files.append((candidate, member_name))
    return sorted(files, key=lambda item: item[1])


def _manifest(context: ManagedArchiveContext, member_names: tuple[str, ...]) -> bytes:
    """Serialize exact authenticated archive metadata."""
    return (
        json.dumps(
            {
                "context": asdict(context),
                "format": "yinshi-managed-backup-v1",
                "members": list(member_names),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_tar(
    target: BinaryIO,
    files: list[tuple[Path, str]],
    manifest: bytes,
) -> None:
    """Write a deterministic uncompressed archive with bounded copy buffers."""
    with tarfile.open(fileobj=target, mode="w") as archive:
        manifest_member = tarfile.TarInfo(_MANIFEST_NAME)
        manifest_member.size = len(manifest)
        manifest_member.mode = 0o600
        manifest_member.mtime = 0
        archive.addfile(manifest_member, io.BytesIO(manifest))
        for source_path, member_name in files:
            descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                source_metadata = os.fstat(descriptor)
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise ValueError("managed backup source changed during creation")
                member = tarfile.TarInfo(member_name)
                member.size = source_metadata.st_size
                member.mode = 0o600
                member.mtime = 0
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    archive.addfile(member, source)
            finally:
                os.close(descriptor)


def _encrypt_file(source_path: Path, archive_path: Path, key: bytes) -> None:
    """Encrypt one staged tar with AES-256-GCM and durable private output."""
    archive_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if archive_path.exists() or archive_path.is_symlink():
        raise FileExistsError(archive_path)
    nonce = os.urandom(_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_ARCHIVE_MAGIC)
    descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source_path.open("rb") as source, os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(_ARCHIVE_MAGIC)
            output.write(nonce)
            for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def create_managed_backup_archive(
    *,
    sqlite_root: Path,
    files_root: Path,
    archive_path: Path,
    archive_key: bytes,
    context: ManagedArchiveContext,
) -> ManagedArchiveRecord:
    """Create one encrypted archive from the managed guest's durable roots."""
    _require_context(context)
    key = _require_key(archive_key)
    sqlite_files = _regular_files(sqlite_root, "sqlite")
    shared_files = _regular_files(files_root, "files")
    files = sorted(sqlite_files + shared_files, key=lambda item: item[1])
    member_names = tuple(member_name for _path, member_name in files)
    manifest = _manifest(context, member_names)
    temporary_parent = archive_path.parent
    temporary_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".archive-", dir=temporary_parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    os.chmod(temporary_path, 0o600)
    try:
        with temporary_path.open("wb") as target:
            _write_tar(target, files, manifest)
            target.flush()
            os.fsync(target.fileno())
        _encrypt_file(temporary_path, archive_path, key)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with archive_path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return ManagedArchiveRecord(
        size_bytes=archive_path.stat().st_size,
        sha256=digest.hexdigest(),
    )


def _decrypt_to_temporary(archive_path: Path, key: bytes, target_path: Path) -> None:
    """Authenticate and decrypt one managed archive into a private tar file."""
    source_size = archive_path.stat().st_size
    minimum = len(_ARCHIVE_MAGIC) + _NONCE_BYTES + _TAG_BYTES
    if source_size <= minimum:
        raise ValueError("managed backup archive is truncated")
    with archive_path.open("rb") as source:
        if source.read(len(_ARCHIVE_MAGIC)) != _ARCHIVE_MAGIC:
            raise ValueError("managed backup archive header is invalid")
        nonce = source.read(_NONCE_BYTES)
        source.seek(-_TAG_BYTES, os.SEEK_END)
        tag = source.read(_TAG_BYTES)
        remaining = source_size - minimum
        source.seek(len(_ARCHIVE_MAGIC) + _NONCE_BYTES)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(_ARCHIVE_MAGIC)
        with target_path.open("wb") as output:
            while remaining:
                chunk = source.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("managed backup archive is truncated")
                remaining -= len(chunk)
                output.write(decryptor.update(chunk))
            output.write(decryptor.finalize())
            output.flush()
            os.fsync(output.fileno())
    os.chmod(target_path, 0o600)


def _inspect_tar(tar_path: Path, expected_context: ManagedArchiveContext) -> tuple[str, ...]:
    """Validate exact archive layout and return ordered payload member names."""
    members: dict[str, tarfile.TarInfo] = {}
    expanded_bytes = 0
    with tarfile.open(tar_path, mode="r") as archive:
        for index, member in enumerate(archive, start=1):
            if index > _MEMBER_COUNT_MAX:
                raise ValueError("managed backup contains too many members")
            path = PurePosixPath(member.name)
            if (
                not member.isfile()
                or member.name in members
                or member.name.startswith("/")
                or "\\" in member.name
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("managed backup contains an unsafe member")
            if member.size < 0 or member.size > _MEMBER_BYTES_MAX:
                raise ValueError("managed backup member exceeds the size limit")
            expanded_bytes += member.size
            if expanded_bytes > _EXPANDED_BYTES_MAX:
                raise ValueError("managed backup exceeds the expanded size limit")
            members[member.name] = member
        manifest_member = members.pop(_MANIFEST_NAME, None)
        if manifest_member is None or manifest_member.size > 64 * 1024:
            raise ValueError("managed backup manifest is missing or too large")
        manifest_source = archive.extractfile(manifest_member)
        if manifest_source is None:
            raise ValueError("managed backup manifest cannot be read")
        try:
            manifest = json.loads(manifest_source.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("managed backup manifest is invalid") from None
    expected_manifest = {
        "context": asdict(expected_context),
        "format": "yinshi-managed-backup-v1",
        "members": sorted(members),
    }
    if manifest != expected_manifest:
        raise ValueError("managed backup manifest does not match expected context")
    for name in members:
        root = PurePosixPath(name).parts[0]
        if root not in {"sqlite", "files"}:
            raise ValueError("managed backup contains an unexpected root")
    return tuple(sorted(members))


def inspect_managed_backup_archive(
    archive_path: Path,
    *,
    archive_key: bytes,
    expected_context: ManagedArchiveContext,
) -> tuple[str, ...]:
    """Authenticate one managed archive and return its validated members."""
    _require_context(expected_context)
    key = _require_key(archive_key)
    if not isinstance(archive_path, Path) or not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    with tempfile.TemporaryDirectory(prefix="yinshi-managed-inspect-") as directory_name:
        tar_path = Path(directory_name) / "archive.tar"
        _decrypt_to_temporary(archive_path, key, tar_path)
        return _inspect_tar(tar_path, expected_context)


def _extract_validated_tar(tar_path: Path, stage_root: Path) -> None:
    """Copy previously validated members into private restore staging."""
    with tarfile.open(tar_path, mode="r") as archive:
        for member in archive:
            if member.name == _MANIFEST_NAME:
                continue
            target = stage_root.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("managed backup member cannot be read")
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise ValueError("managed backup member is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Persist directory entry changes before the next restore transition."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_restore_state(transaction_root: Path, phase: str) -> None:
    """Persist one exact restore phase inside the fixed transaction directory."""
    state_path = transaction_root / "state.json"
    temporary_path = transaction_root / ".state.json.tmp"
    payload = json.dumps(
        {"phase": phase, "version": 1},
        separators=(",", ":"),
        sort_keys=True,
    )
    with temporary_path.open("w", encoding="utf-8") as output:
        output.write(payload + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, state_path)
    _sync_directory(transaction_root)


def _recover_restore_transaction(sqlite_root: Path, files_root: Path) -> None:
    """Recover old roots from one interrupted pre-commit replacement."""
    transaction_root = sqlite_root.parent / _RESTORE_TRANSACTION_NAME
    if not transaction_root.exists() and not transaction_root.is_symlink():
        return
    if transaction_root.is_symlink() or not transaction_root.is_dir():
        raise ValueError("managed restore transaction path is unsafe")
    state_path = transaction_root / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise ValueError("managed restore transaction state is missing")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("managed restore transaction state is invalid") from None
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or state.get("phase")
        not in {"prepared", "old_sqlite_moved", "old_roots_moved", "new_sqlite_installed"}
    ):
        raise ValueError("managed restore transaction state is invalid")
    rollback_root = transaction_root / "rollback"
    for live_root in (sqlite_root, files_root):
        rollback = rollback_root / live_root.name
        if rollback.exists():
            if rollback.is_symlink() or not rollback.is_dir():
                raise ValueError("managed restore rollback root is unsafe")
            if live_root.exists() or live_root.is_symlink():
                failed_root = transaction_root / f"failed-{live_root.name}"
                if failed_root.exists() or failed_root.is_symlink():
                    shutil.rmtree(failed_root)
                os.replace(live_root, failed_root)
            os.replace(rollback, live_root)
            _sync_directory(sqlite_root.parent)
    if not sqlite_root.is_dir() or not files_root.is_dir():
        raise ValueError("managed restore rollback is incomplete")
    shutil.rmtree(transaction_root)
    _sync_directory(sqlite_root.parent)


def restore_managed_backup_archive(
    archive_path: Path,
    *,
    archive_key: bytes,
    expected_context: ManagedArchiveContext,
    sqlite_root: Path,
    files_root: Path,
) -> None:
    """Replace both guest data roots after full authentication and staging."""
    _require_context(expected_context)
    key = _require_key(archive_key)
    if sqlite_root.parent != files_root.parent or sqlite_root == files_root:
        raise ValueError("managed restore roots must be distinct siblings")
    state_root = sqlite_root.parent
    if not state_root.is_dir() or state_root.is_symlink():
        raise ValueError("managed restore state root must be a real directory")
    _recover_restore_transaction(sqlite_root, files_root)
    for root in (sqlite_root, files_root):
        if root.is_symlink() or not root.is_dir():
            raise ValueError("managed restore targets must be real directories")
    with tempfile.TemporaryDirectory(prefix=".yinshi-restore-stage-", dir=state_root) as stage_name:
        stage_root = Path(stage_name)
        tar_path = stage_root / "archive.tar"
        _decrypt_to_temporary(archive_path, key, tar_path)
        _inspect_tar(tar_path, expected_context)
        extracted_root = stage_root / "extracted"
        extracted_root.mkdir(mode=0o700)
        staged_sqlite = extracted_root / "sqlite"
        staged_files = extracted_root / "files"
        staged_sqlite.mkdir(mode=0o700)
        staged_files.mkdir(mode=0o700)
        _extract_validated_tar(tar_path, extracted_root)
        if not staged_sqlite.is_dir() or not staged_files.is_dir():
            raise ValueError("managed backup is missing required data roots")
        transaction_root = state_root / _RESTORE_TRANSACTION_NAME
        transaction_root.mkdir(mode=0o700)
        rollback_root = transaction_root / "rollback"
        rollback_root.mkdir(mode=0o700)
        rollback_sqlite = rollback_root / "sqlite"
        rollback_files = rollback_root / "files"
        _write_restore_state(transaction_root, "prepared")
        try:
            os.replace(sqlite_root, rollback_sqlite)
            _sync_directory(state_root)
            _write_restore_state(transaction_root, "old_sqlite_moved")
            os.replace(files_root, rollback_files)
            _sync_directory(state_root)
            _write_restore_state(transaction_root, "old_roots_moved")
            os.replace(staged_sqlite, sqlite_root)
            _sync_directory(state_root)
            _write_restore_state(transaction_root, "new_sqlite_installed")
            os.replace(staged_files, files_root)
            _sync_directory(state_root)
        except BaseException:
            _recover_restore_transaction(sqlite_root, files_root)
            raise
        else:
            shutil.rmtree(transaction_root)
            _sync_directory(state_root)


def _decode_archive_key(value: object) -> bytes:
    """Decode one canonical unpadded archive key from a sealed job."""
    if not isinstance(value, str) or not value:
        raise ValueError("managed backup job archive_key is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise ValueError("managed backup job archive_key is invalid") from None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != 32 or canonical != value:
        raise ValueError("managed backup job archive_key is invalid")
    return decoded


def _job_context(value: object) -> ManagedArchiveContext:
    """Parse exact authenticated archive context from one sealed job."""
    if not isinstance(value, dict) or set(value) != {
        "archive_id",
        "created_at",
        "owner_digest",
        "runtime_generation",
    }:
        raise ValueError("managed backup job archive context is invalid")
    context = ManagedArchiveContext(
        archive_id=value["archive_id"],
        created_at=value["created_at"],
        owner_digest=value["owner_digest"],
        runtime_generation=value["runtime_generation"],
    )
    _require_context(context)
    return context


def _read_existing_job_result(
    path: Path,
    *,
    archive_path: Path,
    job_id: str,
    operation: str,
) -> bool:
    """Accept only complete same-job output from an interrupted control transfer."""
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError("managed backup result path is unsafe")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("managed backup result is invalid") from None
    if operation == "restore":
        if result != {"job_id": job_id, "status": "restored"}:
            raise ValueError("managed backup result is invalid")
        return True
    if (
        not isinstance(result, dict)
        or set(result) != {"job_id", "sha256", "size_bytes", "status"}
        or result.get("job_id") != job_id
        or result.get("status") != "ready"
        or type(result.get("size_bytes")) is not int
        or result["size_bytes"] <= 0
        or not isinstance(result.get("sha256"), str)
        or len(result["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in result["sha256"])
        or archive_path.is_symlink()
        or not archive_path.is_file()
        or archive_path.stat().st_size != result["size_bytes"]
    ):
        raise ValueError("managed backup result is invalid")
    digest = hashlib.sha256()
    with archive_path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    if digest.hexdigest() != result["sha256"]:
        raise ValueError("managed backup result is invalid")
    return True


def _write_job_result(path: Path, payload: dict[str, object]) -> None:
    """Atomically write one private fixed-shape maintenance result."""
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_managed_backup_job(
    *,
    job_path: Path,
    result_path: Path,
    runner_private_key: bytes,
    sqlite_root: Path,
    files_root: Path,
    maintenance_root: Path,
    expected_job_id: str,
) -> None:
    """Open and execute one exact managed backup maintenance job."""
    if job_path.parent != maintenance_root or result_path.parent != maintenance_root:
        raise ValueError("managed backup job paths must be inside maintenance_root")
    if job_path.is_symlink() or not job_path.is_file():
        raise ValueError("managed backup job must be a regular file")
    payload = open_managed_backup_job(
        job_path.read_bytes(),
        runner_private_key=runner_private_key,
        expected_job_id=expected_job_id,
    )
    if not isinstance(payload, dict) or set(payload) != {
        "archive_context",
        "archive_key",
        "job_id",
        "operation",
        "version",
    }:
        raise ValueError("managed backup job has an invalid shape")
    operation = payload["operation"]
    if (
        payload["version"] != 1
        or payload["job_id"] != expected_job_id
        or operation not in {"create", "restore"}
    ):
        raise ValueError("managed backup job is invalid")
    context = _job_context(payload["archive_context"])
    archive_path = maintenance_root / f"{expected_job_id}.archive.enc"
    if _read_existing_job_result(
        result_path,
        archive_path=archive_path,
        job_id=expected_job_id,
        operation=operation,
    ):
        return
    archive_key = _decode_archive_key(payload["archive_key"])
    if operation == "create":
        record = create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=archive_path,
            archive_key=archive_key,
            context=context,
        )
        result = {
            "job_id": expected_job_id,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "status": "ready",
        }
    else:
        restore_managed_backup_archive(
            archive_path,
            archive_key=archive_key,
            expected_context=context,
            sqlite_root=sqlite_root,
            files_root=files_root,
        )
        result = {"job_id": expected_job_id, "status": "restored"}
    _write_job_result(result_path, result)


async def hold_managed_backup_job(
    *,
    job_id: str,
    maintenance_root: Path,
    run_job: Callable[[], None],
    timeout_seconds: int,
) -> None:
    """Hold one expiring Sprite task through control-plane transfer completion."""
    if _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("job_id must be a canonical lowercase UUID")
    if maintenance_root.is_symlink() or not maintenance_root.is_dir():
        raise ValueError("maintenance_root must be a real directory")
    if not callable(run_job):
        raise TypeError("run_job must be callable")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    release_path = maintenance_root / f"{job_id}.release"
    tracked_paths = (
        maintenance_root / f"{job_id}.job",
        maintenance_root / f"{job_id}.result",
        maintenance_root / f"{job_id}.archive.enc",
        release_path,
    )
    lease = SpriteTaskLease()
    await lease.acquire()
    released = False
    job_completed = False
    try:
        await asyncio.to_thread(run_job)
        job_completed = True
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not release_path.is_file() or release_path.is_symlink():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("managed backup release timed out")
            await asyncio.sleep(1.0)
        released = True
    finally:
        await lease.aclose()
        if released or not job_completed:
            for path in tracked_paths:
                if path.is_file() and not path.is_symlink():
                    path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run one sealed guest job using only fixed managed paths."""
    parser = argparse.ArgumentParser(prog="yinshi-managed-backup")
    parser.add_argument("operation", choices=("create", "restore"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--hold-seconds", type=int)
    arguments = parser.parse_args(argv)
    if _JOB_ID_PATTERN.fullmatch(arguments.job_id) is None:
        parser.error("--job-id must be a canonical lowercase UUID")
    maintenance_root = _STATE_ROOT / "maintenance"

    def run_job() -> None:
        run_managed_backup_job(
            job_path=maintenance_root / f"{arguments.job_id}.job",
            result_path=maintenance_root / f"{arguments.job_id}.result",
            runner_private_key=load_or_create_runner_noise_keypair(
                _STATE_ROOT / "runner-noise.key"
            ).private_key,
            sqlite_root=_STATE_ROOT / "sqlite",
            files_root=_STATE_ROOT / "files",
            maintenance_root=maintenance_root,
            expected_job_id=arguments.job_id,
        )

    if arguments.hold_seconds is None:
        run_job()
    else:
        asyncio.run(
            hold_managed_backup_job(
                job_id=arguments.job_id,
                maintenance_root=maintenance_root,
                run_job=run_job,
                timeout_seconds=arguments.hold_seconds,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
