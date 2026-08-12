"""Encrypted backup tests verify confidentiality and restorable database content."""

import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import _configure_test_env


def _encrypted_tar_with_member(tmp_path: Path, member_kind: str) -> Path:
    """Build one authenticated archive containing a hostile member."""
    from yinshi.backup import _encrypt_archive

    tar_path = tmp_path / f"{member_kind}.tar.gz"
    with tarfile.open(tar_path, mode="w:gz") as archive:
        first = tarfile.TarInfo("../control.db" if member_kind == "traversal" else "bad")
        if member_kind == "link":
            first.type = tarfile.SYMTYPE
            first.linkname = "control.db"
            archive.addfile(first)
        elif member_kind == "device":
            first.type = tarfile.CHRTYPE
            archive.addfile(first)
        else:
            first.size = 1
            archive.addfile(first, io.BytesIO(b"x"))
        if member_kind == "duplicate":
            duplicate = tarfile.TarInfo("bad")
            duplicate.size = 1
            archive.addfile(duplicate, io.BytesIO(b"y"))
    encrypted_path = tmp_path / f"{member_kind}.tar.gz.enc"
    _encrypt_archive(tar_path, encrypted_path, bytes.fromhex("ab" * 32))
    return encrypted_path


def _encrypted_copy_with_corrupt_tenant(archive_path: Path, tmp_path: Path) -> Path:
    """Copy one valid backup while corrupting its tenant database member."""
    from yinshi.backup import _encrypt_archive, decrypt_backup_archive

    source_tar = tmp_path / "valid-backup.tar.gz"
    decrypt_backup_archive(archive_path, source_tar, bytes.fromhex("ab" * 32))
    extracted = tmp_path / "corrupt-backup"
    extracted.mkdir()
    with tarfile.open(source_tar, mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    tenant_databases = list(extracted.glob("users/*/*/yinshi.db"))
    assert len(tenant_databases) == 1
    tenant_databases[0].write_bytes(b"not a database")
    corrupt_tar = tmp_path / "corrupt-backup.tar.gz"
    with tarfile.open(corrupt_tar, mode="w:gz") as archive:
        for child in sorted(extracted.iterdir(), key=lambda path: path.name):
            archive.add(child, arcname=child.name, recursive=True)
    encrypted = tmp_path / "corrupt-backup.tar.gz.enc"
    _encrypt_archive(corrupt_tar, encrypted, bytes.fromhex("ab" * 32))
    return encrypted


def _encrypted_copy_with_control_change(
    archive_path: Path,
    tmp_path: Path,
    user_id: str,
    change: str,
) -> Path:
    """Copy a valid backup after changing one staged control user row."""
    from yinshi.backup import _encrypt_archive, decrypt_backup_archive

    source_tar = tmp_path / f"{change}-source.tar.gz"
    decrypt_backup_archive(archive_path, source_tar, bytes.fromhex("ab" * 32))
    extracted = tmp_path / f"{change}-backup"
    extracted.mkdir()
    with tarfile.open(source_tar, mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    control = sqlite3.connect(extracted / "control.db")
    try:
        if change == "missing-user":
            control.execute("DELETE FROM users WHERE id = ?", (user_id,))
        elif change == "missing-key":
            control.execute(
                "UPDATE users SET encrypted_dek = NULL WHERE id = ?",
                (user_id,),
            )
        else:
            raise ValueError("unsupported staged control change")
        control.commit()
    finally:
        control.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{extracted / 'control.db'}{suffix}").unlink(missing_ok=True)
    changed_tar = tmp_path / f"{change}.tar.gz"
    with tarfile.open(changed_tar, mode="w:gz") as archive:
        for child in sorted(extracted.iterdir(), key=lambda path: path.name):
            archive.add(child, arcname=child.name, recursive=True)
    encrypted = tmp_path / f"{change}.tar.gz.enc"
    _encrypt_archive(changed_tar, encrypted, bytes.fromhex("ab" * 32))
    return encrypted


def _encrypted_tar_with_manifest(tmp_path: Path, manifest: object) -> Path:
    """Build one authenticated archive with a caller-supplied manifest."""
    from yinshi.backup import _encrypt_archive

    tar_path = tmp_path / "manifest.tar.gz"
    payload = json.dumps(manifest).encode("utf-8")
    with tarfile.open(tar_path, mode="w:gz") as archive:
        manifest_member = tarfile.TarInfo("manifest.json")
        manifest_member.size = len(payload)
        archive.addfile(manifest_member, io.BytesIO(payload))
        control_member = tarfile.TarInfo("control.db")
        control_member.size = 1
        archive.addfile(control_member, io.BytesIO(b"x"))
    encrypted_path = tmp_path / "manifest.tar.gz.enc"
    _encrypt_archive(tar_path, encrypted_path, bytes.fromhex("ab" * 32))
    return encrypted_path


def test_create_backup_rejects_managed_fly_before_archive_creation(tmp_path, monkeypatch) -> None:
    """Managed Fly control planes should reject local backup creation immediately."""
    from types import SimpleNamespace

    import yinshi.backup as backup_module

    backup_directory = tmp_path / "backups"
    settings = SimpleNamespace(
        backup_dir=str(backup_directory),
        managed_runtime_provider="fly_sprites",
    )
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        backup_module,
        "_backup_key_from_settings",
        lambda: pytest.fail("backup key must not be read"),
    )

    with pytest.raises(
        RuntimeError,
        match="^Local backup commands are unavailable in managed Fly mode$",
    ):
        backup_module.create_backup()

    assert not backup_directory.exists()


def test_create_backup_blocks_control_user_writes_through_tenant_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control user writes must wait until every tenant snapshot is complete."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    backup_directory = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(backup_directory))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import create_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="locked-backup-user",
        email="locked-backup@example.com",
        display_name="Locked Backup User",
    )
    with get_user_db(tenant):
        pass

    tenant_blocker = sqlite3.connect(tenant.db_path)
    tenant_blocker.execute("PRAGMA journal_mode = DELETE")
    tenant_blocker.execute("BEGIN EXCLUSIVE")
    backup_errors: list[BaseException] = []

    def run_backup() -> None:
        try:
            create_backup()
        except BaseException as error:
            backup_errors.append(error)

    backup_thread = threading.Thread(target=run_backup)
    backup_thread.start()
    try:
        deadline = time.monotonic() + 5
        while not list(backup_directory.glob(".staging-*/control.db")):
            if time.monotonic() >= deadline:
                pytest.fail("backup did not reach tenant snapshot phase")
            time.sleep(0.01)

        statements = [
            (
                "INSERT INTO users (id, email) VALUES (?, ?)",
                ("concurrent-user", "concurrent@example.com"),
            ),
            (
                "UPDATE users SET encrypted_dek = ? WHERE id = ?",
                (b"new-wrapped-key", tenant.user_id),
            ),
            ("DELETE FROM users WHERE id = ?", (tenant.user_id,)),
        ]
        for statement, parameters in statements:
            writer = sqlite3.connect(get_settings().control_db_path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute(statement, parameters)
            finally:
                writer.close()
    finally:
        tenant_blocker.rollback()
        tenant_blocker.close()
        backup_thread.join(timeout=5)

    assert not backup_thread.is_alive()
    assert backup_errors == []

    writer = sqlite3.connect(get_settings().control_db_path, timeout=0)
    try:
        writer.execute(
            "UPDATE users SET encrypted_dek = encrypted_dek WHERE id = ?",
            (tenant.user_id,),
        )
        writer.rollback()
    finally:
        writer.close()


def test_backup_archive_is_encrypted_and_restorable(tmp_path, monkeypatch) -> None:
    """Backups should hide tenant data and decrypt into a valid SQLite snapshot."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    backup_directory = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(backup_directory))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    init_control_db()

    from yinshi.backup import create_backup, decrypt_backup_archive
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="backup-user",
        email="backup@example.com",
        display_name="Backup User",
    )
    secret_marker = "tenant-private-backup-marker"
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("private-repo", str(tmp_path / "repo"), secret_marker),
        )
        database.commit()

    archive_path = create_backup()

    assert archive_path.parent == backup_directory
    assert archive_path.suffix == ".enc"
    assert archive_path.stat().st_mode & 0o777 == 0o600
    assert secret_marker.encode("utf-8") not in archive_path.read_bytes()

    restored_tar = tmp_path / "restored.tar.gz"
    decrypt_backup_archive(archive_path, restored_tar, bytes.fromhex("ab" * 32))
    restore_directory = tmp_path / "restored"
    restore_directory.mkdir()
    with tarfile.open(restored_tar, mode="r:gz") as archive:
        archive.extractall(restore_directory, filter="data")

    restored_databases = list(restore_directory.glob("users/*/*/yinshi.db"))
    assert len(restored_databases) == 1
    restored_database = sqlite3.connect(restored_databases[0])
    row = restored_database.execute("SELECT custom_prompt FROM repos").fetchone()
    restored_database.close()
    assert row == (secret_marker,)
    assert list(backup_directory.glob(".staging-*")) == []


def test_restore_backup_rejects_managed_fly_before_decryption_or_mutation(
    tmp_path, monkeypatch
) -> None:
    """Managed Fly control planes should reject local restore before any data access."""
    from types import SimpleNamespace

    import yinshi.backup as backup_module

    archive_path = tmp_path / "customer-data.enc"
    archive_path.write_bytes(b"not-an-archive")
    control_path = tmp_path / "control.db"
    control_path.write_bytes(b"current-control-data")
    user_data_directory = tmp_path / "users"
    user_data_directory.mkdir()
    user_marker = user_data_directory / "current-user-data"
    user_marker.write_bytes(b"current-user-data")
    settings = SimpleNamespace(
        control_db_path=str(control_path),
        managed_runtime_provider="fly_sprites",
        user_data_dir=str(user_data_directory),
    )
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        backup_module,
        "_backup_key_from_settings",
        lambda: pytest.fail("backup key must not be read"),
    )
    monkeypatch.setattr(
        backup_module,
        "decrypt_backup_archive",
        lambda *args: pytest.fail("archive must not be decrypted"),
    )

    with pytest.raises(
        RuntimeError,
        match="^Local backup commands are unavailable in managed Fly mode$",
    ):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    assert control_path.read_bytes() == b"current-control-data"
    assert user_marker.read_bytes() == b"current-user-data"


def test_restore_requires_confirmation_before_replacing_data(tmp_path, monkeypatch) -> None:
    """Restore should leave configured databases unchanged without confirmation."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    backup_directory = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(backup_directory))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    init_control_db()

    from yinshi.backup import create_backup, restore_backup

    archive_path = create_backup()
    control_path = Path(get_settings().control_db_path)
    original_bytes = control_path.read_bytes()

    with pytest.raises(FileExistsError, match="confirmation"):
        restore_backup(archive_path)

    assert control_path.read_bytes() == original_bytes


def test_restore_authenticates_before_confirmation_check(tmp_path, monkeypatch) -> None:
    """Restore should reject unauthenticated data before inspecting destinations."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from cryptography.exceptions import InvalidTag

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    init_control_db()
    archive_path = create_backup()
    damaged = bytearray(archive_path.read_bytes())
    damaged[-1] ^= 1
    archive_path.write_bytes(damaged)

    with pytest.raises(InvalidTag):
        restore_backup(archive_path)


@pytest.mark.parametrize("member_kind", ["traversal", "link", "device", "duplicate"])
def test_restore_rejects_unsafe_archive_members(tmp_path, monkeypatch, member_kind: str) -> None:
    """Restore should reject unsafe member types, paths, and duplicates."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import restore_backup
    from yinshi.config import get_settings

    get_settings.cache_clear()
    archive_path = _encrypted_tar_with_member(tmp_path, member_kind)

    with pytest.raises(ValueError, match="unsafe|duplicate"):
        restore_backup(archive_path, confirm_replace=True)


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "created_at": "2025-01-01T00:00:00+00:00",
            "format": "yinshi-backup-v1",
            "tenant_database_count": 0,
            "extra": True,
        },
        {
            "created_at": "2025-01-01T00:00:00+00:00",
            "format": "yinshi-backup-v1",
            "tenant_database_count": False,
        },
    ],
)
def test_restore_requires_exact_v1_manifest(tmp_path, monkeypatch, manifest: object) -> None:
    """Restore should reject extra manifest fields and ambiguous field types."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import restore_backup
    from yinshi.config import get_settings

    get_settings.cache_clear()
    archive_path = _encrypted_tar_with_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="manifest"):
        restore_backup(archive_path, confirm_replace=True)


def test_restore_installs_validated_control_and_tenant_databases(tmp_path, monkeypatch) -> None:
    """Restore should replace configured databases with authenticated snapshots."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="restore-user",
        email="restore@example.com",
        display_name="Restore User",
    )
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("restore-repo", str(tmp_path / "repo"), "backed-up"),
        )
        database.commit()
    archive_path = create_backup()
    with get_user_db(tenant) as database:
        database.execute("UPDATE repos SET custom_prompt = ?", ("changed",))
        database.commit()

    restore_backup(archive_path, confirm_replace=True)

    with get_user_db(tenant) as database:
        row = database.execute("SELECT custom_prompt FROM repos").fetchone()
    assert row[0] == "backed-up"
    assert Path(get_settings().control_db_path).stat().st_mode & 0o777 == 0o600
    assert Path(tenant.db_path).stat().st_mode & 0o777 == 0o600


