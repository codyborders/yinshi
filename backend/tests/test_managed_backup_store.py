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

    def list_multipart_uploads(self, **request):
        return {"IsTruncated": False, "Uploads": []}

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
async def test_s3_store_recovers_exact_version_after_lost_completion_response() -> None:
    """Reconciliation should recover one fully validated completed object version."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def list_object_versions(self, **request):
            assert request == {
                "Bucket": "backup-bucket",
                "Prefix": "managed/v1/owner/archive.enc",
                "MaxKeys": 3,
            }
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "IsLatest": True,
                        "Key": "managed/v1/owner/archive.enc",
                        "VersionId": "version-1",
                    }
                ],
            }

    client = Client()
    client.object = payload
    client.metadata = {
        "archive-id": "archive-1",
        "format": "yinshi-managed-backup-v1",
        "sha256": digest,
    }
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is not None
    assert stored.version == "version-1"
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == digest


@pytest.mark.asyncio
async def test_s3_store_rejects_reconciled_metadata_mismatch() -> None:
    """Recovered object metadata must match the trusted guest result exactly."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    expected_digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def list_object_versions(self, **request):
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "IsLatest": True,
                        "Key": "managed/v1/owner/archive.enc",
                        "VersionId": "version-1",
                    }
                ],
            }

    client = Client()
    client.object = payload + b"wrong"
    client.metadata = {
        "archive-id": "archive-1",
        "format": "yinshi-managed-backup-v1",
        "sha256": hashlib.sha256(client.object).hexdigest(),
    }
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="validation failed"):
        await store.reconcile_upload(
            object_key="managed/v1/owner/archive.enc",
            archive_id="archive-1",
            expected_size=len(payload),
            expected_sha256=expected_digest,
        )


@pytest.mark.asyncio
async def test_s3_store_reports_confirmed_absence_for_upload_retry() -> None:
    """Reconciliation should distinguish no published version from ambiguity."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def list_object_versions(self, **request):
            return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is None


@pytest.mark.asyncio
async def test_s3_store_rechecks_versions_after_multipart_completion_race() -> None:
    """Reconciliation must detect a version that settles during multipart inspection."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.version_checks = 0
            self.object = payload
            self.metadata = {
                "archive-id": "archive-1",
                "format": "yinshi-managed-backup-v1",
                "sha256": digest,
            }

        def list_object_versions(self, **request):
            self.version_checks += 1
            if self.version_checks == 1:
                return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "IsLatest": True,
                        "Key": "managed/v1/owner/archive.enc",
                        "VersionId": "version-1",
                    }
                ],
            }

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is not None
    assert stored.version == "version-1"
    assert client.version_checks == 2


@pytest.mark.asyncio
async def test_s3_store_detects_completion_after_final_multipart_check() -> None:
    """Final absence requires one version check after the final multipart check."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.version_checks = 0
            self.object = payload
            self.metadata = {
                "archive-id": "archive-1",
                "format": "yinshi-managed-backup-v1",
                "sha256": digest,
            }

        def list_object_versions(self, **request):
            self.version_checks += 1
            if self.version_checks < 3:
                return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "IsLatest": True,
                        "Key": "managed/v1/owner/archive.enc",
                        "VersionId": "version-1",
                    }
                ],
            }

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is not None
    assert stored.version == "version-1"
    assert client.version_checks == 3


@pytest.mark.asyncio
async def test_s3_store_reports_pending_upload_without_deleting_it() -> None:
    """Storage inspection must not delete an unfinished upload without lease authority."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.uploads = [
                {
                    "Key": "managed/v1/owner/archive.enc",
                    "UploadId": "pending-upload",
                }
            ]
            self.aborted_upload_ids: list[str] = []

        def list_object_versions(self, **request):
            return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}

        def list_multipart_uploads(self, **request):
            return {"IsTruncated": False, "Uploads": list(self.uploads)}

        def abort_multipart_upload(self, **request):
            self.aborted_upload_ids.append(request["UploadId"])

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    pending = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert getattr(pending, "upload_ids", ()) == ("pending-upload",)
    assert client.aborted_upload_ids == []


@pytest.mark.asyncio
async def test_s3_store_aborts_only_supplied_exact_upload_ids() -> None:
    """Authorized cleanup should delete only upload IDs selected by the lease owner."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.aborted_upload_ids: list[str] = []

        def abort_multipart_upload(self, **request):
            self.aborted_upload_ids.append(request["UploadId"])

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.abort_uploads(
        object_key="managed/v1/owner/archive.enc",
        upload_ids=("old-upload-1", "old-upload-2"),
    )

    assert client.aborted_upload_ids == ["old-upload-1", "old-upload-2"]


@pytest.mark.asyncio
async def test_s3_store_aborts_exact_incomplete_upload_before_reporting_absence() -> None:
    """A retry requires no published version and no unfinished multipart upload."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.uploads = [
                {
                    "Key": "managed/v1/owner/archive.enc",
                    "UploadId": "upload-stale",
                }
            ]
            self.aborted_upload_ids: list[str] = []

        def list_object_versions(self, **request):
            return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}

        def list_multipart_uploads(self, **request):
            return {"IsTruncated": False, "Uploads": list(self.uploads)}

        def abort_multipart_upload(self, **request):
            self.aborted_upload_ids.append(request["UploadId"])
            self.uploads.clear()

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    pending = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )
    upload_ids = getattr(pending, "upload_ids", ())
    await store.abort_uploads(
        object_key="managed/v1/owner/archive.enc",
        upload_ids=upload_ids,
    )
    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is None
    assert client.aborted_upload_ids == ["upload-stale"]


