"""Recovery drill controller owns one asynchronous run and sanitized status."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_controller_rejects_overlap_and_publishes_result() -> None:
    """Only one drill may run while callers poll aggregate state."""
    from yinshi.managed_recovery_drill_controller import ManagedRecoveryDrillController
    from yinshi.managed_source_loss_recovery import ManagedSourceLossReceipt

    release = asyncio.Event()

    async def run_drill() -> ManagedSourceLossReceipt:
        await release.wait()
        return ManagedSourceLossReceipt(
            archive_version_count=1,
            cleanup_verified=True,
            data_verified=True,
            multipart_upload_count=0,
            replacement_authority_verified=True,
        )

    controller = ManagedRecoveryDrillController(run_drill=run_drill)
    accepted = await controller.start(commit_sha="1" * 40)

    assert accepted == {"schema_version": 1, "status": "running"}
    with pytest.raises(RuntimeError, match="active"):
        await controller.start(commit_sha="2" * 40)

    release.set()
    for _attempt in range(20):
        result = controller.status()
        if result["status"] != "running":
            break
        await asyncio.sleep(0)

    assert result["status"] == "passed"
    assert result["commit_sha"] == "1" * 40
    assert result["checks"]["cleanup_verified"] is True


@pytest.mark.asyncio
async def test_controller_blocks_new_run_after_cleanup_failure() -> None:
    """A new drill must not overwrite resource identities that need cleanup retry."""
    from yinshi.managed_recovery_drill_controller import ManagedRecoveryDrillController
    from yinshi.managed_source_loss_recovery import ManagedSourceLossReceipt

    async def failed_cleanup() -> ManagedSourceLossReceipt:
        return ManagedSourceLossReceipt(
            archive_version_count=0,
            cleanup_verified=False,
            data_verified=False,
            multipart_upload_count=0,
            replacement_authority_verified=False,
        )

    controller = ManagedRecoveryDrillController(run_drill=failed_cleanup)
    await controller.start(commit_sha="1" * 40)
    for _attempt in range(20):
        if controller.status()["status"] != "running":
            break
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cleanup"):
        await controller.start(commit_sha="2" * 40)
