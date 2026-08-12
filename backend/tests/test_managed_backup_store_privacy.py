"""Supplemental managed backup store boundary tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def test_store_part_limit_matches_s3_contract() -> None:
    """Multipart configuration should reject parts above the S3 maximum."""
    import pytest

    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    with pytest.raises(ValueError, match="part_bytes"):
        S3ManagedBackupStore(
            client=object(),
            bucket="backup-bucket",
            server_side_encryption="AES256",
            part_bytes=5 * 1024**3 + 1,
        )


@pytest.mark.asyncio
async def test_store_metadata_omits_tenant_linkable_owner_digest(tmp_path: Path) -> None:
    """Object metadata must not expose stable tenant-linkable digests."""
    from tests.test_managed_backup_store import FakeS3Client
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    source = tmp_path / "archive.enc"
    source.write_bytes(payload)
    client = FakeS3Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.put_file(
        source,
        object_key="managed/v1/random/archive.enc",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        archive_id="archive-1",
    )

    assert "owner-digest" not in client.metadata
