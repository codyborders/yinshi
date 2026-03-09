"""Shared pytest fixtures for Yinshi tests."""

import os
import sqlite3
import subprocess
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory."""
    return str(tmp_path)


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def db(db_path, monkeypatch):
    """Provide an initialized test database."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db

    init_db()
    with get_db() as conn:
        yield conn
    get_settings.cache_clear()


@pytest.fixture
def mock_sidecar():
    """Provide a mock sidecar client."""
    client = AsyncMock()
    client.connected = True
    client.ping.return_value = True
    return client


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    repo_path = str(tmp_path / "test-repo")
    os.makedirs(repo_path)
    subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_path, check=True, capture_output=True,
    )
    readme = os.path.join(repo_path, "README.md")
    with open(readme, "w") as f:
        f.write("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path, check=True, capture_output=True,
    )
    return repo_path