def test_restore_rejects_invalid_control_database(tmp_path, monkeypatch) -> None:
    """Restore should validate control SQLite integrity before replacement."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import restore_backup
    from yinshi.config import get_settings

    get_settings.cache_clear()
    archive_path = _encrypted_tar_with_manifest(
        tmp_path,
        {
            "created_at": "2025-01-01T00:00:00+00:00",
            "format": "yinshi-backup-v1",
            "tenant_database_count": 0,
        },
    )
    control_target = Path(get_settings().control_db_path)
    original = control_target.read_bytes() if control_target.exists() else None

    with pytest.raises(ValueError, match="control database"):
        restore_backup(archive_path, confirm_replace=True)

    assert (control_target.read_bytes() if control_target.exists() else None) == original


def test_restore_validates_plain_tenants_before_replacing_any_database(
    tmp_path, monkeypatch
) -> None:
    """Restore should reject a corrupt staged tenant before any replacement."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="invalid-tenant-user",
        email="invalid-tenant@example.com",
        display_name="Invalid Tenant User",
    )
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("invalid-tenant-repo", str(tmp_path / "repo"), "backed-up"),
        )
        database.commit()
    valid_archive = backup_module.create_backup()
    archive_path = _encrypted_copy_with_corrupt_tenant(valid_archive, tmp_path)
    with get_user_db(tenant) as database:
        database.execute("UPDATE repos SET custom_prompt = ?", ("current",))
        database.commit()
    control_path = Path(get_settings().control_db_path)
    current_control = control_path.read_bytes()

    with pytest.raises(ValueError, match="tenant database"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    assert control_path.read_bytes() == current_control
    with get_user_db(tenant) as database:
        row = database.execute("SELECT custom_prompt FROM repos").fetchone()
    assert row[0] == "current"


def test_backup_and_restore_round_trip_sqlcipher_tenant_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup and restore must preserve a SQLCipher tenant database."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "44" * 32)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "current")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="sqlcipher-round-trip",
        email="sqlcipher-round-trip@example.com",
        display_name="SQLCipher Round Trip",
    )
    with get_user_db(tenant) as database:
        database.execute("CREATE TABLE restore_marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO restore_marker VALUES ('staged')")
        database.commit()
    archive_path = create_backup()
    with get_user_db(tenant) as database:
        database.execute("UPDATE restore_marker SET value = 'current'")
        database.commit()

    restore_backup(archive_path, confirm_replace=True)

    with get_user_db(tenant) as database:
        value = database.execute("SELECT value FROM restore_marker").fetchone()[0]
    assert value == "staged"


