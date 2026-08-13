"""Verify portable runner data-protection key lifecycle."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yinshi.services.runner_data_key import load_or_create_runner_data_key


def test_fresh_root_creates_stable_random_key_independent_from_noise(tmp_path: Path) -> None:
    """Fresh storage receives a stable key independent from transport identity."""
    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"

    first = load_or_create_runner_data_key(key_path, database_root, b"n" * 32)
    second = load_or_create_runner_data_key(key_path, database_root, b"x" * 32)

    assert len(first) == 32
    assert first != b"n" * 32
    assert first == second
    assert key_path.read_bytes() == first
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert database_root.stat().st_mode & 0o777 == 0o700


def test_initialized_legacy_root_seeds_from_noise_key(tmp_path: Path) -> None:
    """Existing databases retain their current Noise-derived encryption secrets."""
    database_root = tmp_path / "sqlite"
    database_root.mkdir(mode=0o700)
    (database_root / "control.db").write_bytes(b"durable")
    legacy_key = b"l" * 32

    key = load_or_create_runner_data_key(
        database_root / ".yinshi-data-protection-key",
        database_root,
        legacy_key,
    )

    assert key == legacy_key


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "wrong-mode", "wrong-length"])
def test_existing_invalid_key_fails_closed(tmp_path: Path, kind: str) -> None:
    """Portable keys reject aliases, weak modes, and malformed material."""
    database_root = tmp_path / "sqlite"
    database_root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    key_path = database_root / ".yinshi-data-protection-key"
    if kind == "symlink":
        key_path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, key_path)
    else:
        key_path.write_bytes(b"short" if kind == "wrong-length" else b"k" * 32)
        key_path.chmod(0o644 if kind == "wrong-mode" else 0o600)

    with pytest.raises(RuntimeError, match="data-protection key"):
        load_or_create_runner_data_key(key_path, database_root, b"n" * 32)
