"""Tests for encrypted managed backup object storage."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest


class FakeS3Client:
    """Store multipart objects in memory through the S3 client method contract."""

    def __init__(self) -> None:
        self.parts: dict[int, bytes] = {}
        self.object = b""
        self.metadata: dict[str, str] = {}
        self.aborted = False

    def create_multipart_upload(self, **request):
        self.metadata = request["Metadata"]
        assert request["ServerSideEncryption"] == "AES256"
        assert request["ChecksumAlgorithm"] == "SHA256"
        return {"UploadId": "upload-1"}

    def upload_part(self, **request):
        body = request["Body"]
        assert isinstance(body, bytes)
        self.parts[request["PartNumber"]] = body
        return {
            "ETag": f'etag-{request["PartNumber"]}',
            "ChecksumSHA256": request["ChecksumSHA256"],
        }

    def complete_multipart_upload(self, **request):
        assert request["IfNoneMatch"] == "*"
        self.object = b"".join(self.parts[index] for index in sorted(self.parts))
        return {"VersionId": "version-1"}

    def abort_multipart_upload(self, **request):
        self.aborted = True

    def head_object(self, **request):
        assert request["VersionId"] == "version-1"
        return {
            "ContentLength": len(self.object),
            "Metadata": self.metadata,
            "ServerSideEncryption": "AES256",
            "VersionId": "version-1",
        }

    def get_object(self, **request):
        assert request["VersionId"] == "version-1"
        return {"Body": io.BytesIO(self.object), "ContentLength": len(self.object)}

    def delete_object(self, **request):
        assert request["VersionId"] == "version-1"
        self.object = b""
        return {}


@pytest.mark.asyncio
async def test_s3_store_roundtrips_a_versioned_encrypted_object(tmp_path: Path) -> None:
    """Object storage should preserve exact ciphertext and immutable metadata."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = bytes(range(251)) * 50_000
    source = tmp_path / "archive.enc"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    client = FakeS3Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.put_file(
        source,
        object_key="managed/v1/owner/archive.enc",
        expected_size=len(payload),
        expected_sha256=digest,
        archive_id="archive-1",
    )
    restored = tmp_path / "restored.enc"
    await store.get_file(
        object_key="managed/v1/owner/archive.enc",
        object_version=stored.version,
        target_path=restored,
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored.version == "version-1"
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == digest
    assert restored.read_bytes() == payload
    assert client.metadata == {
        "archive-id": "archive-1",
        "format": "yinshi-managed-backup-v1",
        "sha256": digest,
    }
    assert not client.aborted


@pytest.mark.asyncio
async def test_s3_store_removes_completed_object_when_validation_fails(
    tmp_path: Path,
) -> None:
    """A completed version should not survive failed immutable metadata checks."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def head_object(self, **request):
            head = super().head_object(**request)
            head["Metadata"] = {"format": "wrong"}
            return head

    payload = b"encrypted managed archive"
    source = tmp_path / "archive.enc"
    source.write_bytes(payload)
    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="metadata validation"):
        await store.put_file(
            source,
            object_key="managed/v1/owner/archive.enc",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            archive_id="archive-1",
        )

    assert client.object == b""


@pytest.mark.asyncio
async def test_s3_store_confirms_exact_version_absence_after_delete() -> None:
    """Deletion should succeed only after the requested immutable version is absent."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.deleted = False

        def delete_object(self, **request):
            self.deleted = True
            return {}

        def head_object(self, **request):
            if self.deleted:
                error = RuntimeError("missing version")
                error.response = {"Error": {"Code": "NoSuchVersion"}}  # type: ignore[attr-defined]
                raise error
            return super().head_object(**request)

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.delete_file(
        object_key="managed/v1/owner/archive.enc",
        object_version="version-1",
    )

    assert client.deleted


@pytest.mark.asyncio
async def test_s3_store_rejects_unconfirmed_exact_version_deletion() -> None:
    """Deletion must fail closed when the requested version still exists."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def delete_object(self, **request):
            return {}

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="version deletion was not confirmed"):
        await store.delete_file(
            object_key="managed/v1/owner/archive.enc",
            object_version="version-1",
        )


@pytest.mark.asyncio
async def test_s3_store_deletes_only_the_exact_object_version() -> None:
    """Archive deletion should never issue an unversioned object request."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def delete_object(self, **request):
            assert request["VersionId"] == "version-1"
            self.object = b""
            return {"VersionId": "version-1"}

    client = Client()
    client.object = b"ciphertext"
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.delete_file(
        object_key="managed/v1/owner/archive.enc",
        object_version="version-1",
    )

    assert client.object == b""


@pytest.mark.asyncio
async def test_s3_store_preflight_requires_bucket_versioning_and_encryption() -> None:
    """Startup preflight should reject storage without independent version history."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def get_bucket_versioning(self, **request):
            return {"Status": "Suspended"}

        def get_bucket_encryption(self, **request):
            return {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="versioning"):
        await store.preflight()


@pytest.mark.asyncio
async def test_s3_store_preflight_rejects_missing_bucket_encryption() -> None:
    """Startup preflight should reject a versioned bucket without AES256 defaults."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def get_bucket_versioning(self, **request):
            return {"Status": "Enabled"}

        def get_bucket_encryption(self, **request):
            return {"ServerSideEncryptionConfiguration": {"Rules": []}}

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="encryption"):
        await store.preflight()


@pytest.mark.asyncio
async def test_s3_store_preflight_accepts_enabled_versioned_encrypted_bucket() -> None:
    """Startup preflight should accept enabled versioning with AES256 defaults."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def get_bucket_versioning(self, **request):
            return {"Status": "Enabled"}

        def get_bucket_encryption(self, **request):
            return {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.preflight()