@pytest.mark.parametrize("wrapping_source", ["current", "previous", "legacy"])
def test_restore_validates_encrypted_tenants_with_keys_from_staged_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapping_source: str,
) -> None:
    """Encrypted restore must derive its validation key from staged control data."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "current")
    monkeypatch.setenv("KEY_ENCRYPTION_KEYS_PREVIOUS", "")
    if wrapping_source in {"current", "previous"}:
        monkeypatch.setenv("KEY_ENCRYPTION_KEY", "11" * 32)
        monkeypatch.setenv(
            "KEY_ENCRYPTION_KEY_ID",
            "current" if wrapping_source == "current" else "previous",
        )

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.services.crypto import derive_subkey
    from yinshi.services.keys import get_user_dek
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id=f"encrypted-restore-{wrapping_source}",
        email=f"encrypted-restore-{wrapping_source}@example.com",
        display_name="Encrypted Restore User",
    )
    expected_key = derive_subkey(
        get_user_dek(tenant.user_id),
        purpose="tenant-sqlcipher",
        context=tenant.user_id,
    )
    with get_user_db(tenant) as database:
        database.execute("CREATE TABLE restore_marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO restore_marker VALUES ('staged')")
        database.commit()
    archive_path = backup_module.create_backup()

    if wrapping_source == "previous":
        monkeypatch.setenv("KEY_ENCRYPTION_KEY", "22" * 32)
        monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "current")
        monkeypatch.setenv(
            "KEY_ENCRYPTION_KEYS_PREVIOUS",
            json.dumps({"previous": "11" * 32}),
        )
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    validated: list[tuple[Path, bytes]] = []

    def validate_encrypted(path: str, key: bytes) -> None:
        validated.append((Path(path), key))

    monkeypatch.setattr(
        backup_module,
        "_validate_encrypted_user_database",
        validate_encrypted,
        raising=False,
    )

    backup_module.restore_backup(archive_path, confirm_replace=True)

    assert len(validated) == 1
    assert validated[0][0].name == "yinshi.db"
    assert validated[0][1] == expected_key


@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("missing-user", "staged control database is missing the tenant user"),
        ("missing-key", "staged control database is missing the tenant encryption key"),
        ("unavailable-module", "encrypted tenant database is invalid"),
        ("wrong-key", "encrypted tenant database is invalid"),
        ("malformed-database", "encrypted tenant database is invalid"),
        ("integrity-failure", "encrypted tenant database is invalid"),
    ],
)
def test_restore_rejects_encrypted_tenant_validation_failures_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_error: str,
) -> None:
    """Encrypted tenant failures must use local errors before destination changes."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "33" * 32)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "current")

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id=f"restore-failure-{failure_kind}",
        email=f"restore-failure-{failure_kind}@example.com",
        display_name="Restore Failure User",
    )
    with get_user_db(tenant):
        pass
    valid_archive = backup_module.create_backup()
    archive_path = valid_archive
    if failure_kind in {"missing-user", "missing-key"}:
        archive_path = _encrypted_copy_with_control_change(
            valid_archive,
            tmp_path,
            tenant.user_id,
            failure_kind,
        )

    with get_control_db() as control:
        control.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            ("Current Destination", tenant.user_id),
        )
        control.commit()
        control.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    control_path = Path(get_settings().control_db_path)
    tenant_path = Path(tenant.db_path)
    current_control = control_path.read_bytes()
    current_tenant = tenant_path.read_bytes()

    if failure_kind not in {"missing-user", "missing-key"}:

        def reject_encrypted_database(_path: str, _key: bytes) -> None:
            raise RuntimeError(f"external detail for {failure_kind}")

        monkeypatch.setattr(
            backup_module,
            "_validate_encrypted_user_database",
            reject_encrypted_database,
        )
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match=f"^{expected_error}$"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    assert control_path.read_bytes() == current_control
    assert tenant_path.read_bytes() == current_tenant


