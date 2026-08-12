"""Crash-recovery tests for managed guest root replacement."""

from __future__ import annotations

from pathlib import Path


def test_restore_recovers_interrupted_root_replacement(tmp_path: Path) -> None:
    """A retry must recover both roots after interruption between replacements."""
    import json
    import os

    import yinshi.managed_backup_guest as guest

    state_root = tmp_path / "state"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    (sqlite_root / "value").write_text("old-sqlite", encoding="utf-8")
    (files_root / "value").write_text("old-files", encoding="utf-8")

    source_root = tmp_path / "source"
    source_sqlite = source_root / "sqlite"
    source_files = source_root / "files"
    source_sqlite.mkdir(parents=True)
    source_files.mkdir()
    (source_sqlite / "value").write_text("new-sqlite", encoding="utf-8")
    (source_files / "value").write_text("new-files", encoding="utf-8")
    context = guest.ManagedArchiveContext(
        archive_id="archive-1",
        created_at="2026-08-12T12:00:00Z",
        owner_digest="a" * 64,
        runtime_generation=1,
    )
    archive_path = tmp_path / "archive.enc"
    guest.create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=b"k" * 32,
        context=context,
    )

    transaction_root = state_root / ".yinshi-restore-active"
    rollback_root = transaction_root / "rollback"
    rollback_root.mkdir(parents=True)
    os.replace(sqlite_root, rollback_root / "sqlite")
    (transaction_root / "state.json").write_text(
        json.dumps({"phase": "old_sqlite_moved", "version": 1}),
        encoding="utf-8",
    )

    guest.restore_managed_backup_archive(
        archive_path,
        archive_key=b"k" * 32,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )

    assert (sqlite_root / "value").read_text(encoding="utf-8") == "new-sqlite"
    assert (files_root / "value").read_text(encoding="utf-8") == "new-files"
    assert not transaction_root.exists()
