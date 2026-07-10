"""Encrypted backup tests verify confidentiality and restorable database content."""

import sqlite3
import tarfile

from tests.conftest import _configure_test_env


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
