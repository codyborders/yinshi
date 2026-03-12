#!/usr/bin/env python3
"""One-time migration from single-DB to multi-tenant architecture.

Reads all distinct owner_email values from the current yinshi.db,
creates a user for each in the control DB, provisions their data
directory, and copies their repos/workspaces/sessions/messages
into per-user databases.

Usage:
    cd backend
    source venv/bin/activate
    python scripts/migrate_to_multitenant.py [--dry-run]

Environment:
    Reads from .env (DB_PATH, CONTROL_DB_PATH, USER_DATA_DIR, ENCRYPTION_PEPPER)
"""

import argparse
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys

# Add src to path so we can import yinshi modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yinshi.config import get_settings
from yinshi.db import init_control_db, get_control_db
from yinshi.services.accounts import provision_user, resolve_or_create_user
from yinshi.tenant import get_user_db, TenantContext, user_data_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def migrate(dry_run: bool = False) -> None:
    settings = get_settings()
    source_db_path = settings.db_path

    if not os.path.exists(source_db_path):
        logger.error("Source database not found: %s", source_db_path)
        sys.exit(1)

    logger.info("Source database: %s", source_db_path)
    logger.info("Control database: %s", settings.control_db_path)
    logger.info("User data directory: %s", settings.user_data_dir)

    if not dry_run:
        init_control_db()

    source = sqlite3.connect(source_db_path)
    source.row_factory = sqlite3.Row

    # Get all distinct owners
    owners = source.execute(
        "SELECT DISTINCT owner_email FROM repos"
    ).fetchall()
    emails = [r["owner_email"] for r in owners]

    # If there's only NULL owners, create an admin user
    if all(e is None for e in emails):
        emails = ["admin@yinshi.local"]
        logger.info("No owner_email values found; using admin@yinshi.local")
    else:
        # Include NULL owner repos under the first real email
        emails = [e for e in emails if e is not None]

    logger.info("Found %d distinct owner(s): %s", len(emails), emails)

    for email in emails:
        logger.info("--- Migrating user: %s ---", email)

        if dry_run:
            logger.info("[DRY RUN] Would create user for %s", email)
            continue

        # Create/resolve user in control DB
        tenant = resolve_or_create_user(
            provider="google",
            provider_user_id=f"migrated-{email}",
            email=email,
            display_name=email.split("@")[0],
        )
        logger.info("User created: %s -> %s", email, tenant.user_id)

        # Copy repos belonging to this user
        repos = source.execute(
            "SELECT * FROM repos WHERE owner_email = ? OR owner_email IS NULL",
            (email,),
        ).fetchall()

        with get_user_db(tenant) as user_db:
            for repo in repos:
                repo_dict = dict(repo)
                logger.info("  Repo: %s (%s)", repo_dict["name"], repo_dict["id"])

                # Update root_path if it was under ~/.yinshi/repos/
                old_root = repo_dict["root_path"]
                new_root = old_root  # Keep as-is for local paths

                if "/.yinshi/repos/" in old_root:
                    repo_name = os.path.basename(old_root)
                    new_root = os.path.join(tenant.data_dir, "repos", repo_name)
                    if os.path.exists(old_root) and not os.path.exists(new_root):
                        logger.info("    Moving %s -> %s", old_root, new_root)
                        os.makedirs(os.path.dirname(new_root), exist_ok=True)
                        shutil.move(old_root, new_root)

                # Insert repo (without owner_email)
                user_db.execute(
                    "INSERT INTO repos (id, created_at, updated_at, name, remote_url, root_path, custom_prompt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        repo_dict["id"],
                        repo_dict["created_at"],
                        repo_dict["updated_at"],
                        repo_dict["name"],
                        repo_dict["remote_url"],
                        new_root,
                        repo_dict["custom_prompt"],
                    ),
                )

                # Copy workspaces
                workspaces = source.execute(
                    "SELECT * FROM workspaces WHERE repo_id = ?",
                    (repo_dict["id"],),
                ).fetchall()

                for ws in workspaces:
                    ws_dict = dict(ws)
                    user_db.execute(
                        "INSERT INTO workspaces (id, created_at, updated_at, repo_id, name, branch, path, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            ws_dict["id"],
                            ws_dict["created_at"],
                            ws_dict["updated_at"],
                            ws_dict["repo_id"],
                            ws_dict["name"],
                            ws_dict["branch"],
                            ws_dict["path"],
                            ws_dict["state"],
                        ),
                    )

                    # Copy sessions
                    sessions = source.execute(
                        "SELECT * FROM sessions WHERE workspace_id = ?",
                        (ws_dict["id"],),
                    ).fetchall()

                    for sess in sessions:
                        sess_dict = dict(sess)
                        user_db.execute(
                            "INSERT INTO sessions (id, created_at, updated_at, workspace_id, status, model) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                sess_dict["id"],
                                sess_dict["created_at"],
                                sess_dict["updated_at"],
                                sess_dict["workspace_id"],
                                sess_dict["status"],
                                sess_dict["model"],
                            ),
                        )

                        # Copy messages
                        messages = source.execute(
                            "SELECT * FROM messages WHERE session_id = ?",
                            (sess_dict["id"],),
                        ).fetchall()

                        for msg in messages:
                            msg_dict = dict(msg)
                            user_db.execute(
                                "INSERT INTO messages (id, created_at, session_id, role, content, full_message, turn_id) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    msg_dict["id"],
                                    msg_dict["created_at"],
                                    msg_dict["session_id"],
                                    msg_dict["role"],
                                    msg_dict["content"],
                                    msg_dict["full_message"],
                                    msg_dict["turn_id"],
                                ),
                            )

            user_db.commit()
            logger.info("  Committed %d repos for %s", len(repos), email)

    source.close()
    logger.info("Migration complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate to multi-tenant architecture")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
