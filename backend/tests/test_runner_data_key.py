"""Verify portable runner data-protection key lifecycle."""

from __future__ import annotations

import os
import threading
import time
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


def test_concurrent_creation_publishes_only_complete_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent creators should converge without exposing partial final content."""
    import yinshi.services.runner_data_key as data_key

    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"
    first_publish_started = threading.Event()
    allow_first_publish = threading.Event()
    original_link = data_key.os.link
    call_lock = threading.Lock()
    link_calls = 0

    def controlled_link(source: object, target: object, **kwargs: object) -> None:
        nonlocal link_calls
        with call_lock:
            link_calls += 1
            call_number = link_calls
        original_link(source, target, **kwargs)
        if call_number == 1:
            first_publish_started.set()
            assert allow_first_publish.wait(timeout=5)

    monkeypatch.setattr(data_key.os, "link", controlled_link)
    results: list[bytes] = []
    errors: list[BaseException] = []

    def load() -> None:
        try:
            results.append(load_or_create_runner_data_key(key_path, database_root, b"n" * 32))
        except BaseException as exc:
            errors.append(exc)

    winner = threading.Thread(target=load)
    winner.start()
    assert first_publish_started.wait(timeout=5)
    loser = threading.Thread(target=load)
    loser.start()
    time.sleep(0.05)
    assert loser.is_alive()
    allow_first_publish.set()
    winner.join(timeout=5)
    loser.join(timeout=5)

    assert not winner.is_alive() and not loser.is_alive()
    assert errors == []
    assert len(results) == 2 and results[0] == results[1] == key_path.read_bytes()
    assert len(results[0]) == 32
    assert list(database_root.glob(".yinshi-data-protection-key.tmp-*")) == []


def test_first_directory_fsync_failure_never_exposes_published_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication must remain unreadable until its directory entry is durable."""
    import yinshi.services.runner_data_key as data_key

    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"
    original_fsync = data_key.os.fsync
    original_unlink = data_key.os.unlink
    fsync_calls = 0
    observed_keys: list[bytes] = []
    observed_links: list[int] = []
    final_present_during_temporary_cleanup: list[bool] = []

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            observed_links.append(key_path.stat().st_nlink)
            try:
                observed_keys.append(data_key._read_key(key_path))
            except RuntimeError:
                pass
            raise OSError("directory fsync failed")
        original_fsync(descriptor)

    def observe_cleanup_unlink(path: object, *args: object, **kwargs: object) -> None:
        if ".tmp-" in os.fspath(path):
            final_present_during_temporary_cleanup.append(key_path.exists())
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(data_key.os, "fsync", fail_first_directory_fsync)
    monkeypatch.setattr(data_key.os, "unlink", observe_cleanup_unlink)

    with pytest.raises(OSError, match="directory fsync failed"):
        load_or_create_runner_data_key(key_path, database_root, b"n" * 32)

    assert observed_links == [2]
    assert observed_keys == []
    assert final_present_during_temporary_cleanup == [False]
    assert not key_path.exists()
    assert list(database_root.glob(".yinshi-data-protection-key.tmp-*")) == []


def test_second_directory_fsync_failure_preserves_durable_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-cleanup fsync failure must not roll back an observable key."""
    import yinshi.services.runner_data_key as data_key

    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"
    original_fsync = data_key.os.fsync
    fsync_calls = 0

    def fail_second_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 3:
            raise OSError("cleanup fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(data_key.os, "fsync", fail_second_directory_fsync)

    with pytest.raises(OSError, match="cleanup fsync failed"):
        load_or_create_runner_data_key(key_path, database_root, b"n" * 32)

    published_key = key_path.read_bytes()
    assert len(published_key) == 32
    assert load_or_create_runner_data_key(key_path, database_root, b"x" * 32) == published_key
    assert list(database_root.glob(".yinshi-data-protection-key.tmp-*")) == []


def test_publish_failure_removes_private_temporary_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed publication should leave neither final nor temporary key files."""
    import yinshi.services.runner_data_key as data_key

    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(data_key.os, "link", fail_link)

    with pytest.raises(PermissionError, match="denied"):
        load_or_create_runner_data_key(key_path, database_root, b"n" * 32)

    assert not key_path.exists()
    assert list(database_root.glob(".yinshi-data-protection-key.tmp-*")) == []


def test_temporary_cleanup_failure_preserves_durable_published_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary cleanup failure must not roll back a durable published key."""
    import yinshi.services.runner_data_key as data_key

    database_root = tmp_path / "sqlite"
    key_path = database_root / ".yinshi-data-protection-key"
    original_unlink = data_key.os.unlink
    failed = False

    def fail_first_temporary_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed and ".tmp-" in os.fspath(path):
            failed = True
            raise PermissionError("cleanup denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(data_key.os, "unlink", fail_first_temporary_unlink)

    with pytest.raises(PermissionError, match="cleanup denied"):
        load_or_create_runner_data_key(key_path, database_root, b"n" * 32)

    published_key = key_path.read_bytes()
    assert len(published_key) == 32
    assert load_or_create_runner_data_key(key_path, database_root, b"x" * 32) == published_key
    assert list(database_root.glob(".yinshi-data-protection-key.tmp-*")) == []


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
