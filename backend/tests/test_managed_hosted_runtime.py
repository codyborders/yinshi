"""Hosted managed composition owns runtime and provider resources."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_hosted_composition_closes_runtime_before_provider() -> None:
    """One composition owner must close managed resources in required order."""
    from yinshi.services.managed_hosted_runtime import HostedManagedRuntime

    events: list[str] = []

    class Runtime:
        async def aclose(self) -> None:
            events.append("runtime")

    class ProviderClient:
        async def aclose(self) -> None:
            events.append("provider")

    provider = object()
    composition = HostedManagedRuntime(
        runtime_manager=Runtime(),
        backup_provider=provider,
        inventory_provider=provider,
        provider_http_client=ProviderClient(),
    )

    await composition.aclose()

    assert events == ["runtime", "provider"]
