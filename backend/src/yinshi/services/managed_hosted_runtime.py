"""Compose hosted managed runtime capabilities and owned resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HostedManagedRuntime:
    """Own hosted runtime services and narrow provider capabilities."""

    runtime_manager: Any
    backup_provider: Any
    inventory_provider: Any
    provider_http_client: Any

    async def aclose(self) -> None:
        """Close runtime work before its provider transport."""
        error: BaseException | None = None
        try:
            await self.runtime_manager.aclose()
        except BaseException as caught:
            error = caught
        try:
            await self.provider_http_client.aclose()
        except BaseException as caught:
            if error is None:
                error = caught
        if error is not None:
            raise error