@pytest.mark.asyncio
async def test_s3_store_aborts_all_exact_unfinished_uploads() -> None:
    """Reconciliation should remove every bounded unfinished upload for one key."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.uploads = [
                {"Key": "managed/v1/owner/archive.enc", "UploadId": "upload-1"},
                {"Key": "managed/v1/owner/archive.enc", "UploadId": "upload-2"},
            ]
            self.aborted_upload_ids: list[str] = []

        def list_object_versions(self, **request):
            return {"DeleteMarkers": [], "IsTruncated": False, "Versions": []}

        def list_multipart_uploads(self, **request):
            return {"IsTruncated": False, "Uploads": list(self.uploads)}

        def abort_multipart_upload(self, **request):
            upload_id = request["UploadId"]
            self.aborted_upload_ids.append(upload_id)
            self.uploads = [upload for upload in self.uploads if upload["UploadId"] != upload_id]

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    pending = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )
    upload_ids = getattr(pending, "upload_ids", ())
    await store.abort_uploads(
        object_key="managed/v1/owner/archive.enc",
        upload_ids=upload_ids,
    )
    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is None
    assert client.aborted_upload_ids == ["upload-1", "upload-2"]


@pytest.mark.asyncio
async def test_s3_store_recovers_exact_version_across_prefix_neighbor_pages() -> None:
    """Longer keys sharing the prefix must not block exact-version recovery."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.object = payload
            self.metadata = {
                "archive-id": "archive-1",
                "format": "yinshi-managed-backup-v1",
                "sha256": digest,
            }
            self.requests: list[dict[str, object]] = []

        def list_object_versions(self, **request):
            self.requests.append(request)
            if "KeyMarker" not in request:
                return {
                    "DeleteMarkers": [],
                    "IsTruncated": True,
                    "NextKeyMarker": "managed/v1/owner/archive.enc",
                    "NextVersionIdMarker": "version-1",
                    "Versions": [
                        {
                            "Key": "managed/v1/owner/archive.enc",
                            "VersionId": "version-1",
                        }
                    ],
                }
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "Key": "managed/v1/owner/archive.enc.sibling",
                        "VersionId": "sibling-version",
                    }
                ],
            }

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is not None
    assert stored.version == "version-1"
    assert client.requests[1]["KeyMarker"] == "managed/v1/owner/archive.enc"
    assert client.requests[1]["VersionIdMarker"] == "version-1"


@pytest.mark.asyncio
async def test_s3_store_aborts_exact_upload_across_prefix_neighbor_pages() -> None:
    """Multipart pagination should affect only unfinished uploads for the exact key."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.aborted_upload_ids: list[str] = []
            self.upload_page = 0

        def list_object_versions(self, **request):
            return {
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Versions": [
                    {
                        "Key": "managed/v1/owner/archive.enc.sibling",
                        "VersionId": "sibling-version",
                    }
                ],
            }

        def list_multipart_uploads(self, **request):
            self.upload_page += 1
            if self.upload_page == 1:
                return {
                    "IsTruncated": True,
                    "NextKeyMarker": "managed/v1/owner/archive.enc",
                    "NextUploadIdMarker": "upload-1",
                    "Uploads": [
                        {
                            "Key": "managed/v1/owner/archive.enc",
                            "UploadId": "upload-1",
                        }
                    ],
                }
            return {
                "IsTruncated": False,
                "Uploads": [
                    {
                        "Key": "managed/v1/owner/archive.enc.sibling",
                        "UploadId": "sibling-upload",
                    }
                ],
            }

        def abort_multipart_upload(self, **request):
            self.aborted_upload_ids.append(request["UploadId"])

    client = Client()
    store = S3ManagedBackupStore(
        client=client,
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    pending = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )
    upload_ids = getattr(pending, "upload_ids", ())
    await store.abort_uploads(
        object_key="managed/v1/owner/archive.enc",
        upload_ids=upload_ids,
    )
    stored = await store.reconcile_upload(
        object_key="managed/v1/owner/archive.enc",
        archive_id="archive-1",
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert stored is None
    assert client.aborted_upload_ids == ["upload-1"]


@pytest.mark.asyncio
async def test_s3_store_rejects_paginated_reconciliation_results() -> None:
    """Bounded reconciliation must fail closed when matching results may be omitted."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    payload = b"encrypted managed archive"
    digest = hashlib.sha256(payload).hexdigest()

    class Client(FakeS3Client):
        def list_object_versions(self, **request):
            return {"DeleteMarkers": [], "IsTruncated": True, "Versions": []}

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        await store.reconcile_upload(
            object_key="managed/v1/owner/archive.enc",
            archive_id="archive-1",
            expected_size=len(payload),
            expected_sha256=digest,
        )


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
