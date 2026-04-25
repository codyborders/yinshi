"""Tests for account provisioning and resolution."""

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def account_env(tmp_path, monkeypatch):
    """Set up environment for account tests."""
    control_db = str(tmp_path / "control.db")
    user_data_dir = str(tmp_path / "users")
    monkeypatch.setenv("CONTROL_DB_PATH", control_db)
    monkeypatch.setenv("USER_DATA_DIR", user_data_dir)
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db

    init_control_db()

    yield {
        "control_db": control_db,
        "user_data_dir": user_data_dir,
        "tmp_path": tmp_path,
    }
    get_settings.cache_clear()


def test_provision_user_creates_directory_and_db(account_env):
    """provision_user should create user data dir and initialize DB."""
    from yinshi.services.accounts import provision_user

    tenant = provision_user("user123", "user@example.com")

    assert os.path.isdir(tenant.data_dir)
    assert os.path.isfile(tenant.db_path)
    assert tenant.user_id == "user123"
    assert tenant.email == "user@example.com"


def test_provision_user_db_has_tables(account_env):
    """Provisioned user DB should have all required tables."""
    from yinshi.services.accounts import provision_user

    tenant = provision_user("user456", "user@example.com")

    conn = sqlite3.connect(tenant.db_path)
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    conn.close()

    assert "repos" in tables
    assert "workspaces" in tables
    assert "sessions" in tables
    assert "messages" in tables


def test_resolve_or_create_new_user(account_env):
    """resolve_or_create_user should create a new user on first login."""
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-123",
        email="new@example.com",
        display_name="New User",
        avatar_url=None,
        provider_data=None,
    )

    assert tenant.email == "new@example.com"
    assert os.path.isfile(tenant.db_path)


def test_resolve_or_create_existing_user(account_env):
    """resolve_or_create_user should return existing user on second login."""
    from yinshi.services.accounts import resolve_or_create_user

    tenant1 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-456",
        email="existing@example.com",
        display_name="Existing",
    )

    tenant2 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-456",
        email="existing@example.com",
        display_name="Existing",
    )

    assert tenant1.user_id == tenant2.user_id
    assert tenant1.db_path == tenant2.db_path


def test_resolve_links_new_provider_same_email(account_env):
    """resolve_or_create_user should link new provider to existing account by email."""
    from yinshi.db import get_control_db
    from yinshi.services.accounts import resolve_or_create_user

    # First login via Google
    tenant1 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-789",
        email="shared@example.com",
        display_name="Shared User",
    )

    # Second login via GitHub with same email
    tenant2 = resolve_or_create_user(
        provider="github",
        provider_user_id="github-101",
        email="shared@example.com",
        display_name="Shared User",
    )

    assert tenant1.user_id == tenant2.user_id

    # Should have two oauth_identities
    with get_control_db() as db:
        identities = db.execute(
            "SELECT * FROM oauth_identities WHERE user_id = ?",
            (tenant1.user_id,),
        ).fetchall()
        assert len(identities) == 2
        providers = {r["provider"] for r in identities}
        assert providers == {"google", "github"}


def test_resolve_or_create_user_handles_concurrent_provider_logins(
    account_env,
    monkeypatch,
):
    """Concurrent provider callbacks for one email should converge on one user."""
    from yinshi.db import get_control_db
    from yinshi.services import accounts as accounts_service

    login_barrier = threading.Barrier(2)
    call_log: list[str] = []
    call_log_lock = threading.Lock()

    def fake_generate_dek() -> bytes:
        with call_log_lock:
            call_log.append("generate_dek")
        return b"k" * 32

    def fake_wrap_new_user_dek(dek: bytes, user_id: str) -> bytes:
        assert dek == b"k" * 32
        assert user_id
        with call_log_lock:
            call_log.append("wrap_dek")
        return b"wrapped-dek"

    def resolve_google() -> object:
        login_barrier.wait(timeout=5)
        return accounts_service.resolve_or_create_user(
            provider="google",
            provider_user_id="google-race",
            email="race@example.com",
            display_name="Race User",
        )

    def resolve_github() -> object:
        login_barrier.wait(timeout=5)
        return accounts_service.resolve_or_create_user(
            provider="github",
            provider_user_id="github-race",
            email="race@example.com",
            display_name="Race User",
        )

    monkeypatch.setattr(accounts_service, "generate_dek", fake_generate_dek)
    monkeypatch.setattr(accounts_service, "wrap_new_user_dek", fake_wrap_new_user_dek)

    with ThreadPoolExecutor(max_workers=2) as executor:
        google_future = executor.submit(resolve_google)
        github_future = executor.submit(resolve_github)
        google_tenant = google_future.result(timeout=10)
        github_tenant = github_future.result(timeout=10)

    assert google_tenant.user_id == github_tenant.user_id
    assert call_log.count("generate_dek") == 1
    assert call_log.count("wrap_dek") == 1

    with get_control_db() as db:
        users = db.execute("SELECT id FROM users WHERE email = ?", ("race@example.com",)).fetchall()
        identities = db.execute(
            "SELECT provider FROM oauth_identities WHERE user_id = ? ORDER BY provider",
            (google_tenant.user_id,),
        ).fetchall()

    assert len(users) == 1
    assert [row["provider"] for row in identities] == ["github", "google"]


