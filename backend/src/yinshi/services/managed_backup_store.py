"""S3-compatible storage for encrypted managed guest backups."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_OBJECT_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PART_BYTES_MIN = 5 * 1024 * 1024
_PART_BYTES_MAX = 5 * 1024 * 1024 * 1024
_OBJECT_BYTES_MAX = 200 * 1024 * 1024 * 1024
_RECONCILIATION_PAGES_MAX = 8


def create_managed_backup_store(settings: Any) -> "S3ManagedBackupStore":
    """Build one bounded S3 backup store from validated application settings."""
    import boto3
    from botocore.config import Config

    client_options: dict[str, Any] = {
        "endpoint_url": settings.managed_backup_endpoint_url,
        "region_name": settings.managed_backup_region,
    }
    if settings.managed_backup_access_key_id is not None:
        client_options["aws_access_key_id"] = (
            settings.managed_backup_access_key_id.get_secret_value().strip()
        )
        client_options["aws_secret_access_key"] = (
            settings.managed_backup_secret_access_key.get_secret_value().strip()
        )
    client = boto3.client(
        "s3",
        **client_options,
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    return S3ManagedBackupStore(
        client=client,
        bucket=settings.managed_backup_bucket,
        server_side_encryption="AES256",
        require_bucket_default_encryption=(settings.managed_backup_provider == "aws_s3"),
        require_part_checksum_confirmation=(settings.managed_backup_provider == "aws_s3"),
        require_object_encryption_confirmation=(settings.managed_backup_provider == "aws_s3"),
        part_bytes=settings.managed_backup_part_bytes,
    )


class S3Client(Protocol):
    def create_multipart_upload(self, **request: Any) -> dict[str, Any]: ...
    def upload_part(self, **request: Any) -> dict[str, Any]: ...
    def complete_multipart_upload(self, **request: Any) -> dict[str, Any]: ...
    def abort_multipart_upload(self, **request: Any) -> dict[str, Any]: ...
    def head_object(self, **request: Any) -> dict[str, Any]: ...
    def list_object_versions(self, **request: Any) -> dict[str, Any]: ...
    def list_multipart_uploads(self, **request: Any) -> dict[str, Any]: ...
    def get_object(self, **request: Any) -> dict[str, Any]: ...
    def delete_object(self, **request: Any) -> dict[str, Any]: ...
    def get_bucket_versioning(self, **request: Any) -> dict[str, Any]: ...
    def get_bucket_encryption(self, **request: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class StoredManagedBackup:
    """Verified immutable object metadata after upload completion."""

    version: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PendingManagedBackupUploads:
    """Exact unfinished upload IDs found during read-only reconciliation."""

    upload_ids: tuple[str, ...]


class S3ManagedBackupStore:
    """Store already encrypted archives under immutable versioned object keys."""

    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        server_side_encryption: str,
        require_bucket_default_encryption: bool = True,
        require_part_checksum_confirmation: bool = True,
        require_object_encryption_confirmation: bool = True,
        part_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if _BUCKET_PATTERN.fullmatch(bucket) is None:
            raise ValueError("bucket must be a valid S3 bucket name")
        if server_side_encryption != "AES256":
            raise ValueError("server_side_encryption must be AES256")
        if type(require_bucket_default_encryption) is not bool:
            raise TypeError("require_bucket_default_encryption must be Boolean")
        if type(require_part_checksum_confirmation) is not bool:
            raise TypeError("require_part_checksum_confirmation must be Boolean")
        if type(require_object_encryption_confirmation) is not bool:
            raise TypeError("require_object_encryption_confirmation must be Boolean")
        if type(part_bytes) is not int or not _PART_BYTES_MIN <= part_bytes <= _PART_BYTES_MAX:
            raise ValueError("part_bytes is outside S3 multipart limits")
        self._client = client
        self._bucket = bucket
        self._server_side_encryption = server_side_encryption
        self._require_bucket_default_encryption = require_bucket_default_encryption
        self._require_part_checksum_confirmation = require_part_checksum_confirmation
        self._require_object_encryption_confirmation = require_object_encryption_confirmation
        self._part_bytes = part_bytes

    @staticmethod
    def _object_key(value: str) -> str:
        if _OBJECT_KEY_PATTERN.fullmatch(value) is None or ".." in value.split("/"):
            raise ValueError("object_key is invalid")
        return value

    @staticmethod
    def _expected(expected_size: int, expected_sha256: str) -> None:
        if type(expected_size) is not int or not 1 <= expected_size <= _OBJECT_BYTES_MAX:
            raise ValueError("expected_size is outside the backup object limit")
        if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
            raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")

    async def preflight(self) -> None:
        """Require enabled versioning and AES256 bucket default encryption."""
        versioning = await asyncio.to_thread(
            self._client.get_bucket_versioning,
            Bucket=self._bucket,
        )
        if versioning.get("Status") != "Enabled":
            raise RuntimeError("managed backup bucket versioning is not enabled")
        if not self._require_bucket_default_encryption:
            return
        encryption = await asyncio.to_thread(
            self._client.get_bucket_encryption,
            Bucket=self._bucket,
        )
        configuration = encryption.get("ServerSideEncryptionConfiguration")
        rules = configuration.get("Rules") if isinstance(configuration, dict) else None
        if not isinstance(rules, list) or not any(
            isinstance(rule, dict)
            and isinstance(rule.get("ApplyServerSideEncryptionByDefault"), dict)
            and rule["ApplyServerSideEncryptionByDefault"].get("SSEAlgorithm") == "AES256"
            for rule in rules
        ):
            raise RuntimeError("managed backup bucket encryption is not AES256")

    async def put_file(
        self,
        source_path: Path,
        *,
        object_key: str,
        expected_size: int,
        expected_sha256: str,
        archive_id: str,
    ) -> StoredManagedBackup:
        """Upload one file with multipart checksums and conditional completion."""
        key = self._object_key(object_key)
        self._expected(expected_size, expected_sha256)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError("source_path must be a regular file")
        if source_path.stat().st_size != expected_size:
            raise ValueError("source file size does not match expected_size")
        if not archive_id or len(archive_id) > 128:
            raise ValueError("archive object metadata is invalid")
        metadata = {
            "archive-id": archive_id,
            "format": "yinshi-managed-backup-v1",
            "sha256": expected_sha256,
        }
        created = await asyncio.to_thread(
            self._client.create_multipart_upload,
            Bucket=self._bucket,
            Key=key,
            ServerSideEncryption=self._server_side_encryption,
            ChecksumAlgorithm="SHA256",
            ContentType="application/octet-stream",
            Metadata=metadata,
        )
        upload_id = created.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise RuntimeError("backup object upload did not return an upload ID")
        completed_parts: list[dict[str, object]] = []
        digest = hashlib.sha256()
        total = 0
        try:
            with source_path.open("rb") as source:
                part_number = 1
                while chunk := await asyncio.to_thread(source.read, self._part_bytes):
                    total += len(chunk)
                    digest.update(chunk)
                    checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
                    uploaded = await asyncio.to_thread(
                        self._client.upload_part,
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                        ContentLength=len(chunk),
                        ChecksumSHA256=checksum,
                    )
                    if not isinstance(uploaded.get("ETag"), str):
                        raise RuntimeError("backup object part ETag was not returned")
                    if (
                        self._require_part_checksum_confirmation
                        and uploaded.get("ChecksumSHA256") != checksum
                    ):
                        raise RuntimeError("backup object part checksum was not confirmed")
                    completed_parts.append(
                        {
                            "ETag": uploaded["ETag"],
                            "PartNumber": part_number,
                            "ChecksumSHA256": checksum,
                        }
                    )
                    part_number += 1
            if total != expected_size or digest.hexdigest() != expected_sha256:
                raise RuntimeError("backup object source checksum did not match")
            completed = await asyncio.to_thread(
                self._client.complete_multipart_upload,
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                IfNoneMatch="*",
                MultipartUpload={"Parts": completed_parts},
                ChecksumType="COMPOSITE",
            )
        except BaseException:
            await asyncio.to_thread(
                self._client.abort_multipart_upload,
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )
            raise
        version = completed.get("VersionId")
        if not isinstance(version, str) or not version:
            raise RuntimeError("backup object upload did not return a version")
        head = await asyncio.to_thread(
            self._client.head_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=version,
        )
        if (
            head.get("ContentLength") != expected_size
            or head.get("Metadata") != metadata
            or (
                self._require_object_encryption_confirmation
                and head.get("ServerSideEncryption") != self._server_side_encryption
            )
            or head.get("VersionId") != version
        ):
            await self._delete_invalid_version(key, version)
            raise RuntimeError("backup object metadata validation failed")
        if not self._require_object_encryption_confirmation:
            try:
                await self._verify_remote_digest(
                    key=key,
                    version=version,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
            except RuntimeError:
                await self._delete_invalid_version(key, version)
                raise
        return StoredManagedBackup(
            version=version, size_bytes=expected_size, sha256=expected_sha256
        )

    async def _delete_invalid_version(self, key: str, version: str) -> None:
        """Remove one uploaded version that failed validation."""
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=version,
        )

    async def _verify_remote_digest(
        self,
        *,
        key: str,
        version: str,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        """Confirm exact remote ciphertext when provider metadata is insufficient."""
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=version,
        )
        body = response.get("Body")
        read = getattr(body, "read", None)
        close = getattr(body, "close", None)
        try:
            if response.get("ContentLength") != expected_size or not callable(read):
                raise RuntimeError("backup object checksum could not be confirmed")
            digest = hashlib.sha256()
            total = 0
            while chunk := await asyncio.to_thread(read, self._part_bytes):
                total += len(chunk)
                digest.update(chunk)
        finally:
            if callable(close):
                close()
        if total != expected_size or digest.hexdigest() != expected_sha256:
            raise RuntimeError("backup object checksum did not match")

    async def reconcile_upload(
        self,
        *,
        object_key: str,
        archive_id: str,
        expected_size: int,
        expected_sha256: str,
    ) -> StoredManagedBackup | PendingManagedBackupUploads | None:
        """Recover one exact version, pending uploads, or confirmed absence."""
        key = self._object_key(object_key)
        self._expected(expected_size, expected_sha256)
        if not isinstance(archive_id, str) or not archive_id or len(archive_id) > 128:
            raise ValueError("archive_id must be bounded non-empty text")
        matching_versions, matching_markers = await self._list_exact_versions(key)
        if not matching_versions and not matching_markers:
            matching_uploads = await self._list_exact_uploads(key)
            if matching_uploads:
                return PendingManagedBackupUploads(tuple(matching_uploads))
            matching_versions, matching_markers = await self._list_exact_versions(key)
            if not matching_versions and not matching_markers:
                matching_uploads = await self._list_exact_uploads(key)
                if matching_uploads:
                    return PendingManagedBackupUploads(tuple(matching_uploads))
                matching_versions, matching_markers = await self._list_exact_versions(key)
                if not matching_versions and not matching_markers:
                    return None
        if len(matching_versions) != 1 or matching_markers:
            raise RuntimeError("backup upload reconciliation was ambiguous")
        version = matching_versions[0]
        head = await asyncio.to_thread(
            self._client.head_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=version,
        )
        metadata = head.get("Metadata")
        size_bytes = head.get("ContentLength")
        if (
            size_bytes != expected_size
            or not isinstance(metadata, dict)
            or metadata.get("archive-id") != archive_id
            or metadata.get("format") != "yinshi-managed-backup-v1"
            or metadata.get("sha256") != expected_sha256
            or (
                self._require_object_encryption_confirmation
                and head.get("ServerSideEncryption") != self._server_side_encryption
            )
            or head.get("VersionId") != version
        ):
            raise RuntimeError("backup upload reconciliation validation failed")
        if not self._require_object_encryption_confirmation:
            await self._verify_remote_digest(
                key=key,
                version=version,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        return StoredManagedBackup(
            version=version,
            size_bytes=size_bytes,
            sha256=expected_sha256,
        )

    async def _list_exact_versions(self, key: str) -> tuple[list[str], list[dict[str, Any]]]:
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": key,
            "MaxKeys": 3,
        }
        matching_versions: list[str] = []
        matching_markers: list[dict[str, Any]] = []
        for _page in range(_RECONCILIATION_PAGES_MAX):
            response = await asyncio.to_thread(
                self._client.list_object_versions,
                **request,
            )
            versions = response.get("Versions")
            markers = response.get("DeleteMarkers")
            version_entries = versions if isinstance(versions, list) else []
            marker_entries = markers if isinstance(markers, list) else []
            matching_versions.extend(
                str(version["VersionId"])
                for version in version_entries
                if isinstance(version, dict)
                and version.get("Key") == key
                and isinstance(version.get("VersionId"), str)
                and version.get("VersionId")
            )
            matching_markers.extend(
                marker
                for marker in marker_entries
                if isinstance(marker, dict) and marker.get("Key") == key
            )
            returned_keys = [
                str(entry["Key"])
                for entry in (*version_entries, *marker_entries)
                if isinstance(entry, dict) and isinstance(entry.get("Key"), str)
            ]
            if any(returned_key > key for returned_key in returned_keys):
                return matching_versions, matching_markers
            if response.get("IsTruncated") is not True:
                return matching_versions, matching_markers
            next_key = response.get("NextKeyMarker")
            next_version = response.get("NextVersionIdMarker")
            if next_key != key or not isinstance(next_version, str) or not next_version:
                raise RuntimeError("backup upload reconciliation was ambiguous")
            request["KeyMarker"] = next_key
            request["VersionIdMarker"] = next_version
        raise RuntimeError("backup upload reconciliation was ambiguous")

    async def _list_exact_uploads(self, key: str) -> list[str]:
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": key,
            "MaxUploads": 2,
        }
        matching_uploads: list[str] = []
        for _page in range(_RECONCILIATION_PAGES_MAX):
            response = await asyncio.to_thread(
                self._client.list_multipart_uploads,
                **request,
            )
            uploads = response.get("Uploads")
            upload_entries = uploads if isinstance(uploads, list) else []
            matching_uploads.extend(
                str(upload["UploadId"])
                for upload in upload_entries
                if isinstance(upload, dict)
                and upload.get("Key") == key
                and isinstance(upload.get("UploadId"), str)
                and upload.get("UploadId")
            )
            returned_keys = [
                str(upload["Key"])
                for upload in upload_entries
                if isinstance(upload, dict) and isinstance(upload.get("Key"), str)
            ]
            if any(returned_key > key for returned_key in returned_keys):
                return matching_uploads
            if response.get("IsTruncated") is not True:
                return matching_uploads
            next_key = response.get("NextKeyMarker")
            next_upload = response.get("NextUploadIdMarker")
            if next_key != key or not isinstance(next_upload, str) or not next_upload:
                raise RuntimeError("backup upload reconciliation was ambiguous")
            request["KeyMarker"] = next_key
            request["UploadIdMarker"] = next_upload
        raise RuntimeError("backup upload reconciliation was ambiguous")

    async def abort_uploads(
        self,
        *,
        object_key: str,
        upload_ids: tuple[str, ...],
    ) -> None:
        """Abort a bounded set of exact multipart upload IDs."""
        key = self._object_key(object_key)
        if (
            not isinstance(upload_ids, tuple)
            or not upload_ids
            or len(upload_ids) > _RECONCILIATION_PAGES_MAX * 2
            or len(set(upload_ids)) != len(upload_ids)
        ):
            raise ValueError("upload_ids must be a bounded unique tuple")
        if any(
            not isinstance(upload_id, str) or not upload_id or len(upload_id) > 2048
            for upload_id in upload_ids
        ):
            raise ValueError("upload_ids contain invalid text")
        for upload_id in upload_ids:
            await asyncio.to_thread(
                self._client.abort_multipart_upload,
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )

    async def delete_file(
        self,
        *,
        object_key: str,
        object_version: str,
    ) -> None:
        """Delete one exact immutable object version."""
        key = self._object_key(object_key)
        if not isinstance(object_version, str) or not object_version or len(object_version) > 1024:
            raise ValueError("object_version must be bounded non-empty text")
        deleted = await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=object_version,
        )
        if isinstance(deleted, dict) and deleted.get("VersionId") == object_version:
            return
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
                VersionId=object_version,
            )
        except Exception as error:
            response = getattr(error, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
                return
            raise RuntimeError("backup object version deletion could not be confirmed") from None
        raise RuntimeError("backup object version deletion was not confirmed")

    async def get_file(
        self,
        *,
        object_key: str,
        object_version: str,
        target_path: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> StoredManagedBackup:
        """Download one exact object version and validate its ciphertext digest."""
        key = self._object_key(object_key)
        self._expected(expected_size, expected_sha256)
        if not object_version or target_path.exists() or target_path.is_symlink():
            raise ValueError("object version or target path is invalid")
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
            VersionId=object_version,
        )
        if response.get("ContentLength") != expected_size:
            raise RuntimeError("backup object size did not match")
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise RuntimeError("backup object body is unavailable")
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                while chunk := await asyncio.to_thread(body.read, self._part_bytes):
                    total += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            target_path.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if total != expected_size or digest.hexdigest() != expected_sha256:
            target_path.unlink(missing_ok=True)
            raise RuntimeError("backup object checksum did not match")
        return StoredManagedBackup(
            version=object_version,
            size_bytes=expected_size,
            sha256=expected_sha256,
        )
