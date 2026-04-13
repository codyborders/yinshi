"""Account provisioning and resolution for multi-tenant users."""

import json
import logging
import os
import secrets
import sqlite3
from typing import Any

from yinshi.config import get_settings
from yinshi.db import get_control_db
from yinshi.services.crypto import generate_dek, wrap_dek
from yinshi.tenant import TenantContext, get_user_db, init_user_db, user_data_dir

logger = logging.getLogger(__name__)


def make_tenant(user_id: str, email: str) -> TenantContext:
    """Build a TenantContext from user_id and email."""
    settings = get_settings()
    data_dir = user_data_dir(settings.user_data_dir, user_id)
    return TenantContext(
        user_id=user_id,
        email=email,
        data_dir=data_dir,
        db_path=os.path.join(data_dir, "yinshi.db"),
    )


def provision_user(user_id: str, email: str) -> TenantContext:
    """Create the data directory and initialize the user's database."""
    tenant = make_tenant(user_id, email)
    repos_dir = os.path.join(tenant.data_dir, "repos")
    os.makedirs(repos_dir, exist_ok=True)
    init_user_db(tenant.db_path)

    logger.info("Provisioned user %s at %s", user_id, tenant.data_dir)
    return tenant


def _ensure_tenant_provisioned(user_id: str, email: str) -> TenantContext:
    """Return one tenant context backed by an initialized user database."""
    tenant = make_tenant(user_id, email)
    if os.path.isfile(tenant.db_path):
        return tenant
    return provision_user(user_id, email)


