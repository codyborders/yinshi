"""Managed backup storage purges every exact drill-owned artifact."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_purge_object_deletes_versions_markers_and_uploads() -> None:
    """One exact key purge should remove every retained provider artifact."""
    from yinshi.services.managed_backup_store import S3ManagedBackupStore

    deleted: list[str] = []
    aborted: list[str] = []

    class Client:
        def list_object_versions(self, **_request):
            return {
                "IsTruncated": False,
                "Versions": [{"Key": "managed/drill.enc", "VersionId": "v1"}],
                "DeleteMarkers": [{"Key": "managed/drill.enc", "VersionId": "m1"}],
            }

        def list_multipart_uploads(self, **_request):
            return {
                "IsTruncated": False,
                "Uploads": [{"Key": "managed/drill.enc", "UploadId": "u1"}],
            }

        def delete_object(self, **request):
            deleted.append(request["VersionId"])
            return {}

        def abort_multipart_upload(self, **request):
            aborted.append(request["UploadId"])
            return {}

    store = S3ManagedBackupStore(
        client=Client(),
        bucket="backup-bucket",
        server_side_encryption="AES256",
        part_bytes=5 * 1024 * 1024,
    )

    await store.purge_object(object_key="managed/drill.enc")

    assert deleted == ["v1", "m1"]
    assert aborted == ["u1"]
