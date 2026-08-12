"""Exercise atomic managed runtime lifecycle state."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest

from yinshi.db import get_control_db


def _expected_name(prefix: str, key: str, user_id: str) -> str:
    digest = hmac.new(key.encode(), user_id.encode(), hashlib.sha256).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}-{encoded[:32]}"


def test_managed_sprite_name_is_deterministic_non_pii() -> None:
    """Sprite names use bounded lowercase base32 HMAC output."""
    from yinshi.services.managed_runners import managed_sprite_name

    first = managed_sprite_name("private-user-id", prefix="yinshi", secret_key="name-key")
    second = managed_sprite_name("private-user-id", prefix="yinshi", secret_key="name-key")

    assert first == second == _expected_name("yinshi", "name-key", "private-user-id")
    assert "private-user-id" not in first
    assert len(first) == 39
    assert first.islower()
    assert (
        managed_sprite_name("private-user-id", prefix="yinshi", secret_key="rotated-key") != first
    )


def test_absent_runtime_status_is_none(auth_client) -> None:
    """Status lookup returns None when no managed runtime exists."""
    from yinshi.services.managed_runners import get_managed_runtime_status

    tenant = getattr(auth_client, "yinshi_tenant")

    assert get_managed_runtime_status(tenant.user_id) is None


def test_absent_runtime_deletion_claim_is_none(auth_client) -> None:
    """Deletion claim returns None when no managed runtime exists."""
    from yinshi.services.managed_runners import claim_managed_runtime_deletion

    tenant = getattr(auth_client, "yinshi_tenant")

    assert (
        claim_managed_runtime_deletion(
            tenant.user_id,
            datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        )
        is None
    )


def test_runtime_status_returns_typed_state_without_authority(auth_client) -> None:
    """Status lookup exposes typed state but no registration authority."""
    from yinshi.services.managed_runners import (
        ManagedRuntimeStatus,
        claim_managed_runtime_provisioning,
        get_managed_runtime_status,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
    )

    status = get_managed_runtime_status(tenant.user_id)

    assert isinstance(status, ManagedRuntimeStatus)
    assert status.lifecycle_status == "provisioning"
    assert not hasattr(status, "registration_token")


def _provisioning_runtime(auth_client, now: datetime):
    """Create one claim and populate its linked runner with valid readiness facts."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            """
            UPDATE user_runners
            SET status = 'online', last_heartbeat_at = ?,
                capabilities_json = '{"storage_profile":"fly_sprites_posix"}',
                noise_public_key = ?, noise_public_key_confirmed_at = ?
            WHERE id = ?
            """,
            (now.isoformat(), "a" * 43, now.isoformat(), claim.runtime.runner_id),
        )
        database.execute(
            "UPDATE managed_runtimes SET last_error = 'bootstrap_failed' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    return tenant, claim


def test_deletion_claim_and_matching_finalize_preserve_byoc(auth_client) -> None:
    """Deletion claim and matching finalization preserve the BYOC runner."""
    from yinshi.services.managed_runners import (
        DeletionClaimResult,
        claim_managed_runtime_deletion,
        finalize_managed_runtime_deletion,
    )
    from yinshi.services.runners import create_runner_registration

    started_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    deleted_at = datetime(2026, 4, 28, 12, 1, tzinfo=timezone.utc)
    tenant, provisioning_claim = _provisioning_runtime(auth_client, started_at)
    create_runner_registration(
        tenant.user_id,
        name="My BYOC runner",
        cloud_provider="aws",
        region="us-east-1",
        storage_profile="aws_ebs_s3_files",
        control_url="https://control.example",
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE user_runners SET runner_token_hash = 'managed-bearer' WHERE id = ?",
            (provisioning_claim.runtime.runner_id,),
        )
        database.commit()
        byoc_before = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'byoc'",
                (tenant.user_id,),
            ).fetchone()
        )

    claim = claim_managed_runtime_deletion(tenant.user_id, deleted_at)

    assert isinstance(claim, DeletionClaimResult)
    assert claim.claimed is True
    assert claim.runtime.lifecycle_status == "deleting"
    assert claim.runtime.generation == provisioning_claim.runtime.generation + 1
    assert claim.runtime.last_error is None
    assert claim.runtime.updated_at == "2026-04-28T12:01:00Z"
    with get_control_db() as database:
        managed = database.execute(
            "SELECT * FROM user_runners WHERE id = ?",
            (provisioning_claim.runtime.runner_id,),
        ).fetchone()
        byoc_after = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'byoc'",
                (tenant.user_id,),
            ).fetchone()
        )
    assert managed is not None
    assert managed["status"] == "revoked"
    assert managed["revoked_at"] == "2026-04-28T12:01:00Z"
    assert managed["registration_token_hash"] is None
    assert managed["registration_token_expires_at"] is None
    assert managed["runner_token_hash"] is None
    assert byoc_after == byoc_before

    finalized = finalize_managed_runtime_deletion(
        tenant.user_id,
        claim.runtime.generation,
    )

    with get_control_db() as database:
        runtime_after_finalize = database.execute(
            "SELECT * FROM managed_runtimes WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
        managed_after_finalize = database.execute(
            "SELECT * FROM user_runners WHERE id = ?",
            (provisioning_claim.runtime.runner_id,),
        ).fetchone()
        byoc_after_finalize = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'byoc'",
                (tenant.user_id,),
            ).fetchone()
        )
    assert finalized is True
    assert runtime_after_finalize is None
    assert managed_after_finalize is None
    assert byoc_after_finalize == byoc_before


def test_deleting_runtime_returns_unclaimed_without_mutation(auth_client) -> None:
    """Repeated deletion claim returns current state without changing records."""
    from yinshi.services.managed_runners import claim_managed_runtime_deletion

    started_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant, provisioning_claim = _provisioning_runtime(auth_client, started_at)
    first_claim = claim_managed_runtime_deletion(
        tenant.user_id,
        started_at + timedelta(minutes=1),
    )
    assert first_claim is not None
    with get_control_db() as database:
        runtime_before = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_before = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (provisioning_claim.runtime.runner_id,),
            ).fetchone()
        )

    repeated_claim = claim_managed_runtime_deletion(
        tenant.user_id,
        started_at + timedelta(minutes=2),
    )

    with get_control_db() as database:
        runtime_after = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_after = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (provisioning_claim.runtime.runner_id,),
            ).fetchone()
        )
    assert repeated_claim is not None
    assert repeated_claim.claimed is False
    assert repeated_claim.runtime == first_claim.runtime
    assert runtime_after == runtime_before
    assert runner_after == runner_before


def test_matching_provisioning_runtime_becomes_ready(auth_client) -> None:
    """Ready completion stores ready only after every runner fact is valid."""
    from yinshi.services.managed_runners import (
        get_managed_runtime_status,
        mark_managed_runtime_ready,
    )

    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant, claim = _provisioning_runtime(auth_client, now)

    changed = mark_managed_runtime_ready(tenant.user_id, claim.runtime.generation, now)

    status = get_managed_runtime_status(tenant.user_id)
    assert changed is True
    assert status is not None
    assert status.lifecycle_status == "ready"
    assert status.last_error is None
    assert status.updated_at == "2026-04-28T12:00:00Z"


def test_future_heartbeat_cannot_mark_runtime_ready(auth_client) -> None:
    """A heartbeat after the supplied clock is not current readiness."""
    from yinshi.services.managed_runners import (
        get_managed_runtime_status,
        mark_managed_runtime_ready,
    )

    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant, claim = _provisioning_runtime(auth_client, now)
    with get_control_db() as database:
        database.execute(
            "UPDATE user_runners SET last_heartbeat_at = ? WHERE id = ?",
            ((now + timedelta(seconds=1)).isoformat(), claim.runtime.runner_id),
        )
        database.commit()

    changed = mark_managed_runtime_ready(tenant.user_id, claim.runtime.generation, now)

    status = get_managed_runtime_status(tenant.user_id)
    assert changed is False
    assert status is not None
    assert status.lifecycle_status == "provisioning"
    assert status.last_error == "bootstrap_failed"


def test_stale_generation_does_not_change_lifecycle(auth_client) -> None:
    """Completions for an older generation leave current state untouched."""
    from yinshi.services.managed_runners import (
        get_managed_runtime_status,
        mark_managed_runtime_ready,
        refresh_managed_runtime_provisioning,
    )

    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant, claim = _provisioning_runtime(auth_client, now)
    refreshed_at = now + timedelta(minutes=1)
    assert (
        refresh_managed_runtime_provisioning(
            tenant.user_id,
            claim.runtime.generation,
            refreshed_at,
        )
        is True
    )
    with get_control_db() as database:
        database.execute(
            """
            INSERT OR REPLACE INTO managed_runtimes (
                user_id, runner_id, provider_name, sprite_external_id,
                lifecycle_status, generation, artifact_version,
                created_at, updated_at, last_error
            )
            SELECT user_id, runner_id, provider_name, sprite_external_id,
                   lifecycle_status, 2, artifact_version,
                   created_at, updated_at, last_error
            FROM managed_runtimes WHERE user_id = ?
            """,
            (tenant.user_id,),
        )
        database.commit()

    changed = mark_managed_runtime_ready(tenant.user_id, claim.runtime.generation, now)
    refreshed = refresh_managed_runtime_provisioning(
        tenant.user_id,
        claim.runtime.generation,
        refreshed_at + timedelta(minutes=1),
    )

    status = get_managed_runtime_status(tenant.user_id)
    assert changed is False
    assert refreshed is False
    assert status is not None
    assert status.generation == 2
    assert status.lifecycle_status == "provisioning"
    assert status.last_error == "bootstrap_failed"
    assert status.updated_at == "2026-04-28T12:01:00Z"


def test_absent_runtime_claims_generation_one_atomically(auth_client) -> None:
    """First claim creates linked registration and runtime in one transaction."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )

    assert claim.claimed is True
    assert claim.runtime.lifecycle_status == "provisioning"
    assert claim.runtime.generation == 1
    assert claim.registration_token
    assert claim.registration_token not in repr(claim.runtime)
    with get_control_db() as database:
        runtime = database.execute(
            "SELECT * FROM managed_runtimes WHERE user_id = ?", (tenant.user_id,)
        ).fetchone()
        runner = database.execute(
            "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'managed'",
            (tenant.user_id,),
        ).fetchone()
    assert runtime is not None
    assert runner is not None
    assert runtime["runner_id"] == runner["id"]
    assert runner["registration_token_hash"] != claim.registration_token


