"""Catalog endpoints for provider and model discovery."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from yinshi.api.deps import require_tenant
from yinshi.model_catalog import get_provider_metadata
from yinshi.models import ProviderCatalogOut
from yinshi.services.pi_config import resolve_pi_runtime
from yinshi.services.provider_connections import list_provider_connections
from yinshi.services.sidecar import create_sidecar_connection

router = APIRouter(tags=["catalog"])


def _build_provider_entry(
    provider_row: dict[str, Any],
    connected_provider_ids: set[str],
) -> dict[str, Any]:
    """Merge sidecar provider rows with Yinshi metadata."""
    provider_id = provider_row["id"]
    metadata = get_provider_metadata(provider_id)
    return {
        "id": provider_id,
        "label": metadata.label,
        "auth_strategies": list(metadata.auth_strategies),
        "setup_fields": [
            {
                "key": field.key,
                "label": field.label,
                "required": field.required,
                "secret": field.secret,
            }
            for field in metadata.setup_fields
        ],
        "docs_url": metadata.docs_url,
        "connected": provider_id in connected_provider_ids,
        "model_count": provider_row["model_count"],
    }


@router.get("/api/catalog", response_model=ProviderCatalogOut)
async def get_catalog(request: Request) -> dict[str, Any]:
    """Return the current user's provider/model catalog."""
    tenant = require_tenant(request)
    agent_dir, _settings_payload = resolve_pi_runtime(tenant.user_id, tenant.data_dir)
    connections = list_provider_connections(tenant.user_id)
    connected_provider_ids = {connection["provider"] for connection in connections}

    sidecar = await create_sidecar_connection()
    try:
        catalog = await sidecar.get_catalog(agent_dir=agent_dir)
    finally:
        await sidecar.disconnect()

    providers = [
        _build_provider_entry(provider_row, connected_provider_ids)
        for provider_row in catalog["providers"]
    ]
    return {
        "default_model": catalog["default_model"],
        "providers": providers,
        "models": catalog["models"],
    }
