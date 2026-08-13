"""Staging multipart response-loss faults target one exact archive object."""

from __future__ import annotations


def test_completion_fault_requires_exact_archive_identity() -> None:
    """A drill fault must retain the exact object and archive identity."""
    from tests.test_managed_backup_store import FakeS3Client
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    store = S3ManagedBackupStore(
        client=FakeS3Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    store.arm_lost_completion_response(
        object_key="managed/drill.enc",
        archive_id="archive-1",
    )

    assert store.lost_completion_response_target == (
        "managed/drill.enc",
        "archive-1",
    )
