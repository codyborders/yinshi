"""Race-focused workspace file tests using disposable filesystem fixtures."""

import subprocess
from pathlib import Path

import pytest


def test_workspace_file_write_rejects_symlinked_parent(tmp_path: Path) -> None:
    """A browser edit must not follow a parent symlink outside its workspace."""
    from yinshi.services.workspace_files import write_text_file

    workspace_path = tmp_path / "workspace"
    outside_directory = tmp_path / "outside"
    workspace_path.mkdir()
    outside_directory.mkdir()
    outside_file = outside_directory / "target.txt"
    outside_file.write_text("outside", encoding="utf-8")
    (workspace_path / "switch").symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        write_text_file(str(workspace_path), "switch/target.txt", "changed")

    assert outside_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_workspace_diff_rejects_parent_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diff must not follow a parent swapped after Git status validation."""
    from yinshi.services import workspace_files

    workspace_path = tmp_path / "workspace"
    tracked_directory = workspace_path / "switch"
    outside_directory = tmp_path / "outside"
    tracked_directory.mkdir(parents=True)
    outside_directory.mkdir()
    tracked_file = tracked_directory / "target.txt"
    tracked_file.write_text("original\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "init", "-q", str(workspace_path)], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(workspace_path), "add", "."], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(workspace_path),
            "-c",
            "user.name=Yinshi Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    tracked_file.write_text("changed\n", encoding="utf-8")
    (outside_directory / "target.txt").write_text("outside secret\n", encoding="utf-8")

    original_status = workspace_files._changed_file_for_path

    async def swap_after_status(root: Path, display_path: str):
        change = await original_status(root, display_path)
        tracked_directory.rename(workspace_path / "switch-original")
        tracked_directory.symlink_to(outside_directory, target_is_directory=True)
        return change

    monkeypatch.setattr(workspace_files, "_changed_file_for_path", swap_after_status)

    with pytest.raises(PermissionError, match="symlink"):
        await workspace_files.diff_file(str(workspace_path), "switch/target.txt")
