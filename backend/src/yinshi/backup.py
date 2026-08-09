"""Encrypted, SQLCipher-aware backup creation and archive decryption."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.base import CipherContext

from yinshi.config import get_settings, tenant_db_encryption_enabled
from yinshi.db import get_control_db
from yinshi.services.accounts import make_tenant
from yinshi.tenant import (
    TenantContext,
    _open_sqlcipher_connection,
    _tenant_database_key,
    get_user_db,
)

_ARCHIVE_MAGIC = b"YINSHI-BACKUP-V1\n"
_BACKUP_CHUNK_BYTES = 1024 * 1024
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16


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
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.commit()
        integrity_row = target.execute("PRAGMA integrity_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise RuntimeError("SQLite backup failed integrity validation")
    finally:
        target.close()
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
                user_rows = control_database.execute(
                    "SELECT id, email FROM users ORDER BY id"
                ).fetchall()
                _backup_sqlite_connection(
                    control_database,
                    staging_directory / "control.db",
                )

            tenant_count = 0
            for user_row in user_rows:
                tenant = make_tenant(str(user_row["id"]), str(user_row["email"]))
                if not Path(tenant.db_path).is_file():
                    continue
                target_path = (
                    staging_directory / "users" / tenant.user_id[:2] / tenant.user_id / "yinshi.db"
                )
                _backup_tenant_database(tenant, target_path)
                tenant_count += 1

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


def _main() -> int:
    """Run the backup command-line interface."""
    parser = argparse.ArgumentParser(description="Create an encrypted Yinshi database backup")
    parser.parse_args()
    archive_path = create_backup()
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
