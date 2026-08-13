"""Destructive staging recovery runner with guaranteed cleanup."""

from __future__ import annotations

from typing import Protocol

from yinshi.managed_source_loss_recovery import ManagedSourceLossReceipt


class ManagedRecoveryLiveBoundary(Protocol):
    """Narrow live capabilities required by one destructive drill."""

    async def provision(self) -> None: ...
    async def write_fixtures(self) -> None: ...
    async def backup_with_lost_completion(self) -> None: ...
    async def delete_source(self) -> None: ...
    async def restore(self) -> None: ...
    async def verify(self) -> tuple[int, int, bool, bool]: ...
    async def cleanup(self) -> bool: ...


class ManagedRecoveryLiveRunner:
    """Run one complete source-loss drill and emit aggregate checks only."""

    def __init__(self, *, boundary: ManagedRecoveryLiveBoundary) -> None:
        self._boundary = boundary

    async def run(self) -> ManagedSourceLossReceipt:
        """Execute destructive recovery and require cleanup before success."""
        try:
            await self._boundary.provision()
            await self._boundary.write_fixtures()
            await self._boundary.backup_with_lost_completion()
            await self._boundary.delete_source()
            await self._boundary.restore()
            (
                archive_version_count,
                multipart_upload_count,
                data_verified,
                replacement_authority_verified,
            ) = await self._boundary.verify()
        finally:
            cleanup_verified = await self._boundary.cleanup()
        return ManagedSourceLossReceipt(
            archive_version_count=archive_version_count,
            cleanup_verified=cleanup_verified,
            data_verified=data_verified,
            multipart_upload_count=multipart_upload_count,
            replacement_authority_verified=replacement_authority_verified,
        )
