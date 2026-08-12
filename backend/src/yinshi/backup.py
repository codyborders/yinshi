"""Encrypted, SQLCipher-aware backup creation and archive decryption."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.base import CipherContext

from yinshi.config import get_settings, tenant_db_encryption_enabled
from yinshi.db import get_control_db
from yinshi.services.accounts import make_tenant
from yinshi.services.crypto import (
    derive_subkey,
    is_wrapped_dek_envelope,
    unwrap_dek,
    unwrap_dek_with_keks,
)
from yinshi.tenant import (
    TenantContext,
    _open_sqlcipher_connection,
    _tenant_database_key,
    _validate_encrypted_user_database,
    get_user_db,
)

_ARCHIVE_MAGIC = b"YINSHI-BACKUP-V1\n"
_BACKUP_CHUNK_BYTES = 1024 * 1024
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_MAX_ENCRYPTED_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MANIFEST_KEYS = {"created_at", "format", "tenant_database_count"}
_MANAGED_FLY_BACKUP_ERROR = "Local backup commands are unavailable in managed Fly mode"


@dataclass(frozen=True)
class _RestoreArchive:
    """Validated archive metadata needed for restore."""

    tenant_members: dict[str, tarfile.TarInfo]
    control_member: tarfile.TarInfo


def _backup_key_from_settings() -> bytes:
    """Decode the separately managed 256-bit backup key."""
    settings = get_settings()
    if settings.backup_encryption_key is None:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must be configured for backups")
    encoded_key = settings.backup_encryption_key.get_secret_value().strip()
    try:
        key = bytes.fromhex(encoded_key)
    except ValueError as exc:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must be 64 hexadecimal characters") from exc
    if len(key) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def _write_encrypted_chunks(
    source: BinaryIO,
    target: BinaryIO,
    encryptor: CipherContext,
) -> None:
    """Encrypt a source stream into a target stream with bounded memory."""
    if source.closed or target.closed:
        raise ValueError("source and target must be open")
    while True:
        chunk = source.read(_BACKUP_CHUNK_BYTES)
        if not chunk:
            break
        target.write(encryptor.update(chunk))
    target.write(encryptor.finalize())


def _encrypt_archive(source_path: Path, target_path: Path, key: bytes) -> None:
    """Encrypt one tar archive with AES-256-GCM and an authenticated header."""
    if len(key) != 32:
        raise ValueError("key must contain exactly 32 bytes")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if target_path.exists():
        raise FileExistsError(target_path)

    nonce = os.urandom(_GCM_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_ARCHIVE_MAGIC)
    file_descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            source_path.open("rb") as source,
            os.fdopen(file_descriptor, "wb", closefd=False) as target,
        ):
            target.write(_ARCHIVE_MAGIC)
            target.write(nonce)
            _write_encrypted_chunks(source, target, encryptor)
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(file_descriptor)
    os.chmod(target_path, 0o600)


def decrypt_backup_archive(source_path: Path, target_path: Path, key: bytes) -> None:
    """Decrypt and authenticate one backup into a tar archive."""
    if len(key) != 32:
        raise ValueError("key must contain exactly 32 bytes")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if target_path.exists():
        raise FileExistsError(target_path)

    source_size = source_path.stat().st_size
    if source_size > _MAX_ENCRYPTED_ARCHIVE_BYTES:
        raise ValueError("encrypted backup exceeds the restore size limit")
    minimum_size = len(_ARCHIVE_MAGIC) + _GCM_NONCE_BYTES + _GCM_TAG_BYTES
    if source_size <= minimum_size:
        raise ValueError("encrypted backup is truncated")

    with source_path.open("rb") as source:
        magic = source.read(len(_ARCHIVE_MAGIC))
        if magic != _ARCHIVE_MAGIC:
            raise ValueError("encrypted backup header is invalid")
        nonce = source.read(_GCM_NONCE_BYTES)
        source.seek(-_GCM_TAG_BYTES, os.SEEK_END)
        tag = source.read(_GCM_TAG_BYTES)
        ciphertext_bytes = source_size - minimum_size
        source.seek(len(_ARCHIVE_MAGIC) + _GCM_NONCE_BYTES)

        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(_ARCHIVE_MAGIC)
        file_descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(file_descriptor, "wb", closefd=False) as target:
                remaining = ciphertext_bytes
                while remaining > 0:
                    chunk = source.read(min(_BACKUP_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup is truncated")
                    remaining -= len(chunk)
                    target.write(decryptor.update(chunk))
                target.write(decryptor.finalize())
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
        finally:
            os.close(file_descriptor)
    os.chmod(target_path, 0o600)


def _backup_sqlite_connection(source: sqlite3.Connection, target_path: Path) -> None:
    """Write and validate one plaintext SQLite snapshot."""
    if target_path.exists():
        raise FileExistsError(target_path)
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cipher_row = source.execute("PRAGMA cipher_version").fetchone()
    if cipher_row is not None and cipher_row[0]:
        attached = False
        try:
            source.execute(
                "ATTACH DATABASE ? AS backup_snapshot KEY ''",
                (str(target_path),),
            )
            attached = True
            source.execute("SELECT sqlcipher_export('backup_snapshot')").fetchone()
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
        finally:
            if attached:
                source.execute("DETACH DATABASE backup_snapshot")
    else:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()

    target = sqlite3.connect(target_path)
    try:
        integrity_row = target.execute("PRAGMA integrity_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise RuntimeError("SQLite backup failed integrity validation")
    finally:
        target.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{target_path}{suffix}").unlink(missing_ok=True)
    os.chmod(target_path, 0o600)


def _backup_tenant_database(tenant: TenantContext, target_path: Path) -> None:
    """Snapshot one tenant database under its configured encryption policy."""
    settings = get_settings()
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with get_user_db(tenant) as source:
        if tenant_db_encryption_enabled(settings):
            key = _tenant_database_key(tenant)
            target = _open_sqlcipher_connection(str(target_path), key)
            try:
                source.backup(target)
                target.commit()
                integrity_row = target.execute("PRAGMA integrity_check").fetchone()
                if integrity_row is None or str(integrity_row[0]).lower() != "ok":
                    raise RuntimeError("Encrypted tenant backup failed integrity validation")
            finally:
                target.close()
            for suffix in ("-wal", "-shm"):
                Path(f"{target_path}{suffix}").unlink(missing_ok=True)
            os.chmod(target_path, 0o600)
        else:
            _backup_sqlite_connection(source, target_path)


def _purge_stale_staging(backup_directory: Path) -> None:
    """Remove abandoned private staging directories from interrupted backups."""
    for candidate in backup_directory.glob(".staging-*"):
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)
    for candidate in backup_directory.glob(".archive-*.tar.gz"):
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink()


def create_backup() -> Path:
    """Create an encrypted control and tenant database backup archive."""
    settings = get_settings()
    if settings.managed_runtime_provider == "fly_sprites":
        raise RuntimeError(_MANAGED_FLY_BACKUP_ERROR)
    backup_key = _backup_key_from_settings()
    backup_directory = Path(settings.backup_dir).resolve()
    backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_directory, 0o700)
    _purge_stale_staging(backup_directory)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archive_path = backup_directory / f"yinshi-{timestamp}.tar.gz.enc"
    temporary_tar = backup_directory / f".archive-{timestamp}.tar.gz"
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=backup_directory) as staging_name:
            staging_directory = Path(staging_name)
            os.chmod(staging_directory, 0o700)
            with get_control_db() as control_database:
                control_database.execute("BEGIN IMMEDIATE")
                try:
                    user_rows = control_database.execute(
                        "SELECT id, email FROM users ORDER BY id"
                    ).fetchall()
                    with get_control_db() as snapshot_database:
                        _backup_sqlite_connection(
                            snapshot_database,
                            staging_directory / "control.db",
                        )

                    tenant_count = 0
                    for user_row in user_rows:
                        tenant = make_tenant(str(user_row["id"]), str(user_row["email"]))
                        if not Path(tenant.db_path).is_file():
                            continue
                        target_path = (
                            staging_directory
                            / "users"
                            / tenant.user_id[:2]
                            / tenant.user_id
                            / "yinshi.db"
                        )
                        _backup_tenant_database(tenant, target_path)
                        tenant_count += 1
                finally:
                    control_database.rollback()

            manifest = {
                "created_at": datetime.now(UTC).isoformat(),
                "format": "yinshi-backup-v1",
                "tenant_database_count": tenant_count,
            }
            manifest_path = staging_directory / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)

            with tarfile.open(temporary_tar, mode="w:gz") as archive:
                for child in sorted(staging_directory.iterdir(), key=lambda path: path.name):
                    archive.add(child, arcname=child.name, recursive=True)
            os.chmod(temporary_tar, 0o600)
            _encrypt_archive(temporary_tar, archive_path, backup_key)
    finally:
        temporary_tar.unlink(missing_ok=True)
        os.umask(previous_umask)

    return archive_path


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    """Read one regular archive member under a strict byte limit."""
    if member.size > limit:
        raise ValueError(f"archive member exceeds size limit: {member.name}")
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"archive member cannot be read: {member.name}")
    payload = source.read(limit + 1)
    if len(payload) != member.size or len(payload) > limit:
        raise ValueError(f"archive member has an invalid size: {member.name}")
    return payload


def _validate_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> int:
    """Require the exact version-one manifest object and field types."""
    try:
        manifest = json.loads(_read_member(archive, member, _MAX_MANIFEST_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("backup manifest must contain the exact v1 fields")
    if manifest["format"] != "yinshi-backup-v1":
        raise ValueError("backup manifest format is unsupported")
    created_at = manifest["created_at"]
    if not isinstance(created_at, str):
        raise ValueError("backup manifest created_at must be a timestamp")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ValueError("backup manifest created_at must be an ISO timestamp") from exc
    if parsed_created_at.tzinfo is None:
        raise ValueError("backup manifest created_at must include a timezone")
    tenant_count = manifest["tenant_database_count"]
    if type(tenant_count) is not int or tenant_count < 0:
        raise ValueError("backup manifest tenant_database_count must be nonnegative")
    return tenant_count


def _tenant_id_from_member(name: str) -> str | None:
    """Return the tenant ID when a member follows the version-one database path."""
    parts = PurePosixPath(name).parts
    if len(parts) != 4 or parts[0] != "users" or parts[3] != "yinshi.db":
        return None
    prefix, user_id = parts[1], parts[2]
    if not user_id or prefix != user_id[:2]:
        return None
    return user_id


def _inspect_archive_members(tar_path: Path) -> _RestoreArchive:
    """Reject unsafe members and require the exact version-one archive layout."""
    members: dict[str, tarfile.TarInfo] = {}
    total_size = 0
    with tarfile.open(tar_path, mode="r:gz") as archive:
        for index, member in enumerate(archive, start=1):
            if index > _MAX_ARCHIVE_MEMBERS:
                raise ValueError("backup archive contains too many members")
            name = member.name
            parts = name.split("/")
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError(f"backup archive contains an unsafe path: {name}")
            if name in members:
                raise ValueError(f"backup archive contains a duplicate path: {name}")
            if not member.isfile() and not member.isdir():
                raise ValueError(f"backup archive contains an unsafe member: {name}")
            if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(f"backup archive member exceeds size limit: {name}")
            total_size += member.size
            if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("backup archive exceeds the expanded size limit")
            members[name] = member

        file_members = {name: member for name, member in members.items() if member.isfile()}
        if "manifest.json" not in file_members or "control.db" not in file_members:
            raise ValueError("backup archive is missing required v1 files")
        tenant_members: dict[str, tarfile.TarInfo] = {}
        for name, member in file_members.items():
            if name in {"manifest.json", "control.db"}:
                continue
            user_id = _tenant_id_from_member(name)
            if user_id is None:
                raise ValueError(f"backup archive contains an unexpected file: {name}")
            if user_id in tenant_members:
                raise ValueError(f"backup archive contains duplicate tenant data: {user_id}")
            tenant_members[user_id] = member

        allowed_directories = {"users"}
        for member in tenant_members.values():
            path = PurePosixPath(member.name)
            allowed_directories.add(str(path.parent))
            allowed_directories.add(str(path.parent.parent))
        directory_names = {name for name, member in members.items() if member.isdir()}
        if not directory_names.issubset(allowed_directories):
            raise ValueError("backup archive contains unexpected directories")

        tenant_count = _validate_manifest(archive, file_members["manifest.json"])
        if tenant_count != len(tenant_members):
            raise ValueError("backup manifest tenant count does not match archive")
        return _RestoreArchive(
            tenant_members=tenant_members,
            control_member=file_members["control.db"],
        )


def _copy_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
) -> None:
    """Copy one archive member with bounded memory and private permissions."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"archive member cannot be read: {member.name}")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            remaining = member.size
            while remaining:
                chunk = source.read(min(_BACKUP_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError(f"archive member is truncated: {member.name}")
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _validate_control_database(path: Path) -> None:
    """Require restored control database integrity before installation."""
    with path.open("rb") as database_file:
        if database_file.read(16) != b"SQLite format 3\x00":
            raise ValueError("control database is invalid")
    try:
        database = sqlite3.connect(path)
        try:
            rows = database.execute("PRAGMA integrity_check").fetchall()
            if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
                raise ValueError("control database failed SQLite integrity validation")
        finally:
            database.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("control database is invalid") from exc


def _validate_plain_tenant_database(path: Path) -> None:
    """Require one staged plaintext tenant database to pass integrity checks."""
    with path.open("rb") as database_file:
        if database_file.read(16) != b"SQLite format 3\x00":
            raise ValueError("tenant database is invalid")
    try:
        database = sqlite3.connect(path)
        try:
            rows = database.execute("PRAGMA integrity_check").fetchall()
            if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
                raise ValueError("tenant database failed SQLite integrity validation")
        finally:
            database.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("tenant database is invalid") from exc


def _staged_tenant_database_key(control_path: Path, user_id: str) -> bytes:
    """Derive one tenant database key from staged control data only."""
    database = sqlite3.connect(control_path)
    try:
        row = database.execute(
            "SELECT encrypted_dek FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        database.close()
    if row is None:
        raise ValueError("staged control database is missing the tenant user")
    wrapped_dek = row[0]
    if not isinstance(wrapped_dek, bytes) or not wrapped_dek:
        raise ValueError("staged control database is missing the tenant encryption key")

    settings = get_settings()
    try:
        if is_wrapped_dek_envelope(wrapped_dek):
            keyring = settings.key_encryption_keyring_previous
            current_kek = settings.key_encryption_key_bytes
            if current_kek:
                keyring[settings.key_encryption_key_id] = current_kek
            user_dek = unwrap_dek_with_keks(wrapped_dek, user_id, keyring)
        else:
            pepper = settings.encryption_pepper_bytes
            if not pepper:
                raise ValueError("legacy encryption pepper is unavailable")
            user_dek = unwrap_dek(wrapped_dek, user_id, pepper)
    except Exception:
        raise ValueError("staged tenant encryption key could not be unwrapped") from None
    return derive_subkey(
        user_dek,
        purpose="tenant-sqlcipher",
        context=user_id,
    )


def _validate_staged_tenant_database(
    path: Path,
    user_id: str,
    control_path: Path,
) -> None:
    """Validate one staged tenant under the configured encryption policy."""
    if not tenant_db_encryption_enabled(get_settings()):
        _validate_plain_tenant_database(path)
        return
    sqlcipher_key = _staged_tenant_database_key(control_path, user_id)
    try:
        _validate_encrypted_user_database(str(path), sqlcipher_key)
    except Exception:
        raise ValueError("encrypted tenant database is invalid") from None


def _assert_no_symlink_components(path: Path) -> None:
    """Reject every existing symlink component without resolving it."""
    absolute_path = Path(os.path.abspath(path))
    current = Path(absolute_path.anchor)
    for component in absolute_path.parts[1:]:
        current /= component
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError(f"restore destination contains a symlink: {current}")


def _prepare_destination_parent(path: Path) -> None:
    """Create missing destination parents without following existing symlinks."""
    absolute_path = Path(os.path.abspath(path))
    current = Path(absolute_path.anchor)
    for component in absolute_path.parts[1:]:
        current /= component
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            component_stat = os.lstat(current)
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError(f"restore destination contains a symlink: {current}")
        if not stat.S_ISDIR(component_stat.st_mode):
            raise NotADirectoryError(current)


def _sync_directory(path: Path) -> None:
    """Persist directory entry changes before rollback data is removed."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_private_file(source: Path, target: Path) -> None:
    """Copy one rollback file with bounded memory and private permissions."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            source.open("rb") as input_file,
            os.fdopen(descriptor, "wb", closefd=False) as output_file,
        ):
            shutil.copyfileobj(input_file, output_file, _BACKUP_CHUNK_BYTES)
            output_file.flush()
            os.fsync(output_file.fileno())
    finally:
        os.close(descriptor)


def _restore_private_file(source: Path, target: Path) -> None:
    """Restore one file without consuming its private recovery copy."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".yinshi-restore-recover-",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()
    try:
        _copy_private_file(source, temporary_path)
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def _existing_tenant_database_paths(users_root: Path) -> set[Path]:
    """Find tenant database files without traversing symlinked directories."""
    if not users_root.exists():
        return set()
    if not users_root.is_dir():
        raise NotADirectoryError(users_root)

    database_paths: set[Path] = set()
    for prefix_directory in users_root.iterdir():
        prefix_stat = os.lstat(prefix_directory)
        if stat.S_ISLNK(prefix_stat.st_mode):
            raise ValueError(f"restore destination contains a symlink: {prefix_directory}")
        if not stat.S_ISDIR(prefix_stat.st_mode):
            continue
        for tenant_directory in prefix_directory.iterdir():
            tenant_stat = os.lstat(tenant_directory)
            if stat.S_ISLNK(tenant_stat.st_mode):
                raise ValueError(f"restore destination contains a symlink: {tenant_directory}")
            if not stat.S_ISDIR(tenant_stat.st_mode):
                continue
            database_path = tenant_directory / "yinshi.db"
            try:
                database_stat = os.lstat(database_path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(database_stat.st_mode):
                raise ValueError(f"restore destination contains a symlink: {database_path}")
            if not stat.S_ISREG(database_stat.st_mode):
                raise ValueError(f"restore destination is not a regular file: {database_path}")
            database_paths.add(database_path)
    return database_paths


def _install_staged_databases(
    installations: list[tuple[Path, Path]],
    removals: set[Path] | None = None,
) -> None:
    """Replace staged databases while retaining durable rollback copies."""
    removal_targets = set() if removals is None else set(removals)
    installation_targets = {target for _, target in installations}
    if removal_targets & installation_targets:
        raise ValueError("restore targets cannot be installed and removed")
    for target in installation_targets | removal_targets:
        _assert_no_symlink_components(target)
        _prepare_destination_parent(target.parent)
        _assert_no_symlink_components(target)
    sidecars = {
        Path(f"{target}{suffix}")
        for target in installation_targets | removal_targets
        for suffix in ("-wal", "-shm")
    }
    for sidecar in sidecars:
        _assert_no_symlink_components(sidecar)
    existing = {path for path in installation_targets | removal_targets | sidecars if path.exists()}
    rollback_roots: dict[Path, Path] = {}
    rollback_paths: dict[Path, Path] = {}
    changed: set[Path] = set()
    remove_rollback_roots = True
    try:
        for index, path in enumerate(sorted(existing, key=str)):
            root = rollback_roots.get(path.parent)
            if root is None:
                root = Path(tempfile.mkdtemp(prefix=".yinshi-restore-rollback-", dir=path.parent))
                os.chmod(root, 0o700)
                rollback_roots[path.parent] = root
            rollback = root / str(index)
            _copy_private_file(path, rollback)
            rollback_paths[path] = rollback

        for root in rollback_roots.values():
            _sync_directory(root)
            _sync_directory(root.parent)

        try:
            for sidecar in sidecars & existing:
                sidecar.unlink()
                changed.add(sidecar)
            for target in removal_targets & existing:
                target.unlink()
                changed.add(target)
            for stage, target in installations:
                os.replace(stage, target)
                changed.add(target)
                os.chmod(target, 0o600)
            for directory in {path.parent for path in changed}:
                _sync_directory(directory)
        except Exception as installation_error:
            rollback_errors: list[Exception] = []
            for target in sorted(changed, key=str, reverse=True):
                rollback_candidate = rollback_paths.get(target)
                try:
                    if rollback_candidate is None:
                        target.unlink(missing_ok=True)
                    else:
                        _restore_private_file(rollback_candidate, target)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            for path, rollback in rollback_paths.items():
                if path in changed or path.exists() or not rollback.exists():
                    continue
                try:
                    _restore_private_file(rollback, path)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            for directory in {path.parent for path in changed}:
                try:
                    _sync_directory(directory)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                remove_rollback_roots = False
                raise rollback_errors[0] from installation_error
            raise
    finally:
        if remove_rollback_roots:
            for root in rollback_roots.values():
                shutil.rmtree(root)


def restore_backup(archive_path: Path, *, confirm_replace: bool = False) -> None:
    """Authenticate an encrypted backup before inspecting restore destinations."""
    settings = get_settings()
    if settings.managed_runtime_provider == "fly_sprites":
        raise RuntimeError(_MANAGED_FLY_BACKUP_ERROR)
    source_path = Path(archive_path).resolve(strict=True)
    backup_key = _backup_key_from_settings()
    with tempfile.TemporaryDirectory(prefix="yinshi-restore-decrypt-") as decrypt_name:
        tar_path = Path(decrypt_name) / "archive.tar.gz"
        decrypt_backup_archive(source_path, tar_path, backup_key)
        restore_archive = _inspect_archive_members(tar_path)
        control_target = Path(os.path.abspath(settings.control_db_path))
        users_target = Path(os.path.abspath(settings.user_data_dir))
        _assert_no_symlink_components(control_target)
        _assert_no_symlink_components(users_target)
        if control_target.exists() and not confirm_replace:
            raise FileExistsError("restore replacement requires explicit confirmation")
        _prepare_destination_parent(control_target.parent)
        _prepare_destination_parent(users_target.parent)
        _assert_no_symlink_components(control_target)
        _assert_no_symlink_components(users_target)
        with (
            tempfile.TemporaryDirectory(
                prefix=".yinshi-restore-stage-", dir=control_target.parent
            ) as control_stage_name,
            tempfile.TemporaryDirectory(
                prefix=".yinshi-restore-stage-", dir=users_target.parent
            ) as tenant_stage_name,
            tarfile.open(tar_path, mode="r:gz") as archive,
        ):
            control_stage = Path(control_stage_name) / "control.db"
            tenant_stage = Path(tenant_stage_name)
            _copy_archive_member(archive, restore_archive.control_member, control_stage)
            _validate_control_database(control_stage)
            installations: list[tuple[Path, Path]] = []
            staged_tenants: list[tuple[str, Path]] = []
            for user_id, member in restore_archive.tenant_members.items():
                staged = tenant_stage / user_id[:2] / user_id / "yinshi.db"
                target = users_target / user_id[:2] / user_id / "yinshi.db"
                _copy_archive_member(archive, member, staged)
                installations.append((staged, target))
                staged_tenants.append((user_id, staged))
            for user_id, staged in staged_tenants:
                _validate_staged_tenant_database(staged, user_id, control_stage)
            installations.append((control_stage, control_target))
            archived_tenant_targets = {
                target for _, target in installations if target != control_target
            }
            removed_tenant_targets = (
                _existing_tenant_database_paths(users_target) - archived_tenant_targets
            )
            _install_staged_databases(installations, removed_tenant_targets)


def _main() -> int:
    """Run the backup command-line interface."""
    parser = argparse.ArgumentParser(
        description="Create or restore an encrypted Yinshi database backup"
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("create", help="create an encrypted backup")
    restore_parser = commands.add_parser(
        "restore",
        help="restore an encrypted backup while application writers are stopped",
    )
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument(
        "--confirm-replace",
        "--confirm",
        action="store_true",
        dest="confirm_replace",
        help="confirm replacement of configured databases",
    )
    arguments = parser.parse_args()
    if arguments.command == "restore":
        restore_backup(
            arguments.archive,
            confirm_replace=arguments.confirm_replace,
        )
        return 0
    archive_path = create_backup()
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
