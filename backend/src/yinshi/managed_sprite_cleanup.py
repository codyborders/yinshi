"""Guarded operator command for managed Sprite cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx

from yinshi.config import get_settings
from yinshi.services.managed_sprite_reconciliation import (
    ManagedSpriteReconciler,
    ManagedSpriteReconciliationResult,
)
from yinshi.services.sprites import SpritesClient


def _parser() -> argparse.ArgumentParser:
    """Build cleanup arguments without reading settings."""
    parser = argparse.ArgumentParser(prog="yinshi-managed-sprite-cleanup")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--confirm-delete-unreferenced-managed-sprites",
        action="store_true",
    )
    return parser


async def _run_cleanup(*, execute: bool) -> ManagedSpriteReconciliationResult:
    """Build validated provider dependencies and run one reconciliation."""
    settings = get_settings()
    api_token = settings.sprites_api_token
    name_key = settings.sprites_name_key
    if settings.managed_runtime_provider != "fly_sprites" or api_token is None or name_key is None:
        raise RuntimeError("Managed Sprite cleanup is unavailable")
    restore_name_prefix = f"{settings.sprites_name_prefix}-restore"[:30].rstrip("-")
    async with httpx.AsyncClient(
        base_url=settings.sprites_api_url,
        follow_redirects=False,
    ) as http_client:
        provider = SpritesClient(
            api_token=api_token.get_secret_value(),
            http_client=http_client,
        )
        reconciler = ManagedSpriteReconciler(
            provider=provider,
            name_prefix=settings.sprites_name_prefix,
            restore_name_prefix=restore_name_prefix,
            restore_name_key=name_key.get_secret_value(),
            grace=timedelta(seconds=settings.sprites_reconcile_grace_seconds),
        )
        return await reconciler.reconcile_once(dry_run=not execute)


def _result_payload(
    result: ManagedSpriteReconciliationResult,
    *,
    execute: bool,
) -> dict[str, Any]:
    """Return count-only output for one successful command."""
    return {
        "deleted": len(result.deleted),
        "deferred": result.deferred,
        "eligible": len(result.eligible),
        "examined": result.examined,
        "mode": "execute" if execute else "dry-run",
        "retained": result.retained,
        "status": "ok",
    }


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one dry or explicitly confirmed cleanup pass."""
    parser = _parser()
    options = parser.parse_args(arguments)
    if options.execute != options.confirm_delete_unreferenced_managed_sprites:
        parser.error(
            "--execute and --confirm-delete-unreferenced-managed-sprites must be used together"
        )
    execute = bool(options.execute)
    try:
        result = asyncio.run(_run_cleanup(execute=execute))
    except Exception:
        print(json.dumps({"status": "failed"}, separators=(",", ":"), sort_keys=True))
        return 1
    print(
        json.dumps(
            _result_payload(result, execute=execute),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