def test_restore_rejects_symlinked_user_data_root(tmp_path, monkeypatch) -> None:
    """Restore should reject a configured user root that is a symlink."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="symlink-root-user",
        email="symlink-root@example.com",
        display_name="Symlink Root User",
    )
    archive_path = create_backup()
    outside_directory = tmp_path / "outside-users"
    outside_directory.mkdir()
    symlinked_root = tmp_path / "symlinked-users"
    symlinked_root.symlink_to(outside_directory, target_is_directory=True)
    monkeypatch.setenv("USER_DATA_DIR", str(symlinked_root))
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="symlink"):
        restore_backup(archive_path, confirm_replace=True)

    assert not (outside_directory / tenant.user_id[:2] / tenant.user_id / "yinshi.db").exists()


@pytest.mark.parametrize("symlink_kind", ["target", "wal", "shm"])
def test_restore_rejects_symlinked_existing_database_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_kind: str,
) -> None:
    """Restore must reject database targets and sidecars that are symlinks."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    init_control_db()
    archive_path = create_backup()
    control_target = Path(get_settings().control_db_path)
    suffix = "" if symlink_kind == "target" else f"-{symlink_kind}"
    symlink_path = Path(f"{control_target}{suffix}")
    symlink_path.unlink(missing_ok=True)
    outside = tmp_path / f"outside-{symlink_kind}"
    outside.write_bytes(b"outside-data")
    symlink_path.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        restore_backup(archive_path, confirm_replace=True)

    assert symlink_path.is_symlink()
    assert outside.read_bytes() == b"outside-data"


