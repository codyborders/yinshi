"""Live recovery runner performs destructive steps and always cleans resources."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_live_runner_returns_aggregate_checks_after_cleanup() -> None:
    """The runner should validate recovery without returning sensitive identifiers."""
    from yinshi.managed_recovery_live import ManagedRecoveryLiveRunner

    calls: list[str] = []

    class Boundary:
        async def provision(self) -> None:
            calls.append("provision")

        async def write_fixtures(self) -> None:
            calls.append("write")

        async def backup_with_lost_completion(self) -> None:
            calls.append("backup")

        async def delete_source(self) -> None:
            calls.append("delete-source")

        async def restore(self) -> None:
            calls.append("restore")

        async def verify(self) -> tuple[int, int, bool, bool]:
            calls.append("verify")
            return 1, 0, True, True

        async def cleanup(self) -> bool:
            calls.append("cleanup")
            return True

    receipt = await ManagedRecoveryLiveRunner(boundary=Boundary()).run()

    assert calls == [
        "provision",
        "write",
        "backup",
        "delete-source",
        "restore",
        "verify",
        "cleanup",
    ]
    assert receipt.archive_version_count == 1
    assert receipt.multipart_upload_count == 0
    assert receipt.data_verified is True
    assert receipt.replacement_authority_verified is True
    assert receipt.cleanup_verified is True