@pytest.mark.parametrize(
    "error_code",
    [
        "artifact_invalid",
        "provider_unavailable",
        "network_policy_failed",
        "bootstrap_failed",
        "runner_registration_failed",
        "runner_identity_changed",
        "wake_timeout",
        "checkpoint_failed",
        "delete_failed",
    ],
)
def test_matching_provisioning_runtime_becomes_failed_and_revokes_runner(
    auth_client,
    error_code: str,
) -> None:
    """Failure completion stores its code and removes managed runner authority."""
    from yinshi.services.managed_runners import (
        get_managed_runtime_status,
        mark_managed_runtime_failed,
    )

    started_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    failed_at = datetime(2026, 4, 28, 12, 1, tzinfo=timezone.utc)
    tenant, claim = _provisioning_runtime(auth_client, started_at)
    with get_control_db() as database:
        database.execute(
            """
            UPDATE user_runners
            SET runner_token_hash = 'runner-bearer-hash', registered_at = ?
            WHERE id = ?
            """,
            (started_at.isoformat(), claim.runtime.runner_id),
        )
        database.commit()

    changed = mark_managed_runtime_failed(
        tenant.user_id,
        claim.runtime.generation,
        error_code,
        failed_at,
    )

    status = get_managed_runtime_status(tenant.user_id)
    with get_control_db() as database:
        runner = database.execute(
            "SELECT * FROM user_runners WHERE id = ?",
            (claim.runtime.runner_id,),
        ).fetchone()
    assert changed is True
    assert status is not None
    assert status.lifecycle_status == "failed"
    assert status.last_error == error_code
    assert status.updated_at == "2026-04-28T12:01:00Z"
    assert runner is not None
    assert runner["status"] == "revoked"
    assert runner["revoked_at"] == "2026-04-28T12:01:00Z"
    assert runner["registration_token_hash"] is None
    assert runner["registration_token_expires_at"] is None
    assert runner["runner_token_hash"] is None