def test_restore_rechecks_destination_components_before_installation(tmp_path, monkeypatch) -> None:
    """Restore should reject a destination symlink planted after staging."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="late-symlink-user",
        email="late-symlink@example.com",
        display_name="Late Symlink User",
    )
    archive_path = backup_module.create_backup()
    user_root = Path(get_settings().user_data_dir)
    outside_directory = tmp_path / "late-symlink-outside"
    outside_directory.mkdir()
    real_install = backup_module._install_staged_databases

    def plant_symlink_then_install(installations, removals=()) -> None:
        prefix_directory = user_root / tenant.user_id[:2]
        shutil.rmtree(prefix_directory)
        prefix_directory.symlink_to(outside_directory, target_is_directory=True)
        real_install(installations, removals)

    monkeypatch.setattr(
        backup_module,
        "_install_staged_databases",
        plant_symlink_then_install,
    )

    with pytest.raises(ValueError, match="symlink"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    assert not (outside_directory / tenant.user_id / "yinshi.db").exists()


def test_restore_syncs_rollback_directories_before_database_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback files and directory entries must be durable before replacement."""
    import yinshi.backup as backup_module

    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    first_target = first_parent / "control.db"
    second_target = second_parent / "yinshi.db"
    first_target.write_bytes(b"old-control")
    second_target.write_bytes(b"old-tenant")
    first_stage = tmp_path / "staged-control.db"
    second_stage = tmp_path / "staged-tenant.db"
    first_stage.write_bytes(b"new-control")
    second_stage.write_bytes(b"new-tenant")
    events: list[tuple[str, Path]] = []
    real_sync_directory = backup_module._sync_directory
    real_replace = os.replace

    def record_sync(path: Path) -> None:
        events.append(("sync", path))
        real_sync_directory(path)

    def record_replace(source: Path, target: Path) -> None:
        events.append(("replace", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(backup_module, "_sync_directory", record_sync)
    monkeypatch.setattr(os, "replace", record_replace)

    backup_module._install_staged_databases(
        [(first_stage, first_target), (second_stage, second_target)]
    )

    first_replace = next(index for index, event in enumerate(events) if event[0] == "replace")
    pre_replace_syncs = {path for action, path in events[:first_replace] if action == "sync"}
    rollback_directories = {
        path for path in pre_replace_syncs if path.name.startswith(".yinshi-restore-rollback-")
    }
    assert len(rollback_directories) == 2
    assert {path.parent for path in rollback_directories} == {first_parent, second_parent}
    assert {first_parent, second_parent}.issubset(pre_replace_syncs)


def test_restore_removes_tenant_database_missing_from_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful restore must remove tenant databases absent from the archive."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    from yinshi.backup import create_backup, restore_backup
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    archive_path = create_backup()
    extra_tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="tenant-absent-from-archive",
        email="tenant-absent-from-archive@example.com",
        display_name="Tenant Absent From Archive",
    )
    with get_user_db(extra_tenant) as database:
        database.execute("CREATE TABLE current_marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO current_marker VALUES ('current')")
        database.commit()
    extra_path = Path(extra_tenant.db_path)

    restore_backup(archive_path, confirm_replace=True)

    assert not extra_path.exists()


def test_restore_recovers_quarantined_tenant_after_durability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed durability sync must restore tenants absent from the archive."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    archive_path = backup_module.create_backup()
    extra_tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="quarantined-tenant",
        email="quarantined-tenant@example.com",
        display_name="Quarantined Tenant",
    )
    with get_user_db(extra_tenant) as database:
        database.execute("CREATE TABLE current_marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO current_marker VALUES ('current')")
        database.commit()
    extra_path = Path(extra_tenant.db_path)
    control_target = Path(get_settings().control_db_path)
    replacement_started = False
    durability_failure_raised = False
    real_replace = os.replace
    real_sync_directory = backup_module._sync_directory

    def record_replace(source: Path, target: Path) -> None:
        nonlocal replacement_started
        real_replace(source, target)
        if Path(target) == control_target:
            replacement_started = True

    def fail_success_sync(path: Path) -> None:
        nonlocal durability_failure_raised
        if replacement_started and not durability_failure_raised:
            durability_failure_raised = True
            raise OSError("simulated durability failure")
        real_sync_directory(path)

    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(backup_module, "_sync_directory", fail_success_sync)

    with pytest.raises(OSError, match="simulated durability failure"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    assert extra_path.exists()
    with get_user_db(extra_tenant) as database:
        value = database.execute("SELECT value FROM current_marker").fetchone()[0]
    assert value == "current"


def test_restore_rolls_back_after_installation_failure(tmp_path, monkeypatch) -> None:
    """Restore should put old databases back after a replacement failure."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="rollback-user",
        email="rollback@example.com",
        display_name="Rollback User",
    )
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("rollback-repo", str(tmp_path / "repo"), "backed-up"),
        )
        database.commit()
    archive_path = backup_module.create_backup()
    with get_user_db(tenant) as database:
        database.execute("UPDATE repos SET custom_prompt = ?", ("current",))
        database.commit()

    real_replace = backup_module.os.replace
    replace_calls = 0

    def fail_second_replace(source, target) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated installation failure")
        real_replace(source, target)

    monkeypatch.setattr(backup_module.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated installation failure"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    with get_user_db(tenant) as database:
        row = database.execute("SELECT custom_prompt FROM repos").fetchone()
    assert row[0] == "current"


def test_restore_rolls_back_after_permission_failure(tmp_path, monkeypatch) -> None:
    """Restore should put old databases back after replacement permission failure."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="permission-rollback-user",
        email="permission-rollback@example.com",
        display_name="Permission Rollback User",
    )
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("permission-repo", str(tmp_path / "repo"), "backed-up"),
        )
        database.commit()
    archive_path = backup_module.create_backup()
    with get_user_db(tenant) as database:
        database.execute("UPDATE repos SET custom_prompt = ?", ("current",))
        database.commit()

    real_chmod = backup_module.os.chmod

    def fail_installed_database_chmod(path, mode) -> None:
        if Path(path) == Path(tenant.db_path):
            raise PermissionError("simulated permission failure")
        real_chmod(path, mode)

    monkeypatch.setattr(backup_module.os, "chmod", fail_installed_database_chmod)

    with pytest.raises(PermissionError, match="simulated permission failure"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    with get_user_db(tenant) as database:
        row = database.execute("SELECT custom_prompt FROM repos").fetchone()
    assert row[0] == "current"


def test_restore_retains_recovery_copies_when_rollback_fails(tmp_path, monkeypatch) -> None:
    """Restore should retain private recovery copies after incomplete rollback."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "ab" * 32)

    import yinshi.backup as backup_module
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    get_settings.cache_clear()
    init_control_db()
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="retained-rollback-user",
        email="retained-rollback@example.com",
        display_name="Retained Rollback User",
    )
    with get_user_db(tenant) as database:
        database.execute(
            "INSERT INTO repos (name, root_path, custom_prompt) VALUES (?, ?, ?)",
            ("retained-repo", str(tmp_path / "repo"), "backed-up"),
        )
        database.commit()
    archive_path = backup_module.create_backup()
    with get_user_db(tenant) as database:
        database.execute("UPDATE repos SET custom_prompt = ?", ("current",))
        database.commit()

    real_chmod = backup_module.os.chmod
    real_replace = backup_module.os.replace

    def fail_installed_database_chmod(path, mode) -> None:
        if Path(path) == Path(tenant.db_path):
            raise PermissionError("simulated permission failure")
        real_chmod(path, mode)

    def fail_rollback_replace(source, target) -> None:
        if Path(source).name.startswith(".yinshi-restore-recover-"):
            raise OSError("simulated rollback failure")
        real_replace(source, target)

    monkeypatch.setattr(backup_module.os, "chmod", fail_installed_database_chmod)
    monkeypatch.setattr(backup_module.os, "replace", fail_rollback_replace)

    with pytest.raises(OSError, match="simulated rollback failure"):
        backup_module.restore_backup(archive_path, confirm_replace=True)

    rollback_directories = list(Path(tenant.db_path).parent.glob(".yinshi-restore-rollback-*"))
    assert len(rollback_directories) == 1
    recovery_databases = [
        path
        for path in rollback_directories[0].iterdir()
        if path.read_bytes().startswith(b"SQLite format 3\x00")
    ]
    assert len(recovery_databases) == 1
    recovered = sqlite3.connect(recovery_databases[0])
    row = recovered.execute("SELECT custom_prompt FROM repos").fetchone()
    recovered.close()
    assert row[0] == "current"


def _backup_script_python(tmp_path: Path, name: str = "python") -> Path:
    """Build a backup stub that delegates helper programs to Python."""
    python = tmp_path / name
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then\n'
        "  printf '%s\\n' \"$BACKUP_TEST_ARCHIVE\"\n"
        "  exit 0\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o700)
    return python


def test_backup_script_runs_configured_upload_executable(tmp_path: Path) -> None:
    """Scheduled backups must send the encrypted artifact to configured storage."""
    app_root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "yinshi-test.tar.gz.enc"
    archive.write_bytes(b"encrypted")
    upload_record = tmp_path / "uploaded-path"
    environment_record = tmp_path / "upload-environment"
    python_startup_record = tmp_path / "python-startup"
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "record = os.environ.get('YINSHI_TEST_PYTHON_STARTUP')\n"
        "if record:\n"
        "    Path(record).write_text('loaded', encoding='utf-8')\n",
        encoding="utf-8",
    )
    python = _backup_script_python(tmp_path)
    uploader = tmp_path / "upload"
    uploader.write_text(
        "#!/bin/sh\n"
        "uploader_dir=${0%/*}\n"
        "if mode=$(/usr/bin/stat -f '%Lp' -- \"$uploader_dir\" 2>/dev/null); then :; "
        "else mode=$(/usr/bin/stat -c '%a' -- \"$uploader_dir\"); fi\n"
        f'printf \'%s\\n\' "$0" "$#" "$1" "$mode" > {upload_record!s}\n'
        f"/usr/bin/env > {environment_record!s}\n"
        "exit 23\n",
        encoding="utf-8",
    )
    uploader.chmod(0o700)
    environment = {
        **os.environ,
        "YINSHI_APP_ROOT": str(app_root),
        "YINSHI_PYTHON_BIN": str(python),
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_UPLOAD_COMMAND": str(uploader),
        "BACKUP_TEST_ARCHIVE": str(archive),
        "BACKUP_ENCRYPTION_KEY": "backup-secret",
        "DATABASE_URL": "application-secret",
        "SECRET_KEY": "application-secret-key",
        "AWS_SECRET_ACCESS_KEY": "aws-provider-token",
        "FLY_API_TOKEN": "fly-provider-token",
        "GITHUB_TOKEN": "github-provider-token",
        "GOOGLE_APPLICATION_CREDENTIALS": "/provider/credentials.json",
        "HOME": "/hostile-home",
        "TMPDIR": "/hostile-tmp",
        "PYTHONPATH": str(tmp_path),
        "YINSHI_TEST_PYTHON_STARTUP": str(python_startup_record),
    }

    result = subprocess.run(
        [str(app_root / "backend" / "scripts" / "backup.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 23
    execution = upload_record.read_text(encoding="utf-8").splitlines()
    private_uploader = Path(execution[0])
    assert execution[1:] == ["1", str(archive), "700"]
    assert private_uploader != uploader
    assert not private_uploader.exists()
    assert not private_uploader.parent.exists()
    assert not python_startup_record.exists()
    uploader_environment = environment_record.read_text(encoding="utf-8")
    assert "PATH=/usr/bin:/bin" in uploader_environment.splitlines()
    for variable in (
        "HOME",
        "TMPDIR",
        "BACKUP_ENCRYPTION_KEY",
        "BACKUP_UPLOAD_COMMAND",
        "BACKUP_TEST_ARCHIVE",
        "DATABASE_URL",
        "SECRET_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "FLY_API_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PYTHONPATH",
        "YINSHI_TEST_PYTHON_STARTUP",
    ):
        assert f"{variable}=" not in uploader_environment


def test_backup_script_runs_validated_uploader_bytes_after_path_replacement(
    tmp_path: Path,
) -> None:
    """Uploader execution must use bytes associated with validated metadata."""
    app_root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "yinshi-test.tar.gz.enc"
    archive.write_bytes(b"encrypted")
    expected_record = tmp_path / "expected-upload"
    malicious_record = tmp_path / "malicious-upload"
    python = _backup_script_python(tmp_path, "python-race-test")
    transformer = (
        "import os, subprocess, sys\n"
        "script = sys.stdin.read()\n"
        "needle = '    metadata = os.fstat(source_fd)\\n'\n"
        "injection = needle + '    os.replace(os.environ[\"YINSHI_TEST_REPLACEMENT\"], source_path)\\n'\n"
        "script = script.replace(needle, injection, 1)\n"
        "result = subprocess.run([sys.executable, '-I', '-', *sys.argv[2:]], "
        "input=script, text=True)\n"
        "raise SystemExit(result.returncode)\n"
    )
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then\n'
        "  printf '%s\\n' \"$BACKUP_TEST_ARCHIVE\"\n"
        "  exit 0\n"
        "fi\n"
        "shift\n"
        f'exec {shlex.quote(sys.executable)} -I -c {shlex.quote(transformer)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o700)
    uploader = tmp_path / "upload-race-test"
    uploader.write_text(
        f"#!/bin/sh\nprintf 'expected\\n' > {expected_record!s}\n",
        encoding="utf-8",
    )
    uploader.chmod(0o700)
    replacement = tmp_path / "replacement-upload"
    replacement.write_text(
        f"#!/bin/sh\nprintf 'replaced\\n' > {malicious_record!s}\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)
    hostile_bin = tmp_path / "hostile-bin"
    hostile_bin.mkdir()
    environment = {
        **os.environ,
        "YINSHI_APP_ROOT": str(app_root),
        "YINSHI_PYTHON_BIN": str(python),
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_UPLOAD_COMMAND": str(uploader),
        "BACKUP_TEST_ARCHIVE": str(archive),
        "PATH": f"{hostile_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": "/hostile-home",
        "TMPDIR": "/hostile-tmp",
        "BACKUP_ENCRYPTION_KEY": "backup-secret",
        "DATABASE_URL": "application-secret",
        "SECRET_KEY": "application-secret-key",
        "AWS_SECRET_ACCESS_KEY": "aws-provider-token",
        "FLY_API_TOKEN": "fly-provider-token",
        "GITHUB_TOKEN": "github-provider-token",
        "GOOGLE_APPLICATION_CREDENTIALS": "/provider/credentials.json",
        "YINSHI_TEST_REPLACEMENT": str(replacement),
    }

    result = subprocess.run(
        [str(app_root / "backend" / "scripts" / "backup.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    assert expected_record.read_text(encoding="utf-8") == "expected\n"
    assert uploader.read_text(encoding="utf-8").startswith("#!/bin/sh\nprintf 'replaced")
    assert not malicious_record.exists()


@pytest.mark.parametrize("unsafe_kind", ["directory", "symlink"])
def test_backup_script_rejects_unsafe_upload_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """Uploader path must name a regular file without symlink indirection."""
    app_root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "yinshi-test.tar.gz.enc"
    archive.write_bytes(b"encrypted")
    python = _backup_script_python(tmp_path, "python-symlink-test")
    uploader = tmp_path / "unsafe-upload"
    if unsafe_kind == "directory":
        uploader.mkdir(mode=0o700)
    else:
        real_uploader = tmp_path / "real-upload"
        real_uploader.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real_uploader.chmod(0o700)
        uploader.symlink_to(real_uploader)
    environment = {
        **os.environ,
        "YINSHI_APP_ROOT": str(app_root),
        "YINSHI_PYTHON_BIN": str(python),
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_UPLOAD_COMMAND": str(uploader),
        "BACKUP_TEST_ARCHIVE": str(archive),
    }

    result = subprocess.run(
        [str(app_root / "backend" / "scripts" / "backup.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert "BACKUP_UPLOAD_COMMAND must be a trusted executable file" in result.stderr


def test_backup_script_rejects_unsafe_upload_metadata(tmp_path: Path) -> None:
    """Uploader group write permission must be rejected."""
    app_root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "yinshi-test.tar.gz.enc"
    archive.write_bytes(b"encrypted")
    python = _backup_script_python(tmp_path, "python-owner-test")
    uploader = tmp_path / "unsafe-mode-upload"
    uploader.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uploader.chmod(0o720)
    environment = {
        **os.environ,
        "YINSHI_APP_ROOT": str(app_root),
        "YINSHI_PYTHON_BIN": str(python),
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_UPLOAD_COMMAND": str(uploader),
        "BACKUP_TEST_ARCHIVE": str(archive),
    }

    result = subprocess.run(
        [str(app_root / "backend" / "scripts" / "backup.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert "BACKUP_UPLOAD_COMMAND must be a trusted executable file" in result.stderr


def test_backup_cli_supports_create_and_restore_commands(tmp_path, monkeypatch) -> None:
    """CLI should keep no-argument creation and add explicit restore commands."""
    import sys

    import yinshi.backup as backup_module

    created = tmp_path / "created.tar.gz.enc"
    restored: list[tuple[Path, bool]] = []
    monkeypatch.setattr(backup_module, "create_backup", lambda: created)
    monkeypatch.setattr(
        backup_module,
        "restore_backup",
        lambda path, *, confirm_replace=False: restored.append((path, confirm_replace)),
    )

    monkeypatch.setattr(sys, "argv", ["yinshi.backup"])
    assert backup_module._main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["yinshi.backup", "restore", str(tmp_path / "archive.enc"), "--confirm"],
    )
    assert backup_module._main() == 0
    assert restored == [(tmp_path / "archive.enc", True)]
