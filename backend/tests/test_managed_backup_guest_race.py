"""Regression test for managed backup file replacement races."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_create_archive_does_not_follow_a_file_replaced_after_metadata_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive creation should reject replacement links before reading content."""
    from yinshi.managed_backup_guest import ManagedArchiveContext, create_managed_backup_archive

    sqlite_root = tmp_path / "state" / "sqlite"
    files_root = tmp_path / "state" / "files"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    candidate = files_root / "workspace.txt"
    candidate.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-leak", encoding="utf-8")
    original_stat = Path.stat
    replaced = False

    def replace_after_metadata(path: Path, *args, **kwargs):
        nonlocal replaced
        metadata = original_stat(path, *args, **kwargs)
        if path == candidate and not replaced:
            replaced = True
            candidate.unlink()
            os.symlink(outside, candidate)
        return metadata

    monkeypatch.setattr(Path, "stat", replace_after_metadata)
    context = ManagedArchiveContext(
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        created_at="2026-08-12T12:00:00+00:00",
        owner_digest="f" * 64,
        runtime_generation=7,
    )

    with pytest.raises((OSError, ValueError)):
        create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=tmp_path / "archive.enc",
            archive_key=b"p" * 32,
            context=context,
        )
    assert not (tmp_path / "archive.enc").exists()
