"""Persist provider identities owned by this Yinshi deployment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from yinshi.db import get_control_db

ManagedSpriteIdentityKind = Literal["runtime", "restore_candidate"]
ManagedSpriteIdentityStatus = Literal["creating", "active", "retired", "deleting"]


@dataclass(frozen=True, slots=True)
class ManagedSpriteIdentity:
    """One deployment-owned provider identity."""

    sprite_name: str
    provider_name: str
    identity_kind: ManagedSpriteIdentityKind
    user_id: str
    job_id: str | None
    lifecycle_status: ManagedSpriteIdentityStatus
    created_at: str
    updated_at: str


def _timestamp(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).isoformat()


def register_managed_sprite_identity(
    *,
    sprite_name: str,
    identity_kind: ManagedSpriteIdentityKind,
    user_id: str,
    job_id: str | None,
    lifecycle_status: ManagedSpriteIdentityStatus,
    now: datetime,
) -> None:
    """Register one intended provider identity before external creation."""
    if not sprite_name or not user_id:
        raise ValueError("Sprite identity and owner must not be empty")
    if identity_kind == "runtime" and job_id is not None:
        raise ValueError("Runtime Sprite identity must not have a job")
    if identity_kind == "restore_candidate" and not job_id:
        raise ValueError("Restore Sprite identity must have a job")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """INSERT INTO managed_sprite_identities
               (sprite_name, provider_name, identity_kind, user_id, job_id,
                lifecycle_status, created_at, updated_at)
               VALUES (?, 'fly_sprites', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sprite_name) DO UPDATE SET
                   lifecycle_status = excluded.lifecycle_status,
                   updated_at = excluded.updated_at
               WHERE managed_sprite_identities.provider_name = 'fly_sprites'
                 AND managed_sprite_identities.identity_kind = excluded.identity_kind
                 AND managed_sprite_identities.user_id = excluded.user_id
                 AND managed_sprite_identities.job_id IS excluded.job_id""",
            (
                sprite_name,
                identity_kind,
                user_id,
                job_id,
                lifecycle_status,
                timestamp,
                timestamp,
            ),
        )
        if result.rowcount != 1:
            database.rollback()
            raise ValueError("Sprite identity conflicts with another owner")
        database.commit()


def list_managed_sprite_identities() -> tuple[ManagedSpriteIdentity, ...]:
    """Return all provider identities owned by this deployment."""
    with get_control_db() as database:
        rows = database.execute(
            """SELECT sprite_name, provider_name, identity_kind, user_id, job_id,
                      lifecycle_status, created_at, updated_at
               FROM managed_sprite_identities ORDER BY sprite_name"""
        ).fetchall()
    return tuple(
        ManagedSpriteIdentity(
            sprite_name=row["sprite_name"],
            provider_name=row["provider_name"],
            identity_kind=cast(ManagedSpriteIdentityKind, row["identity_kind"]),
            user_id=row["user_id"],
            job_id=row["job_id"],
            lifecycle_status=cast(ManagedSpriteIdentityStatus, row["lifecycle_status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    )


def remove_managed_sprite_identity(sprite_name: str) -> bool:
    """Forget one identity only after confirmed provider absence."""
    if not sprite_name:
        raise ValueError("sprite_name must not be empty")
    with get_control_db() as database:
        result = database.execute(
            "DELETE FROM managed_sprite_identities WHERE sprite_name = ?",
            (sprite_name,),
        )
        database.commit()
    return result.rowcount == 1
