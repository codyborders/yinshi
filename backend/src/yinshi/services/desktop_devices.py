"""Hosted desktop device listing and account-scoped revocation."""

from __future__ import annotations

import time
from dataclasses import dataclass

from yinshi.db import get_control_db
from yinshi.services.live_auth_sessions import signal_desktop_device_revoked


@dataclass(frozen=True, slots=True)
class DesktopDevice:
    """Persisted desktop device metadata safe to return to its account owner."""

    id: str
    name: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None


def list_desktop_devices(*, user_id: str) -> list[DesktopDevice]:
    """Return every desktop device owned by one account, newest first."""
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string")
    if not user_id:
        raise ValueError("user_id must not be empty")
    with get_control_db() as database:
        rows = database.execute(
            """
            SELECT id, name, created_at, last_seen_at, revoked_at
            FROM desktop_devices
            WHERE user_id = ?
            ORDER BY created_at DESC, id ASC
            """,
            (user_id,),
        ).fetchall()
    return [
        DesktopDevice(
            id=row["id"],
            name=row["name"],
            created_at=int(row["created_at"]),
            last_seen_at=(int(row["last_seen_at"]) if row["last_seen_at"] is not None else None),
            revoked_at=(int(row["revoked_at"]) if row["revoked_at"] is not None else None),
        )
        for row in rows
    ]


def desktop_device_is_active(*, user_id: str, device_id: str) -> bool:
    """Return whether one desktop device still grants account authority."""
    for name, value in (("user_id", user_id), ("device_id", device_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if not value:
            raise ValueError(f"{name} must not be empty")
    with get_control_db() as database:
        row = database.execute(
            """
            SELECT d.revoked_at, u.status AS user_status
            FROM desktop_devices d
            JOIN users u ON u.id = d.user_id
            WHERE d.id = ? AND d.user_id = ?
            """,
            (device_id, user_id),
        ).fetchone()
    return row is not None and row["revoked_at"] is None and row["user_status"] == "active"


def revoke_desktop_device(*, user_id: str, device_id: str) -> bool:
    """Revoke one owned device and return false without exposing foreign devices."""
    for name, value in (("user_id", user_id), ("device_id", device_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if not value:
            raise ValueError(f"{name} must not be empty")

    with get_control_db() as database:
        database.execute("BEGIN IMMEDIATE")
        device = database.execute(
            "SELECT revoked_at FROM desktop_devices WHERE id = ? AND user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if device is None:
            return False
        if device["revoked_at"] is None:
            result = database.execute(
                """
                UPDATE desktop_devices
                SET revoked_at = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (int(time.time()), device_id, user_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("desktop device revocation was not stored")
            database.commit()
    signal_desktop_device_revoked(device_id)
    return True