def test_control_db_has_users_table(account_env):
    """Control DB should have users, OAuth identities, keys, and GitHub installs."""
    from yinshi.db import get_control_db

    with get_control_db() as db:
        tables = [
            r["name"]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]

    assert "users" in tables
    assert "oauth_identities" in tables
    assert "api_keys" in tables
    assert "github_installations" in tables


def test_legacy_data_migrated_on_first_login(account_env):
    """First login should auto-migrate repos/workspaces/sessions/messages from legacy DB."""
    from yinshi.config import get_settings
    from yinshi.db import init_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    settings = get_settings()

    # Seed the legacy DB with data for this email
    init_db()
    legacy = sqlite3.connect(settings.db_path)
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute(
        "INSERT INTO repos (id, name, remote_url, root_path, owner_email) "
        "VALUES ('repo1', 'my-project', 'https://github.com/x/y', '/tmp/repo', 'alice@example.com')"
    )
    legacy.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path, state) "
        "VALUES ('ws1', 'repo1', 'main-ws', 'main', '/tmp/repo/.worktrees/main', 'ready')"
    )
    legacy.execute(
        "INSERT INTO sessions (id, workspace_id, status) " "VALUES ('sess1', 'ws1', 'idle')"
    )
    legacy.execute(
        "INSERT INTO messages (id, session_id, role, content) "
        "VALUES ('msg1', 'sess1', 'user', 'hello world')"
    )
    legacy.commit()
    legacy.close()

    # First login creates user and should auto-migrate
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-alice",
        email="alice@example.com",
        display_name="Alice",
    )

    # Verify data landed in per-user DB
    with get_user_db(tenant) as db:
        repos = db.execute("SELECT * FROM repos").fetchall()
        assert len(repos) == 1
        assert repos[0]["name"] == "my-project"

        workspaces = db.execute("SELECT * FROM workspaces").fetchall()
        assert len(workspaces) == 1
        assert workspaces[0]["name"] == "main-ws"

        sessions = db.execute("SELECT * FROM sessions").fetchall()
        assert len(sessions) == 1

        messages = db.execute("SELECT * FROM messages").fetchall()
        assert len(messages) == 1
        assert messages[0]["content"] == "hello world"


def test_legacy_migration_preserves_agents_md(account_env):
    """Legacy repo migration should preserve repo-level AGENTS.md instructions."""
    from yinshi.config import get_settings
    from yinshi.db import init_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    settings = get_settings()
    init_db()
    legacy = sqlite3.connect(settings.db_path)
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute(
        "INSERT INTO repos (id, name, remote_url, root_path, owner_email, agents_md, installation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "repo-agents",
            "agents-project",
            "https://github.com/x/agents",
            "/tmp/repo-agents",
            "agents@example.com",
            "Custom agent instructions",
            42,
        ),
    )
    legacy.commit()
    legacy.close()

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-agents",
        email="agents@example.com",
        display_name="Agents User",
    )

    with get_user_db(tenant) as db:
        repo_row = db.execute(
            "SELECT agents_md, installation_id FROM repos WHERE id = ?",
            ("repo-agents",),
        ).fetchone()

    assert repo_row is not None
    assert repo_row["agents_md"] == "Custom agent instructions"
    assert repo_row["installation_id"] == 42


def test_legacy_migration_skipped_when_no_legacy_data(account_env):
    """First login with no legacy DB should not fail."""
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-bob",
        email="bob@example.com",
        display_name="Bob",
    )

    with get_user_db(tenant) as db:
        repos = db.execute("SELECT * FROM repos").fetchall()
        assert len(repos) == 0


def test_legacy_migration_skipped_on_second_login(account_env):
    """Second login should not re-migrate or duplicate data."""
    from yinshi.config import get_settings
    from yinshi.db import init_db
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.tenant import get_user_db

    settings = get_settings()

    # Seed legacy DB
    init_db()
    legacy = sqlite3.connect(settings.db_path)
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute(
        "INSERT INTO repos (id, name, remote_url, root_path, owner_email) "
        "VALUES ('repo2', 'project2', 'https://github.com/x/z', '/tmp/repo2', 'carol@example.com')"
    )
    legacy.commit()
    legacy.close()

    # First login -- migrates data
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-carol",
        email="carol@example.com",
        display_name="Carol",
    )

    # Second login -- should not duplicate
    resolve_or_create_user(
        provider="google",
        provider_user_id="google-carol",
        email="carol@example.com",
        display_name="Carol",
    )

    with get_user_db(tenant) as db:
        repos = db.execute("SELECT * FROM repos").fetchall()
        assert len(repos) == 1
