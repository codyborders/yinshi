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
