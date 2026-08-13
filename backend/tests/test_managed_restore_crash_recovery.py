"""Crash-recovery tests for managed guest root replacement."""

from __future__ import annotations

from pathlib import Path

import pytest


def _create_restore_fixture(tmp_path: Path) -> tuple[object, Path, Path, Path, bytes]:
    """Create old roots plus one authenticated replacement archive."""
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
    archive_key = b"k" * 32
    guest.create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=archive_key,
        context=context,
    )
    return context, archive_path, sqlite_root, files_root, archive_key


def test_restore_syncs_staged_directories_before_live_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every staged directory must sync before the first live-root replacement."""
    import os

    import yinshi.managed_backup_guest as guest

    context, archive_path, sqlite_root, files_root, archive_key = _create_restore_fixture(tmp_path)
    original_replace = os.replace
    original_sync = guest._sync_directory
    synchronized_names: list[str] = []

    def record_sync(path: Path) -> None:
        synchronized_names.append(path.name)
        original_sync(path)

    def check_publication(source: object, target: object) -> None:
        target_path = Path(target)
        if target_path == sqlite_root:
            assert "sqlite" in synchronized_names
            assert "files" in synchronized_names
            assert "extracted" in synchronized_names
        original_replace(source, target)

    monkeypatch.setattr(guest, "_sync_directory", record_sync)
    monkeypatch.setattr(os, "replace", check_publication)

    guest.restore_managed_backup_archive(
        archive_path,
        archive_key=archive_key,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )


def test_restore_publishes_prepared_journal_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication crash must leave old roots and allow a clean retry."""
    import os

    import yinshi.managed_backup_guest as guest

    context, archive_path, sqlite_root, files_root, archive_key = _create_restore_fixture(tmp_path)
    original_replace = os.replace

    def interrupt_journal_publication(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path.name.startswith(".yinshi-restore-prepare-")
            and target_path.name == ".yinshi-restore-active"
        ):
            raise OSError("injected journal publication crash")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt_journal_publication)
    with pytest.raises(OSError, match="publication crash"):
        guest.restore_managed_backup_archive(
            archive_path,
            archive_key=archive_key,
            expected_context=context,
            sqlite_root=sqlite_root,
            files_root=files_root,
        )

    assert (sqlite_root / "value").read_text(encoding="utf-8") == "old-sqlite"
    assert (files_root / "value").read_text(encoding="utf-8") == "old-files"
    assert not (sqlite_root.parent / ".yinshi-restore-active").exists()

    monkeypatch.setattr(os, "replace", original_replace)
    guest.restore_managed_backup_archive(
        archive_path,
        archive_key=archive_key,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )

    assert (sqlite_root / "value").read_text(encoding="utf-8") == "new-sqlite"
    assert (files_root / "value").read_text(encoding="utf-8") == "new-files"


def test_restore_recovers_interrupted_journal_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup interruption must leave restored roots and permit a clean retry."""
    import shutil

    import yinshi.managed_backup_guest as guest

    context, archive_path, sqlite_root, files_root, archive_key = _create_restore_fixture(tmp_path)
    original_rmtree = shutil.rmtree
    interrupted = False

    def interrupt_cleanup(path: object, *args: object, **kwargs: object) -> None:
        nonlocal interrupted
        target = Path(path)
        if target.name == ".yinshi-restore-cleanup" and not interrupted:
            interrupted = True
            (target / "state.json").unlink()
            raise OSError("injected journal cleanup crash")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", interrupt_cleanup)
    result = guest.restore_managed_backup_archive(
        archive_path,
        archive_key=archive_key,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )

    assert result.committed is True
    assert result.cleanup_pending is True
    assert (sqlite_root / "value").read_text(encoding="utf-8") == "new-sqlite"
    assert (files_root / "value").read_text(encoding="utf-8") == "new-files"

    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    retry_result = guest.restore_managed_backup_archive(
        archive_path,
        archive_key=archive_key,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )

    assert retry_result.committed is True
    assert retry_result.cleanup_pending is False
    assert (sqlite_root / "value").read_text(encoding="utf-8") == "new-sqlite"
    assert (files_root / "value").read_text(encoding="utf-8") == "new-files"
    assert not (sqlite_root.parent / ".yinshi-restore-active").exists()
    assert not (sqlite_root.parent / ".yinshi-restore-cleanup").exists()


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
