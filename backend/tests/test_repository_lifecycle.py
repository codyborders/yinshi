"""Cross-process repository lifecycle lock tests."""

from __future__ import annotations

import asyncio
import multiprocessing
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
