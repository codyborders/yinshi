"""Staging startup recovers retained drill resources before accepting work."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_startup_fails_when_retained_cleanup_cannot_complete(monkeypatch) -> None:
    """A restart must not bypass cleanup failure fencing."""
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary

    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=object(),
        backup_manager=object(),
        provider=object(),
        store=object(),
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.list_retained_managed_recovery_tenants",
        lambda: ("old-drill",),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="retained cleanup"):
        await boundary.recover_retained_cleanup()