def _migrate_legacy_data(tenant: TenantContext) -> None:
    """Copy repos/workspaces/sessions/messages from the legacy DB to the user's DB.

    Runs once on first login. Skips silently if no legacy DB exists or if the
    user has no data in it.
    """
    settings = get_settings()
    legacy_path = settings.db_path
    if not os.path.exists(legacy_path):
        return

    try:
        source = sqlite3.connect(legacy_path)
        source.row_factory = sqlite3.Row
    except sqlite3.Error:
        logger.warning("Could not open legacy DB at %s", legacy_path)
        return

    try:
        repos = source.execute(
            "SELECT * FROM repos WHERE owner_email = ?",
            (tenant.email,),
        ).fetchall()

        if not repos:
            return

        with get_user_db(tenant) as dest:
            for repo in repos:
                r = dict(repo)
                dest.execute(
                    (
                        "INSERT OR IGNORE INTO repos "
                        "(id, created_at, updated_at, name, remote_url, root_path, custom_prompt, agents_md, installation_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        r["id"],
                        r["created_at"],
                        r["updated_at"],
                        r["name"],
                        r["remote_url"],
                        r["root_path"],
                        r.get("custom_prompt"),
                        r.get("agents_md"),
                        r.get("installation_id"),
                    ),
                )

                for ws in source.execute(
                    "SELECT * FROM workspaces WHERE repo_id = ?", (r["id"],)
                ).fetchall():
                    w = dict(ws)
                    dest.execute(
                        (
                            "INSERT OR IGNORE INTO workspaces "
                            "(id, created_at, updated_at, repo_id, name, branch, path, state) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                        ),
                        (
                            w["id"],
                            w["created_at"],
                            w["updated_at"],
                            w["repo_id"],
                            w["name"],
                            w.get("branch", ""),
                            w.get("path", ""),
                            w["state"],
                        ),
                    )

                    for sess in source.execute(
                        "SELECT * FROM sessions WHERE workspace_id = ?", (w["id"],)
                    ).fetchall():
                        s = dict(sess)
                        dest.execute(
                            (
                                "INSERT OR IGNORE INTO sessions "
                                "(id, created_at, updated_at, workspace_id, status, model) "
                                "VALUES (?, ?, ?, ?, ?, ?)"
                            ),
                            (
                                s["id"],
                                s["created_at"],
                                s["updated_at"],
                                s["workspace_id"],
                                s["status"],
                                s.get("model"),
                            ),
                        )

                        for msg in source.execute(
                            "SELECT * FROM messages WHERE session_id = ?", (s["id"],)
                        ).fetchall():
                            m = dict(msg)
                            dest.execute(
                                (
                                    "INSERT OR IGNORE INTO messages "
                                    "(id, created_at, session_id, role, "
                                    "content, full_message, turn_id, turn_status) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                                ),
                                (
                                    m["id"],
                                    m["created_at"],
                                    m["session_id"],
                                    m["role"],
                                    m["content"],
                                    m.get("full_message"),
                                    m.get("turn_id"),
                                    m.get("turn_status"),
                                ),
                            )

            dest.commit()
            logger.info("Migrated %d legacy repo(s) for %s", len(repos), tenant.email)
    except sqlite3.Error:
        logger.exception("Failed to migrate legacy data for %s", tenant.email)
    finally:
        source.close()


def _touch_last_login(db: sqlite3.Connection, user_id: str) -> None:
    """Update last_login_at for an existing user."""
    db.execute(
        "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user_id,),
    )


def _find_user_by_identity(
    db: sqlite3.Connection,
    provider: str,
    provider_user_id: str,
) -> sqlite3.Row | None:
    """Return one user row for one OAuth identity, or None when missing."""
    return db.execute(
        "SELECT oi.user_id, u.email FROM oauth_identities oi "
        "JOIN users u ON oi.user_id = u.id "
        "WHERE oi.provider = ? AND oi.provider_user_id = ?",
        (provider, provider_user_id),
    ).fetchone()


def _find_user_by_email(
    db: sqlite3.Connection,
    email: str,
) -> sqlite3.Row | None:
    """Return one user row for one email, or None when missing."""
    return db.execute(
        "SELECT id, email FROM users WHERE email = ?",
        (email,),
    ).fetchone()


def _insert_oauth_identity(
    db: sqlite3.Connection,
    *,
    user_id: str,
    provider: str,
    provider_user_id: str,
    email: str,
    provider_data_json: str | None,
) -> None:
    """Insert one OAuth identity, tolerating concurrent duplicate inserts."""
    try:
        db.execute(
            "INSERT INTO oauth_identities "
            "(user_id, provider, provider_user_id, provider_email, provider_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, provider, provider_user_id, email, provider_data_json),
        )
    except sqlite3.IntegrityError as error:
        existing_identity_row = _find_user_by_identity(db, provider, provider_user_id)
        if existing_identity_row is None:
            raise
        if existing_identity_row["user_id"] != user_id:
            raise RuntimeError("OAuth identity already belongs to a different user") from error


def _create_user_record(
    db: sqlite3.Connection,
    *,
    email: str,
    display_name: str | None,
    avatar_url: str | None,
) -> tuple[str, bool]:
    """Insert one user row or load the concurrently created row by email."""
    candidate_user_id = secrets.token_hex(16)
    dek = generate_dek()
    settings = get_settings()
    pepper = settings.encryption_pepper_bytes
    encrypted_dek = wrap_dek(dek, candidate_user_id, pepper) if pepper else None

    try:
        db.execute(
            "INSERT INTO users (id, email, display_name, avatar_url, encrypted_dek, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (candidate_user_id, email, display_name, avatar_url, encrypted_dek),
        )
    except sqlite3.IntegrityError:
        existing_user_row = _find_user_by_email(db, email)
        if existing_user_row is None:
            raise
        return existing_user_row["id"], False

    return candidate_user_id, True


def resolve_or_create_user(
    provider: str,
    provider_user_id: str,
    email: str,
    display_name: str | None = None,
    avatar_url: str | None = None,
    provider_data: dict[str, Any] | None = None,
) -> TenantContext:
    """Resolve an existing user or create a new one.

    1. Look up oauth_identities by (provider, provider_user_id) -- return if found
    2. Look up users by email -- link new identity if found
    3. Otherwise, provision a new user
    """
    provider_data_json = json.dumps(provider_data) if provider_data is not None else None

    with get_control_db() as db:
        # 1. Check existing identity
        row = _find_user_by_identity(db, provider, provider_user_id)

        if row:
            _touch_last_login(db, row["user_id"])
            db.commit()
            return _ensure_tenant_provisioned(row["user_id"], row["email"])

        # 2. Check existing user by email
        user_row = _find_user_by_email(db, email)

        if user_row:
            user_id = user_row["id"]
            _insert_oauth_identity(
                db,
                user_id=user_id,
                provider=provider,
                provider_user_id=provider_user_id,
                email=email,
                provider_data_json=provider_data_json,
            )
            _touch_last_login(db, user_id)
            db.commit()
            return _ensure_tenant_provisioned(user_id, email)

        # 3. Create new user
        user_id, user_was_created = _create_user_record(
            db,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        _insert_oauth_identity(
            db,
            user_id=user_id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
            provider_data_json=provider_data_json,
        )
        if not user_was_created:
            _touch_last_login(db, user_id)
        db.commit()

    tenant = _ensure_tenant_provisioned(user_id, email)
    if not user_was_created:
        return tenant

    # Provision outside the control DB transaction.
    _migrate_legacy_data(tenant)
    return tenant
