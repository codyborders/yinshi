"""Cross-process repository lifecycle lock tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest

from yinshi.services.repository_lifecycle import repository_lifecycle


def _hold_repository_lock(
    lock_root: str,
    repo_id: str,
    entered: Any,
    release: Any,
) -> None:
    """Hold one lifecycle lock in an independent process."""

    async def hold() -> None:
        async with repository_lifecycle(repo_id, Path(lock_root)):
            entered.set()
            await asyncio.to_thread(release.wait)

    asyncio.run(hold())


@pytest.mark.asyncio
async def test_repository_lock_file_open_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-file opening should leave unrelated async work responsive."""
    from yinshi.services import repository_lifecycle as lifecycle_module

    original_open = lifecycle_module._open_lock_file
    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    def blocking_open(lock_root: Path, repo_id: str) -> int:
        assert release_operation.wait(timeout=2)
        return original_open(lock_root, repo_id)

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(lifecycle_module, "_open_lock_file", blocking_open)
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        async with lifecycle_module.repository_lifecycle("repo-id", tmp_path):
            pass
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert ticks >= 5


@pytest.mark.asyncio
async def test_repository_lifecycle_serializes_across_processes(tmp_path: Path) -> None:
    """A second process cannot enter until the first process exits."""
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_repository_lock,
        args=(str(tmp_path), "shared-repository", entered, release),
    )
    process.start()
    try:
        assert await asyncio.to_thread(entered.wait, 5)

        second_entered = asyncio.Event()

        async def enter_second() -> None:
            async with repository_lifecycle("shared-repository", tmp_path):
                second_entered.set()

        second = asyncio.create_task(enter_second())
        await asyncio.sleep(0.15)
        assert not second_entered.is_set()

        release.set()
        await asyncio.wait_for(second, timeout=5)
        assert second_entered.is_set()
    finally:
        release.set()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
    assert process.exitcode == 0
