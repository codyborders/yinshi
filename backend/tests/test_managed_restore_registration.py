"""Tests for private replacement runner registration identity."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def test_restore_candidate_registration_confirms_fresh_noise_identity(auth_client) -> None:
    """Internal replacement registration should confirm its new Noise key."""
    from yinshi.services.runners import (
        create_runner_registration,
        get_managed_restore_runner_for_user,
        register_runner,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_runner_registration(
        tenant.user_id,
        name="Managed restore candidate",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="fly_sprites_posix",
        control_url="https://control.example",
        runner_kind="managed_restore",
    )
    public_key = X25519PrivateKey.generate().public_key().public_bytes_raw()
    public_key_text = base64.urlsafe_b64encode(public_key).rstrip(b"=").decode("ascii")

    register_runner(
        registration["registration_token"],
        runner_version="runner-v1",
        capabilities={
            "artifact_sha256": "a" * 64,
            "sqlite_storage": "local_posix",
            "shared_files_storage": "local_posix",
        },
        data_dir="/var/lib/yinshi",
        sqlite_dir="/var/lib/yinshi/sqlite",
        shared_files_dir="/var/lib/yinshi/files",
        storage_profile="fly_sprites_posix",
        noise_public_key=public_key_text,
    )

    candidate = get_managed_restore_runner_for_user(tenant.user_id)
    assert candidate is not None
    assert candidate["noise_public_key"] == public_key_text
    assert candidate["noise_key_confirmed"] is True


def test_restore_candidate_revocation_clears_all_bearer_authority(auth_client) -> None:
    """Failed replacement cleanup should revoke every candidate credential."""
    from yinshi.db import get_control_db
    from yinshi.services.runners import (
        create_runner_registration,
        get_managed_restore_runner_for_user,
        revoke_managed_restore_runner_for_user,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    create_runner_registration(
        tenant.user_id,
        name="Managed restore candidate",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="fly_sprites_posix",
        control_url="https://control.example",
        runner_kind="managed_restore",
    )
    with get_control_db() as database:
        database.execute(
            """UPDATE user_runners
               SET status = 'online', runner_token_hash = 'candidate-token',
                   registered_at = '2026-08-12T12:00:00Z',
                   last_heartbeat_at = '2026-08-12T12:00:00Z'
               WHERE user_id = ? AND kind = 'managed_restore'""",
            (tenant.user_id,),
        )
        database.commit()

    assert revoke_managed_restore_runner_for_user(tenant.user_id)
    candidate = get_managed_restore_runner_for_user(tenant.user_id)
    assert candidate is not None
    assert candidate["status"] == "revoked"
    with get_control_db() as database:
        stored = database.execute(
            """SELECT registration_token_hash, registration_token_expires_at,
                      runner_token_hash, revoked_at
               FROM user_runners WHERE user_id = ? AND kind = 'managed_restore'""",
            (tenant.user_id,),
        ).fetchone()
    assert stored is not None
    assert stored["registration_token_hash"] is None
    assert stored["registration_token_expires_at"] is None
    assert stored["runner_token_hash"] is None
    assert stored["revoked_at"] is not None
