"""Permanent managed Sprite cleanup command behavior tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciliationResult


def test_cleanup_defaults_to_dry_run_and_prints_counts_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default command must inspect eligible objects without mutation."""
    from types import SimpleNamespace

    from pydantic import SecretStr

    import yinshi.managed_sprite_cleanup as cleanup

    expected = ManagedSpriteReconciliationResult(
        examined=4,
        retained=2,
        deleted=(),
        deferred=1,
        eligible=("private-sprite-name",),
    )
    settings = SimpleNamespace(
        managed_runtime_provider="fly_sprites",
        sprites_api_token=SecretStr("provider-token"),
        sprites_name_key=SecretStr("name-key"),
        sprites_api_url="https://api.sprites.dev/v1",
        sprites_name_prefix="yinshi",
        sprites_reconcile_grace_seconds=3600,
    )

    class FakeHttpClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeHttpClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

    class FakeProvider:
        def __init__(self, *, api_token: str, http_client: object) -> None:
            assert api_token == "provider-token"
            assert isinstance(http_client, FakeHttpClient)

    class FakeReconciler:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def reconcile_once(
            self, *, dry_run: bool = False
        ) -> ManagedSpriteReconciliationResult:
            assert dry_run is True
            return expected

    monkeypatch.setattr(cleanup, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        cleanup,
        "httpx",
        SimpleNamespace(AsyncClient=FakeHttpClient),
        raising=False,
    )
    monkeypatch.setattr(cleanup, "SpritesClient", FakeProvider, raising=False)
    monkeypatch.setattr(cleanup, "ManagedSpriteReconciler", FakeReconciler, raising=False)

    status = cleanup.main([])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {
        "deleted": 0,
        "deferred": 1,
        "eligible": 1,
        "examined": 4,
        "mode": "dry-run",
        "retained": 2,
        "status": "ok",
    }


def test_cleanup_rejects_execute_without_confirmation_before_provider_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Execution requires the exact paired confirmation flag."""
    import yinshi.managed_sprite_cleanup as cleanup

    run_cleanup = AsyncMock()
    monkeypatch.setattr(cleanup, "_run_cleanup", run_cleanup)

    with pytest.raises(SystemExit) as error:
        cleanup.main(["--execute"])

    assert error.value.code == 2
    assert "must be used together" in capsys.readouterr().err
    run_cleanup.assert_not_awaited()


def test_cleanup_executes_only_with_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The paired flags run one guarded destructive reconciliation."""
    import yinshi.managed_sprite_cleanup as cleanup

    run_cleanup = AsyncMock(
        return_value=ManagedSpriteReconciliationResult(
            examined=3,
            retained=1,
            deleted=("private-sprite-name",),
            deferred=1,
        )
    )
    monkeypatch.setattr(cleanup, "_run_cleanup", run_cleanup)

    status = cleanup.main(["--execute", "--confirm-delete-unreferenced-managed-sprites"])

    assert status == 0
    run_cleanup.assert_awaited_once_with(execute=True)
    assert json.loads(capsys.readouterr().out) == {
        "deleted": 1,
        "deferred": 1,
        "eligible": 0,
        "examined": 3,
        "mode": "execute",
        "retained": 1,
        "status": "ok",
    }


def test_cleanup_sanitizes_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configuration and provider failures must not expose internal details."""
    import yinshi.managed_sprite_cleanup as cleanup

    run_cleanup = AsyncMock(side_effect=RuntimeError("token and provider response"))
    monkeypatch.setattr(cleanup, "_run_cleanup", run_cleanup)

    status = cleanup.main([])

    assert status == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}