def test_stale_failure_generation_returns_false_without_mutation(auth_client) -> None:
    """Failure completion for an old generation changes no runtime or runner fields."""
    from yinshi.services.managed_runners import mark_managed_runtime_failed

    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant, claim = _provisioning_runtime(auth_client, now)
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET generation = 2 WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
        runtime_before = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_before = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (claim.runtime.runner_id,),
            ).fetchone()
        )

    changed = mark_managed_runtime_failed(
        tenant.user_id,
        claim.runtime.generation,
        "bootstrap_failed",
        now + timedelta(minutes=1),
    )

    with get_control_db() as database:
        runtime_after = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_after = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (claim.runtime.runner_id,),
            ).fetchone()
        )
    assert changed is False
    assert runtime_after == runtime_before
    assert runner_after == runner_before

    from yinshi.services.managed_runners import reconcile_managed_runtime_provisioning

    reconciled = reconcile_managed_runtime_provisioning(
        set(),
        now + timedelta(minutes=2),
    )

    with get_control_db() as database:
        runtime_after_reconcile = database.execute(
            "SELECT * FROM managed_runtimes WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
        runner_after_reconcile = database.execute(
            "SELECT * FROM user_runners WHERE id = ?",
            (claim.runtime.runner_id,),
        ).fetchone()
    assert reconciled == 1
    assert runtime_after_reconcile is not None
    assert runtime_after_reconcile["lifecycle_status"] == "failed"
    assert runtime_after_reconcile["generation"] == 2
    assert runtime_after_reconcile["last_error"] == "provider_unavailable"
    assert runner_after_reconcile is not None
    assert runner_after_reconcile["status"] == "revoked"
    assert runner_after_reconcile["registration_token_hash"] is None
    assert runner_after_reconcile["registration_token_expires_at"] is None
    assert runner_after_reconcile["runner_token_hash"] is None


@pytest.mark.parametrize("error_code", ["not_allowed", "x" * 1001])
def test_failure_code_is_rejected_before_database_access(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    """Unknown codes and long messages fail before opening the database."""
    from yinshi.services import managed_runners

    tenant = getattr(auth_client, "yinshi_tenant")

    def fail_database_access() -> None:
        raise AssertionError("database access must not occur")

    monkeypatch.setattr(managed_runners, "get_control_db", fail_database_access)

    with pytest.raises(ValueError, match="error_code"):
        managed_runners.mark_managed_runtime_failed(
            tenant.user_id,
            1,
            error_code,
            datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        )


def test_ready_runtime_with_different_artifact_observes_by_default(auth_client) -> None:
    """A ready runtime does not rotate authority for an implicit upgrade."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    ready_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant = getattr(auth_client, "yinshi_tenant")
    initial_claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=ready_at,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
        runtime_before = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_before = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (initial_claim.runtime.runner_id,),
            ).fetchone()
        )

    observed = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v2",
        region="ord",
        control_url="https://control.example",
        now=ready_at + timedelta(minutes=1),
    )

    with get_control_db() as database:
        runtime_after = dict(
            database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (tenant.user_id,),
            ).fetchone()
        )
        runner_after = dict(
            database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                (initial_claim.runtime.runner_id,),
            ).fetchone()
        )
    assert observed.claimed is False
    assert observed.registration_token is None
    assert observed.runtime.lifecycle_status == "ready"
    assert observed.runtime.artifact_version == "worker-v1"
    assert runtime_after == runtime_before
    assert runner_after == runner_before


def test_ready_runtime_upgrade_claims_next_generation_with_same_sprite(auth_client) -> None:
    """An allowed artifact upgrade keeps the provider Sprite name."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    ready_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant = getattr(auth_client, "yinshi_tenant")
    initial_claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=ready_at,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()

    upgraded = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="rotated",
        name_key="rotated-name-key",
        artifact_version="worker-v2",
        region="ord",
        control_url="https://control.example",
        allow_upgrade=True,
        now=ready_at + timedelta(minutes=1),
    )

    assert upgraded.claimed is True
    assert upgraded.registration_token
    assert upgraded.runtime.lifecycle_status == "provisioning"
    assert upgraded.runtime.generation == initial_claim.runtime.generation + 1
    assert upgraded.runtime.artifact_version == "worker-v2"
    assert upgraded.runtime.sprite_name == initial_claim.runtime.sprite_name


@pytest.mark.parametrize("allow_upgrade", [0, 1, None, "true"])
def test_allow_upgrade_requires_exact_bool_before_database_access(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
    allow_upgrade: object,
) -> None:
    """Non-boolean upgrade values fail before opening the database."""
    from yinshi.services import managed_runners

    tenant = getattr(auth_client, "yinshi_tenant")

    def fail_database_access() -> None:
        raise AssertionError("database access must not occur")

    monkeypatch.setattr(managed_runners, "get_control_db", fail_database_access)

    with pytest.raises(TypeError, match="allow_upgrade"):
        managed_runners.claim_managed_runtime_provisioning(
            tenant.user_id,
            name_prefix="yinshi",
            name_key="secret-name-key",
            artifact_version="worker-v1",
            region="ord",
            control_url="https://control.example",
            allow_upgrade=allow_upgrade,  # type: ignore[arg-type]
            now=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        )


def test_ready_runtime_with_matching_artifact_always_observes(auth_client) -> None:
    """Upgrade permission does not rotate a matching ready runtime."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    ready_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    tenant = getattr(auth_client, "yinshi_tenant")
    initial_claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        now=ready_at,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()

    observed = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="worker-v1",
        region="ord",
        control_url="https://control.example",
        allow_upgrade=True,
        now=ready_at + timedelta(minutes=1),
    )

    assert observed.claimed is False
    assert observed.registration_token is None
    assert observed.runtime.lifecycle_status == "ready"
    assert observed.runtime.generation == initial_claim.runtime.generation
    assert observed.runtime.runner_id == initial_claim.runtime.runner_id
